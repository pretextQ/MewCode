"""Capture actual offline Textual SVG evidence: uv run python -m tests.helpers.tui_capture."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from mewcode.agent import LoopComplete, PermissionRequest, StreamText, ToolUseEvent
from mewcode.app import ChatInput
from mewcode.conversation import Message
from tests.helpers.tui_offline import OfflineApp


async def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/evidence/tui-2026-11-01/final").resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory() as root:
        for size in [(80, 24), (120, 36), (160, 48), (60, 18)]:
            app = OfflineApp(root)
            async with app.run_test(size=size) as pilot:
                async def capture(name, app=app, pilot=pilot, size=size):
                    await pilot.pause()
                    app.save_screenshot(str(out / f"{name}-{size[0]}x{size[1]}.svg"))

                await capture("initial")
                await app._render_restored_messages([
                    Message(role="user", content="请检查中文输入与布局。"),
                    Message(role="assistant", content="已保留 **Textual** 界面。\n\n- 用户无需标题\n- 输入固定可见"),
                ])
                await capture("conversation")
                await app.action_details()
                await app.query_one("#details-panel").refresh_git()
                await capture("details")
                app.query_one("TabbedContent").active = "execution-tab"
                await capture("execution-empty")
                await app.action_details()
                started = asyncio.Event()

                async def run(_, started=started):
                    yield StreamText("正在读取配置，等待工具结束。")
                    yield ToolUseEvent("Bash", "t", {"command": "python -m pytest tests/test_parser.py"})
                    started.set()
                    await asyncio.Event().wait()
                    yield LoopComplete(1)

                app.agent.run = run
                task = asyncio.create_task(app._send_message("运行检查"))
                await started.wait()
                await capture("running")
                app.action_cancel()
                await task
                await capture("cancelled")
                await app.action_details()
                await capture("execution-records")
                await app.action_details()
                future = asyncio.get_running_loop().create_future()
                await app._handle_permission_request(PermissionRequest("Bash", "python -m pytest tests/test_parser.py", future))
                await capture("permission")
                await pilot.press("escape")
                app._show_error("示例工具失败：返回码 1；未自动重试。")
                app._set_run_status("出错")
                await capture("failure")
                await app._render_restored_messages([Message(role="assistant", content="长历史中文与路径 /workspace/src/parser.py\n\n" * 90)])
                inp = app.query_one(ChatInput)
                inp.load_text("草稿第一行\n草稿第二行")
                await capture("history")
                rows.append({"size": size, "input": str(inp.region), "chat": str(app.query_one("#chat-area").region)})
    (out / "layout.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"Evidence: {out}")


if __name__ == "__main__":
    asyncio.run(main())
