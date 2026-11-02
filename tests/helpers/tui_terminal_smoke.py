"""Interactive offline smoke fixture using the production OS terminal driver.

Run with ``python -m tests.helpers.tui_terminal_smoke --evidence DIR``.
Type history / stream / permission / failure to exercise real terminal input.
The crash command exits only this offline fixture immediately for cleanup checks.
No model, shell tool, MCP server, session store or repository configuration is used.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import tempfile
import time
from pathlib import Path

from mewcode.agent import (
    ErrorEvent,
    LoopComplete,
    PermissionRequest,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
)
from mewcode.app import ChatInput
from mewcode.config import ProviderConfig
from mewcode.conversation import Message
from mewcode.driver import NoAltScreenDriver
from tests.helpers.tui_offline import OfflineApp


class TerminalSmokeApp(OfflineApp):
    def __init__(self, work_dir: str, evidence: Path) -> None:
        self.evidence = evidence
        self.previous_snapshot = ""
        super().__init__(work_dir)

    def get_driver_class(self):
        return NoAltScreenDriver

    def on_mount(self) -> None:
        self.set_interval(0.25, self.record_state)
        self.call_after_refresh(self.seed_history)

    def _select_provider(self, provider: ProviderConfig) -> None:
        super()._select_provider(provider)
        self.agent.run = self.fixture_run

    def record(self, kind: str, **values) -> None:
        with (self.evidence / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, "time": time.time(), **values}, ensure_ascii=False) + "\n")

    async def seed_history(self) -> None:
        self.record("started", platform=platform.platform(), driver=type(self._driver).__name__)
        await self._render_restored_messages([
            Message(role="user", content="离线真实终端验收：中文 English 全角ＡＢＣ，标点【】（）。“引用”"),
            Message(role="assistant", content=(
                "**离线验收替身，未调用模型或执行工具。**\n\n"
                "输入 history / stream / permission / failure；F2 详情，Ctrl+End 返回最新。\n\n"
                + "\n\n".join(
                    f"历史 {index:02d} · 中文 English ＡＢＣ · 路径 /workspace/含 空格/[demo]/src/parser.py · "
                    "命令 python -m pytest tests/test_parser.py --verbose --capture=no"
                    for index in range(1, 46)
                )
            )),
        ])

    def record_state(self) -> None:
        chat = self.query_one("#chat-area")
        inp = self.query_one(ChatInput)
        snapshot = {
            "size": list(self.size), "input": list(inp.region), "chat": list(chat.region),
            "scroll_y": chat.scroll_y, "max_scroll_y": chat.max_scroll_y,
            "anchored": chat.is_anchored, "details": self._details_open,
            "focus": type(self.focused).__name__ if self.focused else None,
            "draft": inp.text, "phase": getattr(self, "_run_phase", ""),
        }
        encoded = json.dumps(snapshot, ensure_ascii=False)
        if encoded != self.previous_snapshot:
            self.record("state", **snapshot)
            self.previous_snapshot = encoded

    async def fixture_run(self, conversation):
        text = conversation.history[-1].content
        self.record("input", text=text)
        if text == "crash":
            os._exit(7)
        elif text == "history":
            yield StreamText("\n\n".join(f"新增历史 {index:02d} · 中文全角ＡＢＣ English" for index in range(60)))
        elif text == "stream":
            yield ToolUseEvent("Bash", "offline-wait", {"command": "OFFLINE FIXTURE: no command executed"})
            for index in range(180):
                yield StreamText(f"流式片段 {index:03d} · 中英混排ＡＢＣ\n\n")
                await asyncio.sleep(0.5)
            yield ToolResultEvent("offline-wait", "Bash", "Offline fixture result only", False, 90.0)
        elif text == "permission":
            future = asyncio.get_running_loop().create_future()
            yield PermissionRequest("Bash", "OFFLINE FIXTURE /workspace/含 空格/[demo]/src/parser.py — no execution", future)
            response = await future
            self.record("permission", response=str(response))
            yield StreamText("权限响应已记录；替身没有执行任何命令。")
        elif text == "failure":
            yield ErrorEvent("离线失败示例：中文 [red]literal[/red]；不执行、不重试。")
        else:
            yield StreamText(f"离线输入回显：{text}\n\n中文 English ＡＢＣ，尾部完整。")
        yield TurnComplete(1)
        yield LoopComplete(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mewcode-terminal-smoke-") as root:
        app = TerminalSmokeApp(root, args.evidence.resolve())
        app.run()
        app.record("exited")


if __name__ == "__main__":
    main()
