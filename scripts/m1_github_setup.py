#!/usr/bin/env python
"""M1 验收辅助：创建/推送 demo 仓库到 GitHub（供真机端到端验收用）。

凭证来源（按优先级）：
1. 环境变量 ``GITHUB_TOKEN``；
2. git credential helper（与 `git push` 用的是同一份凭证）。

凭证只在本进程内存里使用：不进 argv、不打印、不落盘。

用法::

    uv run python scripts/m1_github_setup.py --repo mewcode-alert-demo \
        --path /tmp/m1demo --account pretextQ
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request


def read_token_from_credential_helper(host: str = "github.com") -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol=https\nhost={host}\n\n".encode(),
        capture_output=True,
    )
    if proc.returncode != 0:
        return ""
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def api(token: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, {"message": body[:300]}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="create/push the M1 demo repository")
    parser.add_argument("--repo", default="mewcode-alert-demo")
    parser.add_argument("--path", required=True, help="local demo repo created by m1_demo.py")
    parser.add_argument("--account", required=True, help="github account owner of the repo")
    parser.add_argument("--branch", default="main")
    args = parser.parse_args(argv)

    import os

    token = os.environ.get("GITHUB_TOKEN", "") or read_token_from_credential_helper()
    if not token:
        print("error: no token (set GITHUB_TOKEN or configure a git credential helper)", file=sys.stderr)
        return 2

    status, body = api(token, "GET", f"/repos/{args.account}/{args.repo}")
    if status == 404:
        status, body = api(
            token, "POST", "/user/repos",
            {
                "name": args.repo,
                "description": "Throwaway demo repo for MewCode's alert-driven autofix pipeline",
                "private": False,
                "has_issues": True,
                "has_wiki": False,
                "has_projects": False,
            },
        )
        if status not in (200, 201):
            print(f"error: cannot create repo ({status}): {body.get('message')}", file=sys.stderr)
            return 1
        print(f"created repository: {body['html_url']}")
    elif status == 200:
        print(f"repository already exists: {body['html_url']} (base branch {body.get('default_branch')})")
    else:
        print(f"error: cannot query repo ({status}): {body.get('message')}", file=sys.stderr)
        return 1

    # 推 demo 代码：token 不进 argv（走 credential helper 已存凭证）
    remote = f"https://github.com/{args.account}/{args.repo}.git"
    subprocess.run(["git", "remote", "remove", "origin"], cwd=args.path, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=args.path, check=True)
    subprocess.run(["git", "push", "-u", "origin", args.branch], cwd=args.path, check=True)
    print(f"pushed {args.branch} to {remote}")
    print(f"demo repo ready: https://github.com/{args.account}/{args.repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
