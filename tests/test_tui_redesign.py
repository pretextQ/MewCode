"""Offline acceptance of real Textual layout, streaming, focus and cancellation."""
from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Markdown, Static

from mewcode.agent import (
    ErrorEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    StreamText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
)
from mewcode.app import ChatInput, ToolCallBlock
from mewcode.commands.completion import CompletionPopup
from mewcode.conversation import Message, ToolResultBlock, ToolUseBlock
from mewcode.permissions.replies import normalize_reply
from mewcode.tui.messages import ChatScroll, assistant_row
from tests.helpers.tui_offline import OfflineApp


def script(app, events):
    async def run(_):
        for event in events:
            if isinstance(event, asyncio.Event):
                await event.wait()
            elif isinstance(event, BaseException):
                raise event
            else:
                yield event
    app.agent.run = run


def status(app):
    return str(app.query_one("#run-status", Static).content)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 36), (160, 48), (60, 18)])
async def test_input_visible_with_long_history_and_completion(tmp_path, size):
    app = OfflineApp(str(tmp_path))
    async with app.run_test(size=size) as pilot:
        await app._render_restored_messages([
            Message(role="user", content="中文全角ＡＢＣ /long/path/" * 8),
            Message(role="assistant", content="很多历史\n\n" * 80),
        ])
        inp = app.query_one(ChatInput)
        inp.load_text("多行草稿\n" * 20)
        popup = app.query_one(CompletionPopup)
        popup.show([f"/entry{i}" for i in range(10)])
        for _ in range(9):
            popup.move_down()
        await pilot.pause()
        assert inp.region in app.screen.region
        assert app.query_one("#mode-label").region in app.screen.region
        assert app.query_one("#mode-label").region in app.query_one("#status-bar").content_region
        assert "/entry9" in str(popup.content)
        assert app.query_one("#chat-area").region.bottom <= app.query_one("#input-area").region.y
        inp.focus()
        await pilot.press("escape", "x")
        assert not popup.is_visible and app.focused is inp
        assert "x" in inp.text


@pytest.mark.asyncio
async def test_stream_final_and_restored_share_markdown_without_lost_tail(tmp_path):
    app = OfflineApp(str(tmp_path))
    async with app.run_test() as pilot:
        script(app, [StreamText("**中文"), StreamText("答案**尾部"), TurnComplete(1), LoopComplete(1)])
        await app._send_message("问题")
        await pilot.pause()
        live = [m._markdown for m in app.query(Markdown)]
        assert live == ["**中文答案**尾部"]
        assert str(app.query_one(".user-message", Static).content) == "› 问题"
        assert len(app.query(".assistant-label")) == 1
        assert status(app) == "本次运行结束"
        await app._render_restored_messages([
            Message(role="user", content="问题"), Message(role="assistant", content=live[0]),
        ])
        assert [m._markdown for m in app.query(Markdown)] == live


@pytest.mark.asyncio
async def test_stream_segments_survive_tools_and_keyboard_expand(tmp_path):
    app = OfflineApp(str(tmp_path))
    async with app.run_test() as pilot:
        script(app, [StreamText("前文"), ToolUseEvent("Bash", "x", {"command": "echo [red]"}),
                     ToolResultEvent("x", "Bash", "[red]plain[/red]\n" * 80, True, 0.2),
                     TurnComplete(1), StreamText("后文"), LoopComplete(2)])
        await app._send_message("go")
        assert [m._markdown for m in app.query(Markdown)] == ["前文", "后文"]
        block = app.query_one(ToolCallBlock)
        block.focus()
        await pilot.press("enter")
        assert not block._collapsed
        assert "[red]plain[/red]" in str(block.content)
        assert "截断" in str(block.content)


@pytest.mark.asyncio
async def test_scroll_reader_position_and_return_latest(tmp_path):
    app = OfflineApp(str(tmp_path))
    # Compare reading coordinates with decorative scroll animations disabled.
    # Key delivery and native anchor release still run through real controls.
    app.animation_level = "none"
    async with app.run_test(size=(80, 24)) as pilot:
        chat = app.query_one(ChatScroll)
        await chat.mount(assistant_row("history\n\n" * 80))
        await pilot.pause()
        # Establish the reading test's starting position explicitly: the first
        # follow refresh is scheduled and need not have fired on a fast machine.
        await pilot.press("ctrl+end")
        await pilot.wait_for_scheduled_animations()
        assert chat.is_vertical_scroll_end
        chat.focus()
        await pilot.press("pageup")
        await pilot.wait_for_scheduled_animations()
        before = chat.scroll_y
        assert before < chat.max_scroll_y
        await chat.mount(assistant_row("new answer\n\n" * 20))
        app._follow_chat()
        await pilot.pause()
        assert chat.scroll_y == before
        assert app.query_one("#latest-link").display
        await pilot.press("ctrl+end")
        await pilot.pause()
        assert chat.is_vertical_scroll_end


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["escape", "ctrl+c"])
async def test_cancel_waits_for_teardown_preserves_tail_and_draft(tmp_path, key):
    app = OfflineApp(str(tmp_path))
    async with app.run_test() as pilot:
        started, cleanup, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def run(_):
            yield StreamText("保留尾部")
            yield ToolUseEvent("Bash", "t", {"command": "long command"})
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await finish.wait()

        app.agent.run = run
        task = asyncio.create_task(app._send_message("go"))
        await asyncio.wait_for(started.wait(), 5)
        app.query_one(ChatInput).load_text("尚未发送的草稿")
        await pilot.press(key)
        await asyncio.wait_for(cleanup.wait(), 5)
        assert status(app).startswith("取消中") and app._streaming
        finish.set()
        await task
        assert status(app) == "已停止" and not app._streaming
        assert app.query_one(ChatInput).text == "尚未发送的草稿"
        assert [m._markdown for m in app.query(Markdown)] == ["保留尾部"]
        await pilot.press("ctrl+o")
        assert "已停止" in str(app.query_one(ToolCallBlock).content)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ErrorEvent("stream failed"), RuntimeError("stream failed")])
async def test_error_keeps_text_and_draft(tmp_path, failure):
    app = OfflineApp(str(tmp_path))
    async with app.run_test():
        script(app, [StreamText("partial text"), failure])
        app.query_one(ChatInput).load_text("draft")
        await app._send_message("go")
        assert app.query_one(ChatInput).text == "draft"
        assert app.query_one(Markdown)._markdown == "partial text"
        assert status(app) == "出错"


@pytest.mark.asyncio
async def test_permission_escape_precedes_cancel_and_never_executes(tmp_path):
    app = OfflineApp(str(tmp_path))
    async with app.run_test(size=(60, 18)) as pilot:
        executed = []
        future = asyncio.get_running_loop().create_future()

        async def run(_):
            yield PermissionRequest("Bash", "echo approval required", future)
            if normalize_reply(await future).response != PermissionResponse.DENY:
                executed.append(True)
            yield LoopComplete(1)

        app.agent.run = run
        task = asyncio.create_task(app._send_message("go"))
        try:
            for _ in range(100):
                await pilot.pause(0.01)
                if app.query("#perm-inline"):
                    break
            # Focus may schedule an animated scroll after the widget is mounted.
            # CPU idle alone does not mean its final screen coordinates are ready.
            await pilot.wait_for_scheduled_animations()
            assert app.focused.id == "perm-inline"
            assert app.query_one("#perm-inline").region in app.query_one("#chat-area").content_region
            assert "等待权限" in status(app)
            await pilot.press("escape")
            await task
            assert normalize_reply(future.result()).response == PermissionResponse.DENY
            assert not executed and status(app) == "本次运行结束"
            assert app.focused is app.query_one(ChatInput)
        finally:
            # Finish the run while its controls still exist, even on assertion failure.
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_completion_escape_does_not_cancel_run(tmp_path):
    app = OfflineApp(str(tmp_path))
    async with app.run_test() as pilot:
        gate = asyncio.Event()
        script(app, [gate, LoopComplete(1)])
        task = asyncio.create_task(app._send_message("go"))
        await pilot.pause()
        app.query_one(CompletionPopup).show(["/help"])
        await pilot.press("escape")
        assert app._streaming and not task.done()
        gate.set()
        await task


@pytest.mark.asyncio
async def test_history_tools_report_unknown_duration_and_missing_result(tmp_path):
    app = OfflineApp(str(tmp_path))
    async with app.run_test():
        await app._render_restored_messages([
            Message("assistant", "", tool_uses=[ToolUseBlock("a", "Bash", {"command": "echo"}),
                                                ToolUseBlock("b", "ReadFile", {"file_path": "missing.txt"})]),
            Message("user", "", tool_results=[ToolResultBlock("a", "saved output", True)]),
        ])
        blocks = list(app.query(ToolCallBlock))
        assert "耗时未知" in str(blocks[0].content) and "0.0s" not in str(blocks[0].content)
        assert "✗" in str(blocks[0].content)
        assert "结果未知" in str(blocks[1].content)
        blocks[0].action_expand_details()
        assert "saved output" in str(blocks[0].content)


@pytest.mark.asyncio
async def test_cancel_during_mcp_setup_cleans_streaming(tmp_path):
    app = OfflineApp(str(tmp_path))
    async with app.run_test() as pilot:
        connection = asyncio.create_task(asyncio.Event().wait())
        app._mcp_init_task = connection
        task = asyncio.create_task(app._send_message("go"))
        await pilot.pause()
        await pilot.press("escape")
        await task
        assert status(app) == "已停止" and not app._streaming
        # Connection setup keeps its existing cancellation semantics.
        await asyncio.gather(connection, return_exceptions=True)


@pytest.mark.asyncio
async def test_mcp_cancel_leaves_accurate_status_and_manager_for_shutdown(tmp_path, monkeypatch):
    started = asyncio.Event()

    class Manager:
        def load_configs(self, _):
            pass

        async def register_all_tools(self, _):
            started.set()
            await asyncio.Event().wait()

    manager = Manager()
    monkeypatch.setattr("mewcode.app.MCPManager", lambda: manager)
    app = OfflineApp(str(tmp_path))
    async with app.run_test():
        task = asyncio.create_task(app._init_mcp())
        await started.wait()
        assert app._mcp_connecting
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not app._mcp_connecting
        assert "连接已停止" in app._mcp_server_info
        assert app.mcp_manager is manager


@pytest.mark.asyncio
async def test_cancel_during_recall_cancels_side_query(tmp_path):
    app = OfflineApp(str(tmp_path))
    async with app.run_test():
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def recall(_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        app._prefetch_relevant_memories = recall
        task = asyncio.create_task(app._send_message("go"))
        await started.wait()
        app.action_cancel()
        await task
        assert cancelled.is_set() and status(app) == "已停止"


@pytest.mark.asyncio
async def test_askuser_and_plan_focus_keep_existing_actions(tmp_path):
    from mewcode.plan_dialog import InlinePlanWidget
    from mewcode.tools.ask_user import AskUserEvent

    app = OfflineApp(str(tmp_path))
    async with app.run_test(size=(60, 18)) as pilot:
        future = asyncio.get_running_loop().create_future()
        await app._handle_askuser(AskUserEvent(questions=[{
            "name": "color", "message": "选择颜色", "options": ["red", "blue"],
        }], future=future))
        await pilot.pause()
        assert app.focused.id == "askuser-inline"
        await pilot.press("escape")
        assert future.result() == {}
        assert app.focused is app.query_one(ChatInput)

        # Apply the real feedback action, capturing the next request without running a model.
        feedback = []
        app.agent._get_plan_path = lambda: tmp_path / "absent-plan.md"
        app.send_user_message = lambda text: feedback.append(text)
        await app._show_plan_approval()
        await pilot.pause()
        widget = app.query_one(InlinePlanWidget)
        assert app.focused is widget
        await pilot.press("down", "down", "x", "shift+tab")
        assert feedback == ["x"]
