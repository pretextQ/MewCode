"""Owned native pane and worker status, with bounded confirmed shutdown."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from mewcode.teams.mailbox import Mailbox, create_message


@dataclass
class PaneHandle:
    pane_id: str
    backend: str
    state_file: Path
    owner_pid: str = ""

    @property
    def status(self) -> str:
        try:
            return str(json.loads(self.state_file.read_text(encoding="utf-8"))["state"])
        except (OSError, ValueError, KeyError):
            return "starting"

    def exists(self, live_only: bool = False) -> bool:
        if self.backend == "tmux":
            from mewcode.teams.spawn_tmux import pane_exists
            return pane_exists(self.pane_id, live_only=live_only, owner_pid=self.owner_pid)
        else:
            from mewcode.teams.spawn_iterm2 import pane_exists as iterm_pane_exists
        return iterm_pane_exists(self.pane_id, live_only=live_only)

    @property
    def online(self) -> bool:
        return self.status in ("ready", "running", "listening") and self.exists(live_only=True)

    async def stop(self, mailbox: Mailbox, agent_id: str, lead_id: str, timeout: float = 3) -> bool:
        if not self.exists():
            return True
        mailbox.write(agent_id, create_message(lead_id, agent_id, "Owner shutting down worker",
                                               message_type="shutdown_request"))
        deadline = asyncio.get_running_loop().time() + timeout
        while self.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if self.exists():
            if self.backend == "tmux":
                from mewcode.teams.spawn_tmux import kill_pane
            else:
                from mewcode.teams.spawn_iterm2 import kill_pane
            kill_pane(self.pane_id)
        deadline = asyncio.get_running_loop().time() + 3
        while self.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        return not self.exists()
