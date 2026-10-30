"""Delivery instrumentation shared by interactive/headless model requests."""
from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from mewcode.conversation import ConversationManager
from mewcode.tools.base import StreamEvent, ToolCallComplete

from .bus import AdvisoryBus

if TYPE_CHECKING:
    from mewcode.agent import Agent


async def supervised_stream(
    agent: Agent, conversation: ConversationManager, system: str, tools: list[dict],
) -> AsyncIterator[StreamEvent]:
    bus = agent.supervision
    if not bus.enabled:
        async for event in agent.client.stream(conversation, system=system, tools=tools):
            yield event
        return
    bus.settle()
    selected = bus.select()
    # Apply AFTER compaction/tool-result trimming, to the exact request clone.
    # No reminders leak into persistent history, fork snapshots or later runs.
    outgoing = copy.deepcopy(conversation)
    for channel in ("system", "reminder"):
        text = "".join(bus.render(bus.entries[key].advisory) for key in selected[channel])
        if text and channel == "system":
            system += text
        elif text:
            outgoing.set_system_reminder("supervision", text)
    request_id = bus.request_started()
    confirmed = False
    try:
        async for event in agent.client.stream(outgoing, system=system, tools=tools):
            if not confirmed:
                bus.confirm(request_id, selected["system"] + selected["reminder"])
                confirmed = True
            if isinstance(event, ToolCallComplete):
                bus.observe("tool_call", event.tool_name)
            yield event
    except BaseException as exc:
        bus._event("request_failed", request_id=request_id, confirmed=confirmed, error=type(exc).__name__)
        raise


@asynccontextmanager
async def supervision_run(bus: AdvisoryBus) -> AsyncGenerator[None, None]:
    if not bus.enabled:
        yield
        return
    bus.begin()
    outcome = "completed"
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        outcome = "failed"
        raise
    finally:
        if outcome == "completed":
            bus.settle()
        bus.close(outcome)
