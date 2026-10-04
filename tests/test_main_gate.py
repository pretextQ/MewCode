"""F3.9 TaskManager 门控接口与 __main__ 后台任务等待。"""
from __future__ import annotations

import asyncio

import pytest


class _QuickAgent:
    team_name = ""
    agent_id = "a1"
    _team_manager = None
    total_input_tokens = 0
    total_output_tokens = 0

    async def run_to_completion(self, prompt, conversation=None):
        await asyncio.sleep(0.02)
        return "done"


class TestHasPendingWork:
    @pytest.mark.asyncio
    async def test_no_work_initially(self):
        from mewcode.agents.task_manager import TaskManager

        tm = TaskManager()
        assert tm.has_pending_work() is False

    @pytest.mark.asyncio
    async def test_true_while_running(self):
        from mewcode.agents.task_manager import TaskManager

        tm = TaskManager()
        tm.launch(_QuickAgent(), "task")
        assert tm.has_pending_work() is True

        # 等任务完成
        while tm._notify_queue.empty():
            await asyncio.sleep(0.01)

        # 完成通知未消费：仍视为有未处理的工作
        assert tm.has_pending_work() is True

        tm.poll_completed()
        assert tm.has_pending_work() is False

    @pytest.mark.asyncio
    async def test_false_after_completion_and_poll(self):
        from mewcode.agents.task_manager import TaskManager

        tm = TaskManager()
        tm.launch(_QuickAgent(), "t")
        while tm._notify_queue.empty():
            await asyncio.sleep(0.01)
        while tm._async_tasks:
            await asyncio.sleep(0.01)
        tm.poll_completed()
        assert tm.has_pending_work() is False


class TestMainGate:
    def test_main_uses_public_gate_not_team_only(self):
        """__main__ 不得再用 team_manager._teams 作为唯一门控（仅 AgentTool
        后台任务、无 team 的运行曾被直接跳过等待）。"""
        import inspect

        import mewcode.__main__ as m

        src = inspect.getsource(m._run_prompt)
        assert "has_pending_work" in src
        assert "_teams}" not in src, "门控不得依赖 team 私有状态"
        assert "[poll" not in src, "调试输出未清理"
