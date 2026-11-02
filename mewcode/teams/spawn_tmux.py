from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class TmuxPaneInfo:
    pane_id: str
    session: str
    owner_pid: str = ""


class TmuxSpawnError(Exception):
    pass


def _run_tmux(*args: str) -> str:
    result = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise TmuxSpawnError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def build_cli_command(*args, **kwargs) -> str:
    import shlex

    from mewcode.teams.spawn_cli import build_worker_argv
    return "exec " + shlex.join(build_worker_argv(*args, **kwargs))


def spawn_tmux_teammate(
    team_name: str, teammate_name: str, worktree_path: str, prompt: str,
    agent_type: str = "", model: str = "", mailbox_dir: str = "", **worker_options,
) -> TmuxPaneInfo:
    command = build_cli_command(team_name, teammate_name, worktree_path, prompt,
                                agent_type, model, mailbox_dir, **worker_options)
    try:
        pane_id = _run_tmux("split-window", "-h", "-d", "-P", "-F", "#{pane_id} #{pane_pid}",
                            "-t", team_name, "-c", worktree_path, command)
    except TmuxSpawnError:
        try:
            pane_id = _run_tmux("new-window", "-d", "-t", team_name, "-n", teammate_name,
                                "-P", "-F", "#{pane_id} #{pane_pid}", "-c", worktree_path, command)
        except TmuxSpawnError:
            pane_id = _run_tmux("new-session", "-d", "-s", team_name, "-n", teammate_name,
                                "-P", "-F", "#{pane_id} #{pane_pid}", "-c", worktree_path, command)
    pane_id, owner_pid = pane_id.rsplit(" ", 1)
    return TmuxPaneInfo(pane_id=pane_id, session=team_name, owner_pid=owner_pid)


def pane_exists(pane_id: str, *, live_only: bool = False, owner_pid: str = "") -> bool:
    try:
        dead, pid = _run_tmux("display-message", "-p", "-t", pane_id, "#{pane_dead} #{pane_pid}").split()
        return (not owner_pid or pid == owner_pid) and (not live_only or dead == "0")
    except TmuxSpawnError:
        return False


def send_keys_to_pane(pane_id: str, keys: str = "") -> None:
    try:
        _run_tmux("send-keys", "-t", pane_id, keys, "Enter")
    except TmuxSpawnError:
        log.warning("Failed to send keys to tmux pane %s", pane_id)


def kill_pane(pane_id: str) -> None:
    try:
        _run_tmux("kill-pane", "-t", pane_id)
    except TmuxSpawnError:
        pass
