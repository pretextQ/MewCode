from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass


@dataclass
class ITermPaneInfo:
    session_id: str


class ITermSpawnError(Exception):
    pass


SPAWN_SCRIPT = """on run argv
  tell application "iTerm2"
    tell current session of current window
      set workerSession to split horizontally with default profile command (item 1 of argv)
    end tell
    return unique ID of workerSession
  end tell
end run"""

SESSION_SCRIPT = """on run argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if unique ID of s is (item 1 of argv) then
            if (item 2 of argv) is "close" then close s
            return "found"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "missing"
end run"""


def _run_script(script: str, *args: str) -> str:
    result = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ITermSpawnError("iTerm2 AppleScript operation failed")
    return result.stdout.strip()


def spawn_iterm2_teammate(*args, **kwargs) -> ITermPaneInfo:
    from mewcode.teams.spawn_tmux import build_cli_command
    command = "/bin/sh -c " + shlex.quote(build_cli_command(*args, **kwargs))
    return ITermPaneInfo(_run_script(SPAWN_SCRIPT, command))


def pane_exists(pane_id: str, *, live_only: bool = False) -> bool:
    return _run_script(SESSION_SCRIPT, pane_id, "find") == "found"


def kill_pane(pane_id: str) -> None:
    _run_script(SESSION_SCRIPT, pane_id, "close")
