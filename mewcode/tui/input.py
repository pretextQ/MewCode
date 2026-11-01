from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.binding import Binding
from textual.message import Message as TMessage
from textual.widgets import TextArea

from mewcode.commands.completion import CompletionPopup


class ChatInput(TextArea):
    BINDINGS = [
        Binding("enter", "submit", "Submit", priority=True),
        Binding("shift+enter", "newline", "Newline", priority=True),
        Binding("ctrl+j", "newline", "Newline", priority=True),
        Binding("tab", "complete", "Complete", priority=True),
        Binding("escape", "dismiss_popup", "Dismiss", priority=True),
        Binding("up", "nav_up", "Navigate up", priority=True),
        Binding("down", "nav_down", "Navigate down", priority=True),
    ]

    class Submitted(TMessage):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class TabComplete(TMessage):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cursor_blink = False
        self._history: list[str] = []
        self._history_index: int = -1
        self._history_draft: str = ""
        self._history_file: Path | None = None

    def load_history(self, work_dir: str) -> None:
        self._history_file = Path(work_dir) / ".mewcode" / "history"
        if self._history_file.exists():
            try:
                lines = self._history_file.read_text(encoding="utf-8").splitlines()
                self._history = [ln for ln in lines if ln.strip()]
            except Exception:
                pass

    def _persist_entry(self, text: str) -> None:
        if self._history_file is None:
            return
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_file, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _popup(self) -> CompletionPopup | None:
        try:
            return self.app.query_one(CompletionPopup)
        except Exception:
            return None

    def action_submit(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            selected = popup.get_selected()
            popup.hide()
            if selected:
                self._history.append(selected)
                self._persist_entry(selected)
                self._history_index = -1
                self._history_draft = ""
                self.post_message(self.Submitted(selected))
                self.clear()
                return
        text = self.text.strip()
        if text:
            self._history.append(text)
            self._persist_entry(text)
            self._history_index = -1
            self._history_draft = ""
            self.post_message(self.Submitted(text))
            self.clear()

    def action_newline(self) -> None:
        self.insert("\n")

    def action_complete(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            selected = popup.get_selected()
            if selected:
                popup.hide()
                self.clear()
                self.insert(selected + " ")
            return
        text = self.text.strip()
        if text.startswith("/"):
            self.post_message(self.TabComplete(text))
        else:
            self.insert("\t")

    def action_dismiss_popup(self) -> None:
        popup = self._popup()
        if popup is not None:
            popup.hide()

    def action_nav_up(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            popup.move_up()
            return
        if not self._history:
            return
        if self._history_index == -1:
            self._history_draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self.clear()
        self.insert(self._history[self._history_index])

    def action_nav_down(self) -> None:
        popup = self._popup()
        if popup is not None and popup.is_visible:
            popup.move_down()
            return
        if self._history_index == -1:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.clear()
            self.insert(self._history[self._history_index])
        else:
            self._history_index = -1
            self.clear()
            self.insert(self._history_draft)

    class AtFileRequest(TMessage):
        def __init__(self, prefix: str) -> None:
            super().__init__()
            self.prefix = prefix

    class SlashMenuUpdate(TMessage):
        def __init__(self, prefix: str | None) -> None:
            super().__init__()
            self.prefix = prefix

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/"):
            prefix = text[1:]
            if " " not in prefix and "\n" not in prefix:
                self.post_message(self.SlashMenuUpdate(prefix))
            else:
                self.post_message(self.SlashMenuUpdate(None))
        else:
            self.post_message(self.SlashMenuUpdate(None))

        at_idx = text.rfind("@")
        if at_idx < 0:
            return
        after = text[at_idx + 1:]
        if " " in after or "\n" in after:
            return
        if after:
            self.post_message(self.AtFileRequest(after))
