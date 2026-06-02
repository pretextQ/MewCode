from __future__ import annotations

from textual.containers import Vertical
from textual.message import Message as TMessage
from textual.widgets import OptionList
from textual.widgets.option_list import Option


class CompletionPopup(Vertical):

    DEFAULT_CSS = """
    CompletionPopup {
        dock: bottom;
        height: auto;
        max-height: 12;
        display: none;
        layer: overlay;
    }
    CompletionPopup OptionList {
        height: auto;
        max-height: 12;
        background: $surface;
        border: tall $accent;
    }
    """


    class Selected(TMessage):


        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value


    def compose(self):
        yield OptionList(id="completion-list")

    def show(self, items: list[str]) -> None:
        ol = self.query_one("#completion-list", OptionList)
        ol.clear_options()
        for item in items:
            ol.add_option(Option(item, id=item))
        self.display = True
        ol.focus()


    def hide(self) -> None:
        self.display = False

    @property
    def is_visible(self) -> bool:
        return self.display

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.post_message(self.Selected(event.option.prompt))
        self.hide()
