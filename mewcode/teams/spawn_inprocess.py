from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mewcode.agent import Agent
    from mewcode.conversation import ConversationManager

log = logging.getLogger(__name__)


class InProcessTeammateHandle:
    def __init__(
        self,
        agent: Agent,
        task: asyncio.Task[str],
        name: str,
    ) -> None:
        self.agent = agent
        self.task = task
        self.name = name


    @property
    def done(self) -> bool:
        return self.task.done()

    @property
    def result(self) -> str | None:
        if self.task.done():
            try:
                return self.task.result()
            except (asyncio.CancelledError, Exception):
                return None
        return None


    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()


def spawn_inprocess_teammate(
    agent: Agent,
    prompt: str,
    name: str,
    conversation: ConversationManager | None = None,
) -> InProcessTeammateHandle:


    async def _run() -> str:
        if conversation is not None:
            return await agent.run_to_completion("", conversation)
        return await agent.run_to_completion(prompt)

    task = asyncio.create_task(_run(), name=f"teammate-{name}")
    log.info("Spawned in-process teammate %s", name)
    return InProcessTeammateHandle(agent=agent, task=task, name=name)
