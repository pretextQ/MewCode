"""Shared turn/listen/shutdown contract for local and independent workers."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING

from mewcode.teams.mailbox import create_message

if TYPE_CHECKING:
    from mewcode.teams.manager import TeamManager


async def run_team_worker(
    manager: TeamManager,
    team_name: str,
    agent_id: str,
    name: str,
    initial_run: Callable[[], Awaitable[str]],
    run: Callable[[str], Awaitable[str]],
    completed: Callable[[str], None],
    state: Callable[[str], None],
) -> None:
    team = manager.get_team(team_name)
    mailbox = manager.get_mailbox(team_name)
    if team is None or mailbox is None:
        raise RuntimeError(f"Team '{team_name}' is unavailable")

    async def turn(action: Callable[[], Awaitable[str]]) -> None:
        state("running")
        team.set_member_active(agent_id, True)
        team.save()
        result = await action()
        team.set_member_active(agent_id, False)
        team.save()
        completed(result)
        state("listening")
        mailbox.write(team.lead_agent_id, create_message(
            name, team.lead_agent_id, f"[idle] {name}: completed task\n{result}", summary=f"{name} idle",
        ))

    try:
        await turn(initial_run)
        while True:
            await asyncio.sleep(1)
            messages = mailbox.consume(agent_id)
            shutdown = next((m for m in messages if m.message_type == "shutdown_request"), None)
            if shutdown is not None:
                mailbox.write(team.lead_agent_id, create_message(
                    name, team.lead_agent_id, f"{name} stopped", message_type="shutdown_response",
                    metadata={"request_id": shutdown.metadata.get("request_id", shutdown.id)},
                ))
                state("stopped")
                return
            text = [m for m in messages if m.message_type == "text"]
            if text:
                prompt = "\n\n".join(f"[Message from {m.from_agent}] {m.content}" for m in text)
                await turn(partial(run, prompt))
    finally:
        team.set_member_active(agent_id, False)
        # Deletion callers must await the worker before removing its resources.
        team.save()
