"""Shared turn/listen/shutdown contract for local and independent workers."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING

from mewcode.teams.mailbox import MailboxMessage, create_message

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

    queued: list[MailboxMessage] = []

    async def turn(action: Callable[[], Awaitable[str]]) -> None:
        state("running")
        manager.update_worker_activity(team, agent_id, True)
        result = await action()
        manager.update_worker_activity(team, agent_id, bool(queued))
        completed(result)
        mailbox.write(team.lead_agent_id, create_message(
            name, team.lead_agent_id, f"[idle] {name}: completed task\n{result}", summary=f"{name} idle",
        ))
        state("running" if queued else "listening")

    running: asyncio.Task[None] | None = asyncio.create_task(turn(initial_run))
    try:
        while True:
            if running is not None and running.done():
                await running
                running = None
            if running is None and queued:
                prompt = "\n\n".join(f"[Message from {m.from_agent}] {m.content}" for m in queued)
                queued.clear()
                state("running")
                running = asyncio.create_task(turn(partial(run, prompt)))
            if running is not None and not running.done():
                done, _ = await asyncio.wait([running], timeout=1)
                if done:
                    continue
            else:
                await asyncio.sleep(1)
            messages = mailbox.consume(agent_id)
            shutdown = next((m for m in messages if m.message_type == "shutdown_request"), None)
            if shutdown is not None:
                if running is not None and not running.done():
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                mailbox.write(team.lead_agent_id, create_message(
                    name, team.lead_agent_id, f"{name} stopped", message_type="shutdown_response",
                    metadata={"request_id": shutdown.metadata.get("request_id", shutdown.id)},
                ))
                state("stopped")
                return
            queued.extend(m for m in messages if m.message_type == "text")
            if queued:
                state("running")
                manager.update_worker_activity(team, agent_id, True)
    finally:
        if running is not None:
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        manager.update_worker_activity(team, agent_id, False)
