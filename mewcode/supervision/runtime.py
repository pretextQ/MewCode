"""Delivery instrumentation shared by interactive/headless model requests."""
from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pydantic import BaseModel

from mewcode.conversation import ConversationManager
from mewcode.tools.base import StreamEvent, Tool, ToolCallComplete, ToolResult
from mewcode.tools.bash import Bash
from mewcode.tools.edit_file import EditFile
from mewcode.tools.write_file import WriteFile

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
    bus.intent_anchor.suggest()
    bus.self_verify.suggest(not agent.plan_mode and agent.registry.is_enabled("Bash")
                            and type(agent.registry.get("Bash")) is Bash)
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
                bus.observe("tool_call", agent.registry.resolve_tool_call(event).tool_name)
            yield event
    except BaseException as exc:
        bus._event("request_failed", request_id=request_id, confirmed=confirmed, error=type(exc).__name__)
        raise


async def execute_observed_tool(agent: Agent, tool: Tool, params: BaseModel) -> ToolResult:
    """Observe actual invocation, excluding denied/rejected/invalid tool calls."""
    if not agent.supervision.enabled or type(tool) not in (Bash, EditFile, WriteFile):
        return await tool.execute(params)
    detector = agent.supervision.self_verify
    arguments = params.model_dump()
    token = detector.start(tool, arguments)
    try:
        result = await tool.execute(params)
    except BaseException:
        detector.finish(tool, arguments, token, False)
        raise
    detector.finish(tool, arguments, token, not result.is_error)
    return result


@asynccontextmanager
async def supervision_run(bus: AdvisoryBus, task: str = "") -> AsyncGenerator[None, None]:
    if not bus.enabled:
        yield
        return
    bus.begin(task)
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
