"""Application chrome for the conversation/workbench layout and dark terminal palette."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from rich.text import Text
from textual.theme import Theme

MEWCODE_THEME = Theme(
    name="mewcode", primary="#b4a1ff", secondary="#9aa9c5", accent="#b4a1ff",
    foreground="#e4e8f0", background="#0e1015", surface="#181c25", panel="#151922",
    boost="#272138", success="#72cf9c", warning="#e5bf73", error="#ff8ca2", dark=True,
    variables={
        "text": "#e4e8f0", "text-muted": "#a3afc4", "frame-border": "#343c4e",
        "user-surface": "#252137", "composer-surface": "#181d27",
        "input-cursor-background": "#b4a1ff", "input-cursor-foreground": "#0e1015",
    },
)


def brand_title() -> Text:
    try:
        release = "v" + version("mewcode")
    except PackageNotFoundError:
        release = "development"
    title = Text("/\\· ·/\\  ", style=MEWCODE_THEME.primary)
    title.append(f"MewCode  {release}", style=MEWCODE_THEME.foreground or "#e4e8f0")
    return title


def workspace_title(work_dir: str, branch: str, width: int = 0) -> Text:
    path = Text(f"▱ {work_dir}")
    suffix = Text(f"   ⎇ {branch}")
    if width > 0:
        suffix.truncate(max(1, width // 3), overflow="ellipsis")
        path.truncate(max(1, width - suffix.cell_len), overflow="ellipsis")
    return path + suffix
