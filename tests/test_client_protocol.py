"""client 协议层行为测试（F2.2 thinking 预算、F2.7 截断信号）。"""
from __future__ import annotations

import pytest

from mewcode.config import ProviderConfig


def _anthropic_provider(**overrides) -> ProviderConfig:
    defaults = dict(
        name="test",
        protocol="anthropic",
        base_url="http://localhost:8080",
        model="claude-sonnet-4-20250514",
        api_key="unit-test-key-not-real",
        thinking=True,
    )
    defaults.update(overrides)
    return ProviderConfig(**defaults)


# ---------------------------------------------------------------------------
# F2.2 thinking 预算
# ---------------------------------------------------------------------------

class TestThinkingBudget:
    def test_budget_leaves_visible_output_room(self):
        """budget_tokens + 8192 <= max_tokens，且 >= 1024 下限。"""
        from mewcode.client import AnthropicClient

        client = AnthropicClient(_anthropic_provider())
        budget = client._thinking_budget()
        assert budget is not None
        assert budget >= 1024
        assert budget + 8192 <= client.max_output_tokens, (
            f"thinking budget {budget} must leave visible-output room in "
            f"max_tokens {client.max_output_tokens}"
        )

    def test_small_max_tokens_respects_lower_bound(self):
        """max_output_tokens 很小时预算退化为 1024 下限且不越界。"""
        from mewcode.client import AnthropicClient

        provider = _anthropic_provider(max_output_tokens=2048)
        client = AnthropicClient(provider)
        budget = client._thinking_budget()
        assert budget >= 1024
        assert budget < client.max_output_tokens

    def test_thinking_disabled_returns_none(self):
        from mewcode.client import AnthropicClient

        client = AnthropicClient(_anthropic_provider(thinking=False))
        assert client._thinking_budget() is None

    def test_adaptive_models_use_same_formula(self):
        """不依赖未公开的 0 预算自适应约定，统一走显式预算公式。"""
        from mewcode.client import AnthropicClient

        provider = _anthropic_provider(model="claude-opus-4-6-20260101")
        client = AnthropicClient(provider)
        budget = client._thinking_budget()
        assert budget is not None and budget >= 1024


# ---------------------------------------------------------------------------
# F2.7 openai 系协议：输出上限与截断信号
# ---------------------------------------------------------------------------

from types import SimpleNamespace


class _StubCompletions:
    def __init__(self, chunks: list) -> None:
        self._chunks = chunks
        self.kwargs: dict = {}

    async def create(self, **kwargs):
        self.kwargs = kwargs

        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()


def _chunk(choices=None, usage=None):
    return SimpleNamespace(choices=choices or [], usage=usage)


def _choice(delta=None, finish_reason=None):
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def _delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _compat_client(chunks: list):
    from mewcode.client import OpenAICompatClient

    client = OpenAICompatClient(_anthropic_provider(
        protocol="openai", model="deepseek-v4-flash",
    ))
    stub = _StubCompletions(chunks)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    return client, stub


class TestChatCompletionsSignals:
    @pytest.mark.asyncio
    async def test_sends_max_output_limit(self):
        client, stub = _compat_client([])
        conv = _conversation("hi")
        async for _ in client.stream(conv):
            pass
        assert stub.kwargs.get("max_tokens") == client.max_output_tokens

    @pytest.mark.asyncio
    async def test_length_finish_reason_maps_to_max_tokens(self):
        chunks = [
            _chunk([_choice(delta=_delta(content="partial"))]),
            _chunk([_choice(finish_reason="length")]),
            _chunk(usage=SimpleNamespace(
                prompt_tokens=100, completion_tokens=50,
                prompt_tokens_details=SimpleNamespace(cached_tokens=20),
            )),
        ]
        client, _ = _compat_client(chunks)
        conv = _conversation("hi")

        ends = []
        async for e in client.stream(conv):
            if getattr(e, "stop_reason", None):
                ends.append(e)

        assert len(ends) == 1
        assert ends[0].stop_reason == "max_tokens"
        assert ends[0].output_tokens == 50
        assert ends[0].input_tokens == 80  # 100 - 20 cached

    @pytest.mark.asyncio
    async def test_stream_end_emitted_without_usage_chunk(self):
        """provider 不发 usage chunk 时也必须有 StreamEnd。"""
        chunks = [
            _chunk([_choice(delta=_delta(content="hello"))]),
            _chunk([_choice(finish_reason="stop")]),
        ]
        client, _ = _compat_client(chunks)
        conv = _conversation("hi")

        ends = []
        async for e in client.stream(conv):
            if getattr(e, "stop_reason", None):
                ends.append(e)

        assert len(ends) == 1
        assert ends[0].stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_stream_end_emitted_without_any_finish_reason(self):
        chunks = [_chunk([_choice(delta=_delta(content="orphan"))])]
        client, _ = _compat_client(chunks)
        conv = _conversation("hi")

        ends = []
        async for e in client.stream(conv):
            if getattr(e, "stop_reason", None):
                ends.append(e)

        assert len(ends) == 1
        assert ends[0].stop_reason == "end_turn"


class TestResponsesSignals:
    @pytest.mark.asyncio
    async def test_sends_max_output_tokens(self):
        from mewcode.client import OpenAIClient

        client = OpenAIClient(_anthropic_provider(protocol="openai"))
        stub = _StubCompletions([])
        client._client = SimpleNamespace(responses=SimpleNamespace(create=stub.create))
        conv = _conversation("hi")
        async for _ in client.stream(conv):
            pass
        assert stub.kwargs.get("max_output_tokens") == client.max_output_tokens

    @pytest.mark.asyncio
    async def test_incomplete_maps_to_max_tokens(self):
        from mewcode.client import OpenAIClient

        incomplete_resp = SimpleNamespace(
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            usage=None,
        )
        chunks = [SimpleNamespace(type="response.completed", response=incomplete_resp)]
        client = OpenAIClient(_anthropic_provider(protocol="openai"))
        stub = _StubCompletions(chunks)
        client._client = SimpleNamespace(responses=SimpleNamespace(create=stub.create))
        conv = _conversation("hi")

        ends = []
        async for e in client.stream(conv):
            if getattr(e, "stop_reason", None):
                ends.append(e)

        assert len(ends) == 1
        assert ends[0].stop_reason == "max_tokens"


def _conversation(text: str):
    from mewcode.conversation import ConversationManager

    conv = ConversationManager()
    conv.add_user_message(text)
    return conv
