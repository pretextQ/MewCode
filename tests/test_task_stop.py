"""Coordinator registration and real TaskManager/pane ownership stop paths."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mewcode.agent import Agent
from mewcode.agents.task_manager import TaskManager
from mewcode.client import LLMClient
from mewcode.teams.manager import TeamManager
from mewcode.teams.models import TeammateInfo
from mewcode.tools import create_default_registry
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from mewcode.tools.team_create import TeamCreateParams, TeamCreateTool


@pytest.fixture(autouse=True)
def offline_environment(isolated_home, monkeypatch):
    monkeypatch.chdir(isolated_home)


class OfflineClient(LLMClient):
    called = False
    async def stream(self, conversation, system="", tools=None):
        if not self.called:
            self.called = True
            yield ToolCallComplete("stop-worker", "TaskStop", {"agent_id": "worker"})
        else:
            yield TextDelta("done")
        yield StreamEnd("end_turn")


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,env", [(True, True), (True, False), (False, True), (False, False)])
async def test_real_teamcreate_mode_registers_taskstop_only_with_both_switches(enabled, env, monkeypatch):
    monkeypatch.setenv("MEWCODE_COORDINATOR_MODE", "1" if env else "0")
    manager = TeamManager()
    parent = Agent(OfflineClient(), create_default_registry(), "openai-compat")
    create = TeamCreateTool(manager, parent, is_interactive=False, enable_coordinator_mode=enabled)
    result = await create.execute(TeamCreateParams(team_name="registration"))
    assert not result.is_error
    assert (parent.registry.get("TaskStop") is not None) == (enabled and env)
    if enabled and env:
        assert "TaskStop" in str(parent.registry.get_all_schemas("openai-compat"))
        assert parent._full_registry.get("TaskStop") is None
        from mewcode.agents.parser import AgentDef
        from mewcode.agents.tool_filter import build_teammate_tools, resolve_agent_tools
        definition = AgentDef("worker", "offline")
        for backend in ("in-process", "tmux"):
            child = build_teammate_tools(parent.registry, manager, "registration", "child-id", "child", backend)
            assert child.get("TaskStop") is None
        assert resolve_agent_tools(parent.registry, definition).get("TaskStop") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("listening", [False, True])
async def test_actual_agent_taskstop_waits_for_worker_cleanup_and_is_idempotent(listening, monkeypatch, isolated_home):
    monkeypatch.setenv("MEWCODE_COORDINATOR_MODE", "1")
    manager, tasks = TeamManager(), TaskManager()
    parent = Agent(OfflineClient(), create_default_registry(), "openai-compat")
    await TeamCreateTool(manager, parent, is_interactive=False, enable_coordinator_mode=True).execute(
        TeamCreateParams(team_name="stopping"))
    team = manager.get_team("stopping")
    edit = isolated_home / "keep-edit.txt"
    edit.write_text("keep", encoding="utf-8")
    manager.register_member(team.name, TeammateInfo("worker", "worker-id", "worker", "offline",
                                                   str(isolated_home), "in-process", True))
    started, cleaned = asyncio.Event(), asyncio.Event()
    async def run(prompt):
        started.set()
        if listening:
            return "initial done"
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.02)
            cleaned.set()
    worker = SimpleNamespace(agent_id="worker-id", team_name=team.name, _team_manager=manager,
                             total_input_tokens=0, total_output_tokens=0, run_to_completion=run)
    tid = tasks.launch(worker, "initial", name="worker")
    handle = tasks._async_tasks[tid]
    try:
        await started.wait()
        if listening:
            await until(lambda: tasks.get(tid).status == "listening")
            tasks.poll_completed()
        assert parent.registry.get("TaskStop") is not None
        assert await parent.run_to_completion("stop worker") == "done"
        assert handle.done() and (listening or cleaned.is_set())
        assert tasks.get(tid).status == "stopped"
        assert edit.read_text(encoding="utf-8") == "keep" and manager.get_team(team.name) is not None
        stop = parent.registry.get("TaskStop")
        params = stop.params_model(agent_id="worker-id")
        assert not (await stop.execute(params)).is_error
        sender = parent.registry.get("SendMessage")
        late = await sender.execute(sender.params_model(to="worker", message="late", summary="late test"))
        assert late.is_error
        unknown = await stop.execute(stop.params_model(agent_id="unknown"))
        assert unknown.is_error and "worker" in unknown.output
        foreign = manager.create_team("foreign", "different-lead", is_interactive=False)
        manager.register_member(foreign.name, TeammateInfo("other", "foreign-id", "worker", "offline", "", "in-process"))
        assert (await stop.execute(stop.params_model(agent_id="foreign-id"))).is_error
    finally:
        await tasks.shutdown_all()


@pytest.mark.asyncio
async def test_stop_timeout_does_not_report_cleanup_complete(isolated_home):
    from mewcode.tools.task_stop import TaskStopParams, TaskStopTool
    manager, tasks = TeamManager(), TaskManager()
    team = manager.create_team("timeout", "lead-id", is_interactive=False)
    manager.register_member(team.name, TeammateInfo("worker", "worker-id", "role", "offline", "", "in-process", True))
    started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def run(prompt):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
        return "done"
    worker = SimpleNamespace(agent_id="worker-id", team_name=team.name, _team_manager=manager,
                             total_input_tokens=0, total_output_tokens=0, run_to_completion=run)
    tid = tasks.launch(worker, "initial")
    handle = tasks._async_tasks[tid]
    stopper = TaskStopTool(manager, team.lead_agent_id, timeout=0.02)
    try:
        await started.wait()
        result = await stopper.execute(TaskStopParams(agent_id="worker-id"))
        assert result.is_error and not handle.done() and cancelling.is_set()
        assert tasks.get(tid).status != "stopped"
        release.set()
        await until(handle.done)
        assert not (await stopper.execute(TaskStopParams(agent_id="worker-id"))).is_error
        assert tasks.get(tid).status == "stopped"
    finally:
        release.set()
        await tasks.shutdown_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("reap", [True, False])
async def test_pane_stop_confirms_only_owned_native_handle(reap, isolated_home):
    from mewcode.teams.pane_handle import PaneHandle
    from mewcode.teams.spawn_cli import state_path, write_state
    from mewcode.tools.task_stop import TaskStopParams, TaskStopTool
    manager = TeamManager()
    team = manager.create_team("pane", "lead-id", is_interactive=False)
    manager.register_member(team.name, TeammateInfo("worker", "worker-id", "role", "offline", "", "tmux", True))
    path = state_path(team.config_path, "worker-id")
    write_state(path, "running")
    pane = PaneHandle("%owned", "tmux", path)
    manager.register_pane_handle("worker-id", pane)
    alive, kills = True, []
    def kill(pane_id):
        nonlocal alive
        kills.append(pane_id)
        if reap:
            alive = False
    with patch.object(PaneHandle, "exists", lambda self, live_only=False: alive), \
            patch("mewcode.teams.spawn_tmux.kill_pane", side_effect=kill):
        result = await TaskStopTool(manager, "lead-id", timeout=0.01).execute(TaskStopParams(agent_id="worker-id"))
        assert result.is_error == (not reap)
        assert kills == ["%owned"]
        assert (json.loads(path.read_text(encoding="utf-8"))["state"] == "stopped") == reap
