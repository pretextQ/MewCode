"""F3.12: fire-and-forget 任务引用保全与 hooks owner 隔离。

回归场景（审查报告）：
- ``agent.py`` / ``hooks/engine.py`` / ``app.py`` 的裸 ``asyncio.ensure_future``
  不保存引用——CPython 事件循环只持弱引用，任务可能被 GC 中途回收。
- HookEngine 被父代理与所有子代理共享，通知队列 / prompt 输出互相污染。
- ``conditions.py`` 按枚举顺序找运算符，``args.text ~= a==b`` 会被错拆。
"""
from __future__ import annotations

import asyncio
import gc
from typing import Any

import pytest

from mewcode.hooks import Action, Hook, HookContext, HookEngine, parse_condition
from mewcode.hooks.engine import OwnedHookEngine

# ---------------------------------------------------------------------------
# HookEngine.spawn：引用保全
# ---------------------------------------------------------------------------

async def _wait_bg_cleared(engine: HookEngine, timeout: float = 2.0) -> None:
    """done callback（discard）由 call_soon 调度，等事件循环再转几圈。"""
    for _ in range(int(timeout / 0.01)):
        if not engine._bg_tasks:
            return
        await asyncio.sleep(0.01)
    assert not engine._bg_tasks


@pytest.mark.asyncio
async def test_spawn_keeps_strong_reference_until_done() -> None:
    engine = HookEngine()
    finished = asyncio.Event()

    async def work() -> None:
        await asyncio.sleep(0.02)
        finished.set()

    task = engine.spawn(work())
    # 丢弃本地引用后立刻 gc：事件循环只持弱引用，没有 engine 的强引用
    # 保存任务就可能被回收。
    del task
    gc.collect()
    await asyncio.wait_for(finished.wait(), timeout=2.0)
    await _wait_bg_cleared(engine)

    assert engine._bg_tasks == set()


@pytest.mark.asyncio
async def test_spawn_survives_without_external_reference() -> None:
    # 与上一测试等价的"无局部变量"形态：协程创建后立即交给 spawn，
    # 调用方不持有 Task。
    engine = HookEngine()
    engine.spawn(_slow_set(_done_flag := asyncio.Event()))

    gc.collect()
    await asyncio.wait_for(_done_flag.wait(), timeout=2.0)
    await _wait_bg_cleared(engine)
    assert engine._bg_tasks == set()


async def _slow_set(flag: asyncio.Event) -> None:
    await asyncio.sleep(0.02)
    flag.set()


@pytest.mark.asyncio
async def test_cancel_background_cancels_running_tasks() -> None:
    engine = HookEngine()
    cancelled = asyncio.Event()

    async def forever() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    engine.spawn(forever())
    await asyncio.sleep(0)  # 让任务先启动、挂到 sleep 上
    assert len(engine._bg_tasks) == 1

    await engine.cancel_background()

    assert cancelled.is_set()
    await _wait_bg_cleared(engine)
    assert engine._bg_tasks == set()


# ---------------------------------------------------------------------------
# owner 隔离：通知与 prompt 输出分桶
# ---------------------------------------------------------------------------

def _echo_hook(hook_id: str) -> Hook:
    return Hook(
        id=hook_id,
        event="post_tool_use",
        action=Action(type="command", command="echo hi"),
    )


def _prompt_hook(hook_id: str, message: str) -> Hook:
    return Hook(
        id=hook_id,
        event="pre_send",
        action=Action(type="prompt", message=message),
    )


@pytest.mark.asyncio
async def test_subagent_notifications_do_not_leak_across_owners() -> None:
    engine = HookEngine([_echo_hook("h1")])
    parent = engine.for_owner("parent-agent")
    child_a = engine.for_owner("child-a")
    child_b = engine.for_owner("child-b")

    ctx = HookContext(event_name="post_tool_use")
    await parent.run_hooks("post_tool_use", ctx)
    await child_a.run_hooks("post_tool_use", ctx)
    await child_b.run_hooks("post_tool_use", ctx)

    drained_a = child_a.drain_notifications()
    drained_b = child_b.drain_notifications()
    drained_parent = parent.drain_notifications()

    assert [n.hook_id for n in drained_a] == ["h1"]
    assert [n.hook_id for n in drained_b] == ["h1"]
    assert [n.hook_id for n in drained_parent] == ["h1"]
    # drain 后各自清空，互不影响
    assert child_a.drain_notifications() == []
    assert child_b.drain_notifications() == []
    assert parent.drain_notifications() == []


@pytest.mark.asyncio
async def test_parent_drain_does_not_consume_child_notifications() -> None:
    engine = HookEngine([_echo_hook("h-child")])
    child = engine.for_owner("child-a")
    parent = engine.for_owner("parent")

    await child.run_hooks("post_tool_use", HookContext(event_name="post_tool_use"))

    # 父代理 drain 取不到子代理的通知（修复前共享列表会被父 drain 消费）。
    assert parent.drain_notifications() == []
    assert [n.hook_id for n in child.drain_notifications()] == ["h-child"]


@pytest.mark.asyncio
async def test_prompt_messages_isolated_per_owner() -> None:
    engine = HookEngine(
        [
            _prompt_hook("p1", "from-parent"),
        ]
    )
    parent = engine.for_owner("parent")
    child = engine.for_owner("child")

    ctx = HookContext(event_name="pre_send")
    await parent.run_hooks("pre_send", ctx)

    assert parent.get_prompt_messages() == ["from-parent"]
    assert child.get_prompt_messages() == []
    assert parent.get_prompt_messages() == []


@pytest.mark.asyncio
async def test_default_owner_bucket_keeps_legacy_usage_working() -> None:
    # 不经 for_owner、不传 owner 的调用落在默认桶——旧调用方式语义不变。
    engine = HookEngine([_echo_hook("h1")])
    await engine.run_hooks("post_tool_use", HookContext(event_name="post_tool_use"))
    assert [n.hook_id for n in engine.drain_notifications()] == ["h1"]
    assert engine.drain_notifications() == []


@pytest.mark.asyncio
async def test_async_exec_hook_spawned_with_reference() -> None:
    engine = HookEngine([_echo_hook("h-async", )])
    engine.hooks[0].async_exec = True

    parent = engine.for_owner("owner")
    await parent.run_hooks("post_tool_use", HookContext(event_name="post_tool_use"))

    # spawn 的任务已入集合；等待其自然完成后被 discard。
    for _ in range(50):
        if not engine._bg_tasks:
            break
        await asyncio.sleep(0.01)
    assert engine._bg_tasks == set()
    assert [n.hook_id for n in parent.drain_notifications()] == ["h-async"]


def test_for_owner_returns_view_sharing_hooks() -> None:
    engine = HookEngine([_echo_hook("h1")])
    view = engine.for_owner("agent-1")
    assert isinstance(view, OwnedHookEngine)
    assert view.hooks is engine.hooks
    ctx = HookContext(event_name="post_tool_use")
    assert view.find_matching_hooks("post_tool_use", ctx) == [engine.hooks[0]]


# ---------------------------------------------------------------------------
# conditions：运算符取最早出现位置
# ---------------------------------------------------------------------------

def test_parse_tilde_operator_with_comparison_in_value() -> None:
    group = parse_condition('args.text ~= a==b')
    assert group is not None
    assert len(group.conditions) == 1
    cond = group.conditions[0]
    assert cond.field == "args.text"
    assert cond.operator == "~="
    assert cond.value == "a==b"


def test_parse_regex_operator_with_glob_char_in_value() -> None:
    group = parse_condition('args.path =~ /src/*/x==y/')
    assert group is not None
    cond = group.conditions[0]
    assert cond.operator == "=~"
    # 首尾 / 保留给 evaluate 的正则分支剥除
    assert cond.value == "/src/*/x==y/"


def test_parse_still_splits_on_real_operators() -> None:
    group = parse_condition('tool == "Bash" && args.command =~ /rm/')
    assert group is not None
    assert group.logic == "and"
    assert [c.operator for c in group.conditions] == ["==", "=~"]


# ---------------------------------------------------------------------------
# Agent 级：后台记忆提取保引用
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_memory_extraction_runs_in_background() -> None:
    from mewcode.agent import Agent
    from mewcode.tools import create_default_registry
    from tests.test_agent import MockLLMClient

    agent = Agent(MockLLMClient([]), create_default_registry(), "anthropic")

    extracted = asyncio.Event()

    async def fake_extract(conversation: Any) -> list[str]:
        await asyncio.sleep(0.02)
        extracted.set()
        return []

    agent._extract_memories = fake_extract  # type: ignore[method-assign]
    agent._spawn_background(agent._extract_memories(None))

    # 不保存 Task 引用，gc 后任务仍应完成（agent._bg_tasks 强引用）。
    gc.collect()
    await asyncio.wait_for(extracted.wait(), timeout=2.0)
    for _ in range(100):
        if not agent._bg_tasks:
            break
        await asyncio.sleep(0.01)
    assert agent._bg_tasks == set()


@pytest.mark.asyncio
async def test_agent_cancel_background_tasks() -> None:
    from mewcode.agent import Agent
    from mewcode.tools import create_default_registry
    from tests.test_agent import MockLLMClient

    agent = Agent(MockLLMClient([]), create_default_registry(), "anthropic")
    cancelled = asyncio.Event()

    async def forever() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    agent._spawn_background(forever())
    await asyncio.sleep(0)  # 让任务先启动、挂到 sleep 上
    await agent.cancel_background_tasks()

    assert cancelled.is_set()
    for _ in range(100):
        if not agent._bg_tasks:
            break
        await asyncio.sleep(0.01)
    assert agent._bg_tasks == set()
