"""假 LLM（tests/helpers/fake_llm.py）与真实客户端的对接测试。

假模型只有在被**我们自己的协议客户端**正确解析时才有价值——否则真机测试
会出现"假绿"（端到端跑通的是假象）。这里用真实的 OpenAICompatClient 驱动它，
断言流式事件序列（工具调用 → 文本 → 用量）与真机预期一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers.fake_llm import DEFAULT_FINAL_TEXT, FakeLLM  # noqa: E402

from mewcode.client import (  # noqa: E402
    StreamEnd,
    TextDelta,
    ToolCallComplete,
    ToolCallStart,
    create_client,
)
from mewcode.config import ProviderConfig  # noqa: E402
from mewcode.conversation import ConversationManager  # noqa: E402


@pytest.fixture
def llm():
    server = FakeLLM(script=[
        {"tool_calls": [("mcp_logs_query_logs", '{"query": "{app=\\"checkout\\"}"}')]},
        {"text": DEFAULT_FINAL_TEXT},
    ]).start()
    try:
        yield server
    finally:
        server.stop()


def _provider(llm: FakeLLM) -> ProviderConfig:
    return ProviderConfig(
        name="fake", protocol="openai-compat", base_url=llm.base_url,
        model="fake-model", api_key="test-key",
    )


async def _collect(client, conversation, tools=None):
    return [event async for event in client.stream(conversation, system="sys", tools=tools)]


class TestFakeLLMProtocolShape:
    @pytest.mark.asyncio
    async def test_tool_call_then_final_text(self, llm: FakeLLM) -> None:
        client = create_client(_provider(llm))
        conv = ConversationManager()

        first = await _collect(client, conv, tools=[{
            "name": "mcp_logs_query_logs", "description": "query logs",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }])
        starts = [e for e in first if isinstance(e, ToolCallStart)]
        completes = [e for e in first if isinstance(e, ToolCallComplete)]
        ends = [e for e in first if isinstance(e, StreamEnd)]

        assert len(starts) == 1 and starts[0].tool_name == "mcp_logs_query_logs"
        assert len(completes) == 1
        assert completes[0].arguments == {"query": '{app="checkout"}'}
        # 客户端把工具轮也记为 end_turn（agent 依据 ToolCallComplete 事件判断工具轮），
        # 这里断言的是用量与事件序列，不是 stop_reason 的命名
        assert ends and ends[0].input_tokens == 120 and ends[0].output_tokens == 20

        # 工具结果回到对话后，第二轮应给出最终文本
        second = await _collect(client, conv)
        text = "".join(e.text for e in second if isinstance(e, TextDelta))
        assert "ROOT CAUSE" in text and text == DEFAULT_FINAL_TEXT
        assert [e for e in second if isinstance(e, StreamEnd)][0].stop_reason == "end_turn"
    @pytest.mark.asyncio
    async def test_request_body_carries_tools_and_history(self, llm: FakeLLM) -> None:
        client = create_client(_provider(llm))
        conv = ConversationManager()
        await _collect(client, conv, tools=[{
            "name": "mcp_logs_query_logs", "description": "query logs",
            "input_schema": {"type": "object", "properties": {}},
        }])
        body = llm.posted_bodies()[0]
        assert body["model"] == "fake-model"
        assert body["tools"][0]["function"]["name"] == "mcp_logs_query_logs"
        assert body["messages"][0]["role"] == "system"

    def test_unsupported_paths_are_404(self, llm: FakeLLM) -> None:
        import httpx

        with httpx.Client(timeout=10) as http:
            assert http.get(f"{llm.base_url}/models").status_code == 404
            assert http.post(f"{llm.base_url}/responses", json={}).status_code == 404
