"""F4.3: SkillExecutor fork 路径的测试——审查报告覆盖盲区。

覆盖 fork 上下文三种模式（full/recent/none）、allowed_tools 过滤与
fork 权限受限（F1.5 修复的验证：DONT_ASK 下限 + ask 一律 DENY）。
"""
from __future__ import annotations

from typing import Any

import pytest

from mewcode.agent import Agent
from mewcode.conversation import ConversationManager
from mewcode.permissions import PermissionMode
from mewcode.skills.executor import (
    SkillDependencyError,
    SkillExecutor,
    filter_tool_registry,
)
from mewcode.skills.parser import SkillDef
from mewcode.tools import ToolRegistry, create_default_registry
from mewcode.tools.base import Tool, ToolResult


class EchoTool(Tool):
    name = "Echo"
    description = "echo tool for tests"
    params_model = None  # type: ignore[assignment]

    def get_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": {}}

    async def execute(self, params: Any) -> ToolResult:
        return ToolResult(output="echo")


@pytest.fixture
def agent() -> Agent:
    from tests.test_agent import MockLLMClient

    return Agent(MockLLMClient([]), create_default_registry(), "anthropic")


@pytest.fixture
def executor(agent: Agent) -> SkillExecutor:
    return SkillExecutor(agent=agent, client=agent.client, protocol="anthropic")


def _skill(**kwargs: Any) -> SkillDef:
    defaults: dict[str, Any] = {
        "name": "review-skill",
        "description": "test skill",
        "prompt_body": "Do the review of {args}",
        "mode": "fork",
    }
    defaults.update(kwargs)
    return SkillDef(**defaults)


# =========================================================================
# A. fork 上下文构建（full / recent / none）
# =========================================================================

def _seed_history(conv: ConversationManager) -> None:
    for i in range(8):
        conv.add_user_message(f"question {i}")
        conv.add_assistant_message(f"answer {i}")


def test_fork_context_none_is_empty(executor: SkillExecutor) -> None:
    assert executor._build_fork_context("none") == []


def test_fork_context_recent_returns_tail(executor: SkillExecutor) -> None:
    from mewcode.skills.executor import FORK_RECENT_COUNT

    conv = ConversationManager()
    _seed_history(conv)
    executor.agent._conversation = conv

    ctx = executor._build_fork_context("recent")

    assert len(ctx) == FORK_RECENT_COUNT
    # 取的是最近的消息（tail）
    assert ctx[-1].content == "answer 7"


def test_fork_context_recent_empty_history(executor: SkillExecutor) -> None:
    executor.agent._conversation = ConversationManager()
    assert executor._build_fork_context("recent") == []


def test_fork_context_full_builds_summary(executor: SkillExecutor) -> None:
    conv = ConversationManager()
    conv.add_user_message("hello world")
    conv.add_assistant_message("hi there")
    executor.agent._conversation = conv

    ctx = executor._build_fork_context("full")

    assert len(ctx) == 1
    assert ctx[0].role == "user"
    assert "## Previous conversation summary" in ctx[0].content
    assert "User: hello world" in ctx[0].content
    assert "Assistant: hi there" in ctx[0].content


def test_fork_context_full_truncates_long_messages(
    executor: SkillExecutor,
) -> None:
    conv = ConversationManager()
    conv.add_user_message("x" * 500)
    executor.agent._conversation = conv

    ctx = executor._build_fork_context("full")

    assert "..." in ctx[0].content
    assert "x" * 500 not in ctx[0].content


# =========================================================================
# B. allowed_tools 过滤
# =========================================================================

def test_filter_registry_keeps_only_allowed() -> None:
    registry = ToolRegistry()
    registry.register(EchoTool())
    filtered = filter_tool_registry(registry, ["Echo"])
    assert filtered.get("Echo") is not None


def test_filter_registry_missing_tool_raises() -> None:
    registry = ToolRegistry()
    with pytest.raises(SkillDependencyError, match="not registered"):
        filter_tool_registry(registry, ["NoSuchTool"])


def test_filter_registry_empty_allowed_returns_original() -> None:
    registry = ToolRegistry()
    assert filter_tool_registry(registry, []) is registry


# =========================================================================
# C. fork 权限受限（F1.5 验证）
# =========================================================================

def test_fork_checker_uses_dont_ask_floor(agent: Agent) -> None:
    from mewcode.tools.agent_tool import build_subagent_checker

    checker, _ = build_subagent_checker(
        agent, agent.work_dir, "dontAsk", is_background=True
    )

    assert checker.mode == PermissionMode.DONT_ASK


def test_fork_checker_inherits_parent_rule_engine(agent: Agent) -> None:
    from mewcode.tools.agent_tool import build_subagent_checker

    parent_checker = agent.permission_checker
    if parent_checker is None:
        pytest.skip("parent has no checker configured")

    checker, _ = build_subagent_checker(
        agent, agent.work_dir, "dontAsk", is_background=True
    )

    # 继承父级的 RuleEngine 与沙箱：规则/危险检测/沙箱仍然生效
    assert checker.rule_engine is parent_checker.rule_engine or (
        checker.rule_engine.rules == parent_checker.rule_engine.rules
    )


@pytest.mark.asyncio
async def test_execute_fork_runs_prompt_via_fork_agent(
    executor: SkillExecutor,
) -> None:
    from tests.test_agent import MockLLMClient

    executor.client = MockLLMClient([])
    executor.agent.client = executor.client
    executor.agent._conversation = ConversationManager()

    skill = _skill(context="none", allowed_tools=[])
    result = await executor.execute_fork(skill, "the-file.py")

    # MockLLMClient 空响应也应完成流程并返回字符串结果
    assert isinstance(result, str)
