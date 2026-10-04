#!/usr/bin/env python
"""M1 demo 工具：准备一个"埋了 bug 的 demo 仓库"，并发送模拟告警。

用法（在 MewCode 仓库根目录）::

    # 1) 造 demo 仓库（本地 git，可推到 GitHub 当验收目标）
    uv run python scripts/m1_demo.py init --path D:/tmp/mewcode-demo --bug config_error

    # 2) 服务起来后，模拟 Alertmanager 发一条告警
    uv run python scripts/m1_demo.py alert --port 8321 --repo demo --bug config_error

设计说明：三类 bug（空指针 / 配置错误 / 超时未处理）与 tests/test_service_e2e.py
里的 DEMO_BUGS 保持一致——自动化测试与人工 demo 用同一套素材。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

BUGS: dict[str, tuple[str, str, str]] = {
    "null_deref": (
        "def owner_name(user):\n    return user['profile']['name'].upper()\n",
        "def owner_name(user):\n    profile = user.get('profile') or {}\n"
        "    return (profile.get('name') or 'unknown').upper()\n",
        "import app\n"
        "try:\n"
        "    name = app.owner_name({'profile': None})\n"
        "except Exception as e:\n"
        "    print('null_deref crash: %r' % e); raise SystemExit(1)\n"
        "print('ok:', name)\n",
    ),
    "config_error": (
        "TIMEOUT_SECONDS = 0\n\n\ndef timeout():\n    return TIMEOUT_SECONDS\n",
        "TIMEOUT_SECONDS = 30\n\n\ndef timeout():\n    return TIMEOUT_SECONDS\n",
        "import app\n"
        "if app.timeout() <= 0:\n"
        "    print('config_error: timeout is %r' % app.timeout()); raise SystemExit(1)\n"
        "print('ok:', app.timeout())\n",
    ),
    "unhandled_timeout": (
        "def fetch(client):\n    return client.get('/data')\n",
        "def fetch(client):\n    try:\n        return client.get('/data')\n"
        "    except TimeoutError:\n        return None\n",
        "import app\n\n"
        "class Client:\n"
        "    def get(self, path):\n"
        "        raise TimeoutError('upstream timed out')\n\n"
        "try:\n"
        "    app.fetch(Client())\n"
        "except TimeoutError as e:\n"
        "    print('unhandled_timeout: %r' % e); raise SystemExit(1)\n"
        "print('ok: timeout handled')\n",
    ),
}

WORKFLOW = """name: CI

on:
  push:
    branches: ["**"]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Run tests
        run: python test_app.py
"""

#: M2 W4 demo：仓库带 compose 依赖 + 只有真起依赖才能过的集成测试。
#: 服务层负责 `docker compose -p mewfix-<job> up -d --wait`，集成测试在沙箱
#: 容器内跑（加入 compose 网络，按服务名 cache 寻址）；CI 不跑它。
COMPOSE_YML = """services:
  cache:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 3s
      retries: 30
"""

INTEGRATION_TEST = '''"""Integration check: needs the docker-compose cache service (M2 W4).

Runs inside the sandbox container joined to the compose project network, so the
dependency is reached by its service name ("cache") - no host ports involved.
"""
import socket

with socket.create_connection(("cache", 6379), timeout=10) as sock:
    sock.sendall(b"PING\\r\\n")
    if b"PONG" not in sock.recv(1024):
        raise SystemExit("integration: cache did not answer PING")
    sock.sendall(b"SET mewcode ok\\r\\n")
    sock.recv(1024)
    sock.sendall(b"GET mewcode\\r\\n")
    if b"ok" not in sock.recv(1024):
        raise SystemExit("integration: cache lost the value")
print("integration ok: cache reachable and responsive")
'''


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(path), capture_output=True, text=True, check=check)


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    buggy, _fixed, test_source = BUGS[args.bug]
    path.mkdir(parents=True, exist_ok=True)

    if not (path / ".git").exists():
        _git(path, "init")
        _git(path, "config", "user.email", "demo@example.com")
        _git(path, "config", "user.name", "MewCode Demo")
    _git(path, "checkout", "-B", args.branch)

    (path / "app.py").write_text(buggy, encoding="utf-8")
    (path / "test_app.py").write_text(test_source, encoding="utf-8")
    if args.with_compose:
        (path / "docker-compose.yml").write_text(COMPOSE_YML, encoding="utf-8")
        (path / "test_integration.py").write_text(INTEGRATION_TEST, encoding="utf-8")
    (path / "README.md").write_text(
        "# mewcode-alert-demo\n\n"
        "Throwaway repository for demonstrating MewCode's alert-driven fix pipeline.\n"
        f"Planted bug: `{args.bug}`.\n"
        + (
            "\nHas a docker-compose dependency (`cache`) and an integration test that\n"
            "only passes when the service layer starts it (M2 W4).\n"
            if args.with_compose
            else ""
        ),
        encoding="utf-8",
    )
    workflow = path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(WORKFLOW, encoding="utf-8")

    _git(path, "add", ".")
    _git(path, "commit", "-m", f"demo: planted {args.bug}", check=False)
    print(f"demo repo ready at {path} (branch {args.branch}, bug {args.bug})")
    print(f"next: git remote add origin <your-demo-repo> && git push -u origin {args.branch}")
    return 0


def cmd_alert(args: argparse.Namespace) -> int:
    summary = args.summary or f"{args.bug} in production"
    payload = {
        "version": "4",
        "status": "firing",
        "receiver": "mewcode",
        "groupKey": '{}:{alertname="DemoBug"}',
        "commonLabels": {"alertname": "DemoBug", "severity": "critical"},
        "commonAnnotations": {"summary": summary},
        "externalURL": "http://localhost:9093",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "DemoBug",
                    "severity": "critical",
                    "repository": args.repo,
                    "service": "demo-api",
                },
                "annotations": {
                    "summary": summary,
                    "description": f"traceback points at app.py ({args.bug})",
                    "runbook_url": "https://example.com/runbook/demo",
                },
                "startsAt": "2026-10-04T10:00:00Z",
                "generatorURL": "http://localhost:9090/graph",
                "fingerprint": args.fingerprint or f"demo-fp-{args.bug}",
            }
        ],
    }
    url = f"http://{args.host}:{args.port}/webhook/alert"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **({"X-MewCode-Token": args.token} if args.token else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 4xx/5xx 也要把服务端说明打出来
        print(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"cannot reach the service at {url}: {e.reason}", file=sys.stderr)
        print("hint: start it with `uv run mewcode serve --port <port>`", file=sys.stderr)
        return 1
    print(json.dumps(body, ensure_ascii=False, indent=2))
    if body.get("accepted"):
        print(f"\njob accepted: {body['accepted'][0]['id']}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="M1 demo helper")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a demo repo with a planted bug")
    init.add_argument("--path", required=True)
    init.add_argument("--bug", choices=sorted(BUGS), default="config_error")
    init.add_argument("--branch", default="main")
    init.add_argument(
        "--with-compose",
        action="store_true",
        help="also add docker-compose.yml + test_integration.py (M2 W4 self-started test env)",
    )
    init.set_defaults(func=cmd_init)

    alert = sub.add_parser("alert", help="send a simulated Alertmanager webhook")
    alert.add_argument("--host", default="127.0.0.1")
    alert.add_argument("--port", type=int, default=8321)
    alert.add_argument("--repo", default="demo", help="repository name in service.repos")
    alert.add_argument("--bug", choices=sorted(BUGS), default="config_error")
    alert.add_argument("--summary", default="")
    alert.add_argument("--fingerprint", default="")
    alert.add_argument("--token", default="")
    alert.set_defaults(func=cmd_alert)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
