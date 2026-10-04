"""AskUserQuestion 全流程测试（F2.4）。"""
from __future__ import annotations

import asyncio

import pytest

from mewcode.tools.ask_user import AskUserParams, AskUserTool, QuestionItem


@pytest.mark.asyncio
async def test_prepare_then_execute_roundtrip():
    """UI 在 ToolUseEvent 阶段 prepare()，用户作答后 execute() 拿到答案。

    回归：await future 期间 agent 不产出事件，旧实现只在 ToolResultEvent
    分支检查 _pending_event（此时已被 finally 清空）→ 无人 resolve → 300s 超时。
    """
    tool = AskUserTool()
    event = tool.prepare([
        {"type": "radio", "name": "color", "message": "Pick one",
         "options": ["red", "blue"]},
    ])
    assert tool.current_question() is event

    async def resolve() -> None:
        await asyncio.sleep(0.01)
        event.future.set_result({"color": "red"})

    resolver = asyncio.create_task(resolve())
    result = await tool.execute(
        AskUserParams(questions=[
            QuestionItem(type="radio", name="color", message="Pick one",
                         options=["red", "blue"]),
        ])
    )
    await resolver

    assert not result.is_error
    assert "color: red" in result.output


@pytest.mark.asyncio
async def test_execute_without_prepare_times_out_fast_when_resolved_none():
    """无 prepare 兜底路径：事件内部创建后仍可被 resolve。"""
    tool = AskUserTool()

    async def resolve_later() -> None:
        # 等 execute 进入 wait_for 后从工具上取到事件并作答
        for _ in range(50):
            await asyncio.sleep(0.01)
            event = tool.current_question()
            if event is not None:
                event.future.set_result({"q1": "ans"})
                return

    resolver = asyncio.create_task(resolve_later())
    result = await tool.execute(
        AskUserParams(questions=[
            QuestionItem(type="text", name="q1", message="Say something"),
        ])
    )
    await resolver
    assert not result.is_error
    assert "q1: ans" in result.output


def test_dialog_answer_key_uses_question_name():
    """对话框 answers 的 key 必须是 q.name（旧实现落到 message 文本，
    工具侧按 name 取值 → 答案全部丢失变成 (no answer)）。"""
    from mewcode.askuser_dialog import InlineAskUserWidget

    q = {"type": "radio", "name": "color", "message": "Pick one", "options": ["red"]}
    assert InlineAskUserWidget._answers_key(q, 0) == "color"
    # 缺 name 时回退 message 而非静默错位
    q_noname = {"type": "text", "message": "Say something"}
    assert InlineAskUserWidget._answers_key(q_noname, 0) == "Say something"
