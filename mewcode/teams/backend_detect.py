from __future__ import annotations

import os
import shutil

from mewcode.teams.models import BackendType


class BackendDetectionError(Exception):
    pass


def _in_tmux_session() -> bool:
    return bool(os.environ.get("TMUX"))


def _in_iterm2() -> bool:
    return os.environ.get("TERM_PROGRAM") == "iTerm.app"


def _it2_available() -> bool:
    return shutil.which("it2") is not None


def _tmux_installed() -> bool:
    return shutil.which("tmux") is not None


def detect_backend(
    teammate_mode: str = "",
    is_interactive: bool = True,
) -> BackendType:
    """Resolve the pane backend for Agent Teams.

    An explicit teammate_mode is validated strictly and raises
    BackendDetectionError when unavailable; without an explicit mode the
    environment is probed and the error surfaces to the caller (e.g.
    TeamCreate turns it into a tool error result).
    """
    if teammate_mode == "in-process" or not is_interactive:
        return BackendType.IN_PROCESS

    if teammate_mode == "tmux":
        if _in_tmux_session() or _tmux_installed():
            return BackendType.TMUX
        raise BackendDetectionError(
            "teammate_mode 'tmux' is configured but tmux is not available.\n"
            "Install tmux (e.g. brew install tmux) or set "
            "'teammate_mode: \"in-process\"' in config.yaml."
        )

    if teammate_mode == "iterm2":
        if _in_iterm2() and _it2_available():
            return BackendType.ITERM2
        raise BackendDetectionError(
            "teammate_mode 'iterm2' is configured but iTerm2 + it2 CLI is not available.\n"
            "Or set 'teammate_mode: \"in-process\"' in config.yaml to use in-process backend."
        )

    return detect_pane_backend(teammate_mode, is_interactive)


def detect_pane_backend(
    teammate_mode: str = "",
    is_interactive: bool = True,
) -> BackendType:
    """Detect pane backend when user explicitly requests tmux."""
    if teammate_mode == "in-process" or not is_interactive:
        return BackendType.IN_PROCESS

    if _in_tmux_session():
        return BackendType.TMUX

    if _in_iterm2() and _it2_available():
        return BackendType.ITERM2

    if _tmux_installed():
        return BackendType.TMUX

    raise BackendDetectionError(
        "No suitable terminal backend found for Agent Team.\n"
        "Install one of the following:\n"
        "  - tmux: brew install tmux\n"
        "  - iTerm2 + it2 CLI: https://iterm2.com/utilities/it2check\n"
        "Or set 'teammate_mode: \"in-process\"' in config.yaml to use in-process backend."
    )
