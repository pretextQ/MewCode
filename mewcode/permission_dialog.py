from __future__ import annotations

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, Static

from mewcode.permissions.replies import (
    MAX_FEEDBACK_CHARS,
    PermissionReply,
    PermissionResponse,
    clean_feedback,
    normalize_reply,
)

_PERM_OPTIONS = [
    ("Yes", PermissionResponse.ALLOW),
    ("Yes, and don't ask again for this pattern", PermissionResponse.ALLOW_ALWAYS),
    ("No", PermissionResponse.DENY),
]


class FeedbackInput(Input):
    def _on_paste(self, event: events.Paste) -> None:
        event.stop()
        event.prevent_default()
        start, end = sorted(self.selection)
        available = MAX_FEEDBACK_CHARS - len(self.value) + end - start
        self.insert_text_at_cursor(clean_feedback(event.text)[:max(0, available)])


class InlinePermissionWidget(Vertical, can_focus=True):
    """渲染在聊天区域内部的内联权限确认提示。

    与 Go 版 TUI 的权限对话框一致：工具名 + 描述 + 带编号的
    选项，支持方向键导航 + 回车确认。
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("escape", "deny", "Deny", priority=True),
    ]

    class Responded(Message):


        def __init__(self, response: PermissionResponse | PermissionReply, request_token: object | None = None) -> None:
            super().__init__()
            self.reply = normalize_reply(response)
            self.response = self.reply.response
            self.request_token = request_token

    def __init__(self, tool_name: str, description: str, request_token: object | None = None, **kwargs) -> None:
        super().__init__(id="perm-inline", **kwargs)
        self._tool_name = tool_name
        self._description = description
        self._cursor = 0
        self._responded = False
        self._request_token = request_token

    def compose(self) -> ComposeResult:
        yield Static(self._build_content(), id="perm-content")
        feedback = FeedbackInput(placeholder="可选拒绝反馈 · Enter 确认 · Esc 无反馈拒绝",
                                 max_length=MAX_FEEDBACK_CHARS, id="perm-feedback")
        feedback.display = False
        yield feedback


    def on_mount(self) -> None:
        self.focus()

    def _build_content(self) -> str:
        lines = []
        lines.append(f"  [bold yellow]{escape(self._tool_name)} command[/bold yellow]")
        lines.append(f"    {escape(self._description)}")
        lines.append("  [dim]This command requires approval · Esc 拒绝[/dim]")
        lines.append("  Do you want to proceed?")

        for i, (label, _resp) in enumerate(_PERM_OPTIONS):
            if i == self._cursor:
                lines.append(f" [bold cyan]❯[/bold cyan] {i + 1}. [bold]{label}[/bold]")
            else:
                lines.append(f"   {i + 1}. [dim]{label}[/dim]")

        return "\n".join(lines)


    def _refresh(self) -> None:
        content = self.query_one("#perm-content", Static)
        content.update(self._build_content())
        feedback = self.query_one("#perm-feedback", Input)
        feedback.display = self._cursor == 2
        if self._cursor == 2:
            feedback.focus()
        else:
            feedback.value = ""
            self.focus()
        self.call_after_refresh(self.scroll_visible, animate=False, top=False, immediate=True)

    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh()

    def action_cursor_down(self) -> None:
        if self._cursor < len(_PERM_OPTIONS) - 1:
            self._cursor += 1
            self._refresh()

    def action_select(self) -> None:
        _, response = _PERM_OPTIONS[self._cursor]
        feedback = self.query_one("#perm-feedback", Input).value if response == PermissionResponse.DENY else ""
        self._respond(PermissionReply(response, feedback))


    def action_deny(self) -> None:
        self._respond(PermissionReply(PermissionResponse.DENY))

    def _respond(self, reply: PermissionReply) -> None:
        if self._responded:
            return
        self._responded = True
        self.post_message(self.Responded(reply, self._request_token))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_select()
