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
