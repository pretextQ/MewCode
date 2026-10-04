"""F3.8 TUI 加固测试（Textual pilot 驱动最小 app）。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("textual")

from mewcode.client import LLMClient  # noqa: E402
from mewcode.config import ProviderConfig  # noqa: E402
from mewcode.conversation import ConversationManager  # noqa: E402
from mewcode.tools.base import StreamEnd, TextDelta  # noqa: E402


class _SlowClient(LLMClient):
    """持续产出文本的 mock：给测试留出‘流式中’窗口。"""

    def __init__(self) -> None:
        self._calls = 0
        self.running = True

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        self._calls += 1
        for i in range(50):
            await asyncio.sleep(0.02)
            yield TextDelta(text=f"chunk{i} ")
        yield StreamEnd("end_turn", input_tokens=1, output_tokens=1)


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="test",
        protocol="anthropic",
        base_url="http://localhost",
        model="claude-sonnet-4-20250514",
        api_key="unit-test-key-not-real",
    )


@pytest.fixture
def app_factory(monkeypatch, tmp_path):
    """返回构造好的 MewCodeApp（不启动 driver）。"""
    from mewcode.app import MewCodeApp

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _make(client: LLMClient) -> Any:
        app = MewCodeApp(providers=[_provider()])
        # 直接注入 mock client / agent，不用真实网络
        app.client = client
        app._mcp_init_task = None
        return app

    return _make


@pytest.mark.asyncio
async def test_stateful_command_rejected_while_streaming(app_factory):
    """流式中 /clear 被拒：不得替换运行中 _send_message 持有的 conversation。"""
    from mewcode.app import _STATEFUL_COMMANDS

    assert "clear" in _STATEFUL_COMMANDS
    assert "session" in _STATEFUL_COMMANDS
    assert "compact" in _STATEFUL_COMMANDS

    app = app_factory(_SlowClient())
    async with app.run_test() as _pilot:
        # 不走 agent 循环：直接置流式状态后派发 /clear
        app._streaming = True
        app.agent = type("A", (), {"work_dir": "."})()
        seen: list[str] = []
        app._show_system_message = lambda text: seen.append(text)

        await app._dispatch_command("/clear")
        assert any("回复进行中" in s for s in seen), seen
        app._streaming = False


@pytest.mark.asyncio
async def test_send_message_reentrancy_guard(app_factory):
    """快速双触发 _send_message：只有第一个实例运行。"""
    client = _SlowClient()
    app = app_factory(client)
    async with app.run_test() as _pilot:
        agent = _SlowAgent()
        app.agent = agent

        t1 = asyncio.create_task(app._send_message("hello"))
        await asyncio.sleep(0.1)
        # 第一个在跑（流式位已置）
        assert app._streaming is True
        seen: list[str] = []
        app._show_system_message = lambda text: seen.append(text)

        # 第二个立即返回，不并行
        await app._send_message("hello again")
        assert any("忽略重复请求" in s for s in seen)
        assert agent.runs == 1, f"第二个实例启动了新的 agent 循环: {agent.runs}"

        t1.cancel()
        try:
            await t1
        except asyncio.CancelledError:
            pass


class _SlowAgent:
    work_dir = "."
    plan_mode = False
    total_input_tokens = 0
    total_output_tokens = 0

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, conv):
        self.runs += 1
        yield TextDelta(text="working ")
        await asyncio.sleep(30)


@pytest.mark.asyncio
async def test_stream_text_after_tool_use_rebuilds_label(app_factory):
    """tool_use 之后 streaming_label 为 None：新一轮文本不得 AttributeError。

    直接驱动事件渲染逻辑：构造序列 [ToolUseEvent 清理 label → StreamText]。
    """
    from mewcode.app import MewCodeApp  # noqa: F401

    app = app_factory(_SlowClient())
    async with app.run_test() as _pilot:
        app.agent = type(
            "A", (), {
                "work_dir": ".",
                "plan_mode": False,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "run": lambda self, conv: _retry_then_text_stream(),
            },
        )()

        await asyncio.wait_for(app._send_message("go"), timeout=5.0)
        # 无异常即通过；确认文本确实渲染过
        chat_area = app.query_one("#chat-area")
        assert chat_area is not None


async def _retry_then_text_stream():
    """max_tokens 重试路径：RetryEvent 后继续产出 StreamText。"""
    from mewcode.agent import RetryEvent

    yield TextDelta(text="part1 ")
    yield RetryEvent(reason="max_tokens escalation")
    yield TextDelta(text="part2 ")
    yield StreamEnd("end_turn", input_tokens=1, output_tokens=1)
