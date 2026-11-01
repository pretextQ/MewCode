"""Shared live/history message construction and reader-controlled scrolling."""
from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Markdown, Static


def user_row(content: str) -> Vertical:
    return Vertical(Static(Text("› " + content), classes="message user-message"), classes="user-row")


def assistant_row(content: str) -> Vertical:
    return Vertical(
        Static("✦ MewCode", classes="assistant-label", markup=False),
        Markdown(content, classes="message ai-message"),
        classes="ai-row",
    )


class ChatScroll(VerticalScroll):
    BINDINGS = [Binding("ctrl+end", "latest", "返回最新")]

    def on_mount(self) -> None:
        self.anchor()
        self.set_interval(0.2, self._refresh_latest)

    def anchor(self, anchor: bool = True) -> None:
        # Native anchoring bottom-aligns even a short transcript. Keep short
        # conversations at the top as in the design, and anchor once overflowing.
        super().anchor(anchor and self.max_scroll_y > 0)

    def _refresh_latest(self) -> None:
        if self.max_scroll_y > 0 and not self.is_anchored:
            self.anchor()
        elif self.max_scroll_y == 0 and self.is_anchored:
            self.anchor(False)
        if self.app.query("#latest-link"):
            self.app.query_one("#latest-link").display = not self.is_vertical_scroll_end

    def action_latest(self) -> None:
        self.anchor()
