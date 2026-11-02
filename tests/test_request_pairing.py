"""Request-only repairs, including the real Agent cancellation/SDK boundary."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from mewcode.agent import Agent
from mewcode.client import LLMClient, create_client
from mewcode.config import ProviderConfig
from mewcode.conversation import ConversationManager, Message, ThinkingBlock, ToolResultBlock, ToolUseBlock
from mewcode.conversation_pairing import pair_request_messages
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, Tool, ToolCallComplete


@pytest.fixture(autouse=True)
def offline_environment(isolated_home, monkeypatch):
    monkeypatch.chdir(isolated_home)


class WaitParams(BaseModel):
    pass


class WaitTool(Tool[WaitParams]):
    name = "WaitProbe"
    description = "Offline blocking fixture"
    params_model = WaitParams
    category = "command"

    def __init__(self):
        self.started = asyncio.Event()

    async def execute(self, params):
        self.started.set()
        await asyncio.Event().wait()


class ScriptedClient(LLMClient):
    async def stream(self, conversation, system="", tools=None):
        yield ToolCallComplete("interrupted", "WaitProbe", {})
        yield StreamEnd("end_turn")


class Captured(Exception):
    pass


async def capture(protocol, conv):
    client = create_client(ProviderConfig(
        name="offline", protocol=protocol, model="offline", api_key="offline", base_url="http://127.0.0.1:1",
    ))
    sdk = client._client
    payload = {}

    def intercept(**kwargs):
        payload.update(kwargs)
        raise Captured

    async def intercept_async(**kwargs):
        intercept(**kwargs)

    endpoint = sdk.messages if protocol == "anthropic" else sdk.responses if protocol == "openai" else sdk.chat.completions
    try:
        with patch.object(endpoint, "stream" if protocol == "anthropic" else "create",
                          side_effect=intercept if protocol == "anthropic" else intercept_async):
            with pytest.raises(Captured):
                async for _ in client.stream(conv):
                    pass
        return payload
    finally:
        await client.close()


def assert_wire_pairs(protocol, payload):
    """Check adjacency, roles and IDs rather than balanced totals."""
    pending, seen = set(), set()
    for row in payload.get("messages", payload.get("input", [])):
        calls, results = [], []
        if protocol == "anthropic":
            blocks = row["content"] if isinstance(row["content"], list) else []
            calls = [b["id"] for b in blocks if b["type"] == "tool_use"]
            results = [b["tool_use_id"] for b in blocks if b["type"] == "tool_result"]
            if calls:
                assert row["role"] == "assistant"
            if results:
                assert row["role"] == "user"
        elif protocol == "openai":
            if row.get("type") == "function_call":
                calls = [row["call_id"]]
            elif row.get("type") == "function_call_output":
                results = [row["call_id"]]
        else:
            calls = [c["id"] for c in row.get("tool_calls", [])]
            if calls:
                assert row["role"] == "assistant"
            if row.get("role") == "tool":
                results = [row["tool_call_id"]]
        if not calls and not results:
            assert not pending
        for call in calls:
            assert call not in seen
            seen.add(call)
            pending.add(call)
        for result in results:
            assert result in pending
            pending.remove(result)
    assert not pending


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["anthropic", "openai", "openai-compat"])
async def test_cancelled_agent_next_request(protocol, tmp_path, monkeypatch, isolated_home):
    monkeypatch.chdir(tmp_path)
    registry = create_default_registry()
    blocker = WaitTool()
    registry.register(blocker)
    agent = Agent(ScriptedClient(), registry, "anthropic", work_dir=str(tmp_path))
    conv = ConversationManager()
    conv.add_user_message("wait")

    async def consume():
        async for _ in agent.run(conv):
            pass

    running = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(blocker.started.wait(), 5)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    conv.add_user_message("continue")
    conv.set_system_reminder("probe", "transient reminder")
    before = deepcopy(conv)
    payload = await capture(protocol, conv)
    assert_wire_pairs(protocol, payload)
    assert "side effects" in str(payload)
    assert "transient reminder" in str(payload)
    assert conv == before


def test_valid_history_is_stable_and_request_copy_is_independent():
    messages = [
        Message("user", "start"),
        Message("assistant", "read", tool_uses=[ToolUseBlock("a", "Read", {"path": "x"})],
                thinking_blocks=[ThinkingBlock("thinking", "signature")]),
        Message("user", "", tool_results=[ToolResultBlock("a", "real output")]),
        Message("assistant", "done"),
    ]
    normalized = pair_request_messages(messages)
    assert normalized == messages
    assert pair_request_messages(normalized) == normalized
    normalized[1].tool_uses[0].arguments["path"] = "changed"
    assert messages[1].tool_uses[0].arguments == {"path": "x"}


def test_malformed_roles_mixed_text_empty_ids_and_duplicate_results():
    messages = [
        Message("user", "", tool_uses=[ToolUseBlock("", "Read", {})]),
        Message("assistant", "keep mixed text", tool_results=[ToolResultBlock("", "real")]),
        Message("user", "", tool_results=[ToolResultBlock("", "duplicate fact")]),
        Message("user", ""),
    ]
    normalized = pair_request_messages(messages)
    assert normalized[0].role == "assistant"
    assert normalized[0].tool_uses[0].tool_use_id
    assert normalized[1].role == "user"
    assert normalized[1].tool_results[0].content == "real"
    assert "keep mixed text" in str(normalized)
    assert "duplicate fact" in str(normalized)
    assert all(m.content or m.tool_uses or m.tool_results or m.thinking_blocks for m in normalized)


def test_reused_id_in_later_turn_does_not_fill_prior_interruption():
    normalized = pair_request_messages([
        Message("assistant", "", tool_uses=[ToolUseBlock("a", "Write", {})]),
        Message("user", "next"),
        Message("assistant", "", tool_uses=[ToolUseBlock("a", "Read", {})]),
        Message("user", "", tool_results=[ToolResultBlock("a", "new turn result")]),
    ])
    assert normalized[1].tool_results[0].is_error
    assert "side effects" in normalized[1].tool_results[0].content
    assert normalized[-1].tool_results[0].content == "new turn result"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["anthropic", "openai", "openai-compat"])
async def test_late_partial_duplicate_and_orphan_results(protocol):
    conv = ConversationManager(history=[
        Message("user", "start"),
        Message("assistant", "", tool_uses=[ToolUseBlock("a", "Read", {}), ToolUseBlock("b", "Write", {})],
                thinking_blocks=[ThinkingBlock("signed thinking", "signature")]),
        Message("user", "", tool_results=[ToolResultBlock("a", "Permission denied: explicit deny", True)]),
        Message("user", "next question"),
        Message("user", "", tool_results=[ToolResultBlock("b", "late real result")]),
        Message("assistant", "", tool_uses=[ToolUseBlock("a", "Read", {}), ToolUseBlock("a", "Read", {})]),
        Message("user", "", tool_results=[ToolResultBlock("a", "second turn"), ToolResultBlock("a", "third result")]),
        Message("user", "", tool_results=[ToolResultBlock("orphan", "orphan fact")]),
        Message("assistant", ""),
    ])
    before = deepcopy(conv)
    payload = await capture(protocol, conv)
    assert_wire_pairs(protocol, payload)
    for text in ("Permission denied: explicit deny", "late real result", "orphan fact", "third result"):
        assert text in str(payload)
    assert "side effects" not in str(payload)
    if protocol == "anthropic":
        assert "signed thinking" in str(payload) and "signature" in str(payload)
    assert conv == before
