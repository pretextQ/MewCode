"""测试用的假内部后端：Loki 与 GitHub API 的最小实现（仅回答问题，不模拟全协议）。

两个用途：
1. 单测里进程内启动（``make_loki()`` / ``make_github()``），喂给内置 MCP server；
2. 沙箱真机测试里作为独立进程跑在**容器内**（``python fake_backends.py ...``），
   这样"日志数据 → MCP server → agent 工具调用"这条链在没有真实内网、
   也不依赖宿主网络的条件下就能被验证。

每个 handler 只实现被问到的 GET；其它方法一律记下并回 405——测试据此断言
"整条链路没有写过外部系统"（只读是硬约束，不是口头承诺）。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_LOG_TS_NS = 1_759_500_000_000_000_000  # 固定时间戳：断言不用跟时钟赛跑
DEFAULT_LOG_LINE = "ERROR: TIMEOUT_SECONDS must be > 0 — upstream call timed out after 0s"
DEFAULT_LOG_STREAM = {"app": "checkout", "env": "prod"}
DEFAULT_WORKFLOW_RUNS = [
    {
        "run_number": 41,
        "name": "pytest",
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
        "created_at": "2026-10-04T06:00:00Z",
        "html_url": "https://github.com/demo/repo/actions/runs/41",
    }
]
DEFAULT_CHECK_RUNS = [
    {
        "name": "pytest (ubuntu-latest)",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/demo/repo/runs/1",
    }
]


@dataclass
class RecordedRequest:
    method: str
    path: str
    params: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeBackend"

    def log_message(self, *args) -> None:  # 静音：测试输出只要结果
        pass

    # -- 记录与非 GET ---------------------------------------------------
    def _record(self, method: str) -> RecordedRequest:
        parsed = urlparse(self.path)
        record = RecordedRequest(
            method=method,
            path=parsed.path,
            params=parse_qs(parsed.query),
            headers={k: v for k, v in self.headers.items()},
        )
        self.server.requests.append(record)  # type: ignore[attr-defined]
        return record

    def _respond(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reject_write(self, method: str) -> None:
        self._record(method)
        self._respond({"error": "this fake backend is read-only"}, status=405)

    do_POST = do_PUT = do_PATCH = do_DELETE = _reject_write


class _LokiHandler(_Handler):
    def do_GET(self) -> None:
        record = self._record("GET")
        fail_status = getattr(self.server, "fail_status", None)  # type: ignore[attr-defined]
        if fail_status:
            self._respond({"status": "error", "message": "backend exploded"}, status=fail_status)
            return
        if record.path == "/loki/api/v1/query_range":
            values = [[str(self.server.log_ts_ns), line] for line in self.server.log_lines]  # type: ignore[attr-defined]
            self._respond({
                "status": "success",
                "data": {"resultType": "streams", "result": [{
                    "stream": self.server.log_stream,  # type: ignore[attr-defined]
                    "values": values,
                }] if values else []},
            })
        elif record.path == "/loki/api/v1/labels":
            self._respond({"status": "success", "data": ["app", "env"]})
        elif record.path.startswith("/loki/api/v1/label/") and record.path.endswith("/values"):
            self._respond({"status": "success", "data": sorted(self.server.log_stream.values())})  # type: ignore[attr-defined]
        else:
            self._respond({"error": "not found"}, status=404)


class _GitHubHandler(_Handler):
    def do_GET(self) -> None:
        record = self._record("GET")
        parts = record.path.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "repos" and parts[1] == "missing":
            self._respond({"message": "Not Found"}, status=404)
        elif len(parts) == 5 and parts[3] == "actions" and parts[4] == "runs":
            self._respond({"total_count": len(self.server.workflow_runs), "workflow_runs": self.server.workflow_runs})  # type: ignore[attr-defined]
        elif len(parts) == 6 and parts[3] == "commits" and parts[5] == "check-runs":
            self._respond({"total_count": len(self.server.check_runs), "check_runs": self.server.check_runs})  # type: ignore[attr-defined]
        else:
            self._respond({"message": "Not Found"}, status=404)


class FakeBackend:
    """跑在后台线程里的假后端；``requests`` 记录所有到达的请求。"""

    def __init__(self, handler, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._server = ThreadingHTTPServer((host, port), handler)
        self.requests: list[RecordedRequest] = []
        self._server.requests = self.requests  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def start(self) -> FakeBackend:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def request_paths(self) -> list[str]:
        return [r.path for r in self.requests]


def make_loki(
    lines: list[str] | None = None,
    stream: dict[str, str] | None = None,
    *,
    ts_ns: int = DEFAULT_LOG_TS_NS,
    host: str = "127.0.0.1",
    port: int = 0,
    fail_status: int | None = None,
) -> FakeBackend:
    backend = FakeBackend(_LokiHandler, host=host, port=port)
    backend._server.log_lines = list(lines) if lines is not None else [DEFAULT_LOG_LINE]  # type: ignore[attr-defined]
    backend._server.log_stream = dict(stream or DEFAULT_LOG_STREAM)  # type: ignore[attr-defined]
    backend._server.log_ts_ns = ts_ns  # type: ignore[attr-defined]
    backend._server.fail_status = fail_status  # type: ignore[attr-defined]
    return backend


def make_github(
    runs: list[dict] | None = None,
    checks: list[dict] | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> FakeBackend:
    backend = FakeBackend(_GitHubHandler, host=host, port=port)
    backend._server.workflow_runs = [dict(r) for r in (runs if runs is not None else DEFAULT_WORKFLOW_RUNS)]  # type: ignore[attr-defined]
    backend._server.check_runs = [dict(c) for c in (checks if checks is not None else DEFAULT_CHECK_RUNS)]  # type: ignore[attr-defined]
    return backend


def main() -> None:
    """容器内独立进程用：起两个假后端并阻塞（Ctrl-C / 容器退出即结束）。"""
    parser = argparse.ArgumentParser(description="fake Loki + GitHub backends for tests")
    parser.add_argument("--loki-port", type=int, default=8899)
    parser.add_argument("--github-port", type=int, default=8900)
    args = parser.parse_args()

    loki = make_loki(host="0.0.0.0", port=args.loki_port)
    github = make_github(host="0.0.0.0", port=args.github_port)
    loki.start()
    github.start()
    print(f"fake backends ready: loki={loki.base_url} github={github.base_url}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
