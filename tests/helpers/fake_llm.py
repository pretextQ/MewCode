"""测试用的假 LLM：最小 OpenAI Chat Completions（SSE 流式）实现。

用途：让**容器内的完整 agent 路径**（`mewcode -p --output-format json`）在没有
真实模型、没有网络的前提下跑通——这是唯一能自动发现"容器里那条命令根本跑
不起来"这类问题的手段（真机演示前踩到过：`--config` 参数当时并不存在）。

按脚本回答：第 1 轮用 tool_calls 调一个工具，工具结果回来后的第 2 轮给最终
文本。脚本是固定的、确定性的——测试断言的是"链路通"，不是"模型聪明"。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_FINAL_TEXT = (
    "ROOT CAUSE: the service crashed because TIMEOUT_SECONDS was 0\n"
    "FIX: set it to a positive value\n"
    "VERIFICATION: ran the test script"
)


def _sse(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode("utf-8")


def _chunk(delta: dict, finish_reason: str | None, usage: dict | None = None) -> bytes:
    body = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "fake-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return _sse(body)


class _ChatHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeLLM"

    def log_message(self, *args) -> None:
        pass

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _respond_sse(self, chunks: list[bytes]) -> None:
        body = b"".join(chunks) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # /v1/models 之类的探测：404 即可（调用方容忍）
        self.server.requests.append({"method": "GET", "path": self.path, "body": None})  # type: ignore[attr-defined]
        self._respond(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        self.server.requests.append(  # type: ignore[attr-defined]
            {"method": "POST", "path": self.path, "headers": dict(self.headers), "body": body}
        )
        if not self.path.endswith("/chat/completions"):
            self._respond(404, {"error": {"message": f"unsupported path {self.path}"}})
            return

        turn = self.server.turn_index  # type: ignore[attr-defined]
        self.server.turn_index = turn + 1  # type: ignore[attr-defined]
        script = self.server.script  # type: ignore[attr-defined]
        step = script[min(turn, len(script) - 1)]

        if "tool_calls" in step:
            chunks: list[bytes] = []
            for index, (name, arguments) in enumerate(step["tool_calls"]):
                chunks.append(_chunk({"tool_calls": [{
                    "index": index, "id": f"call_{index}", "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }]}, finish_reason=None))
            chunks.append(_chunk({}, finish_reason="tool_calls", usage={
                "prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140,
            }))
            self._respond_sse(chunks)
            return

        text = step.get("text", DEFAULT_FINAL_TEXT)
        self._respond_sse([
            _chunk({"role": "assistant", "content": text}, finish_reason=None),
            _chunk({}, finish_reason="stop", usage={
                "prompt_tokens": 200, "completion_tokens": 60, "total_tokens": 260,
            }),
        ])


class FakeLLM:
    """按 ``script`` 依次回答的假模型。

    script 元素：
      {"tool_calls": [("mcp_logs_query_logs", '{"query": "{app=\\"x\\"}"}')]}
      {"text": "最终答复"}
    最后一轮会重复用于超出的请求，方便断言"工具调用之后模型给了结论"。
    """

    def __init__(
        self,
        script: list[dict] | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._server = ThreadingHTTPServer((host, port), _ChatHandler)
        self.requests: list[dict] = []
        self._server.requests = self.requests  # type: ignore[attr-defined]
        self._server.turn_index = 0  # type: ignore[attr-defined]
        self._server.script = script or []  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{port}/v1"

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> FakeLLM:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def posted_bodies(self) -> list[dict]:
        return [r["body"] for r in self.requests if r["method"] == "POST" and r["body"] is not None]


def main() -> None:
    parser = argparse.ArgumentParser(description="fake OpenAI-compatible LLM for tests")
    parser.add_argument("--port", type=int, default=18321)
    parser.add_argument("--tool", default="mcp_logs_query_logs")
    parser.add_argument("--tool-args", default='{"query": "{app=\\"checkout\\"}"}')
    args = parser.parse_args()
    llm = FakeLLM(
        script=[
            {"tool_calls": [(args.tool, args.tool_args)]},
            {"text": DEFAULT_FINAL_TEXT},
        ],
        host="0.0.0.0",
        port=args.port,
    )
    llm.start()
    print(f"fake llm ready: {llm.base_url}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
