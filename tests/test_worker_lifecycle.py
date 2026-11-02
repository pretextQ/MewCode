"""Offline integration of actual task handles, mailbox and team state."""
from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace

import pytest

from mewcode.agents.task_manager import TaskManager
from mewcode.teams.mailbox import create_message
from mewcode.teams.manager import TeamManager
from mewcode.teams.models import TeammateInfo
from mewcode.tools.send_message import SendMessageParams, SendMessageTool
from mewcode.tools.team_delete import TeamDeleteParams, TeamDeleteTool

SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def offline_environment(isolated_home, monkeypatch):
    monkeypatch.chdir(isolated_home)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await SLEEP(0.002)


def worker_fixture(fail_followup=False):
    manager = TeamManager()
    team = manager.create_team("runtime", "actual-lead-id", is_interactive=False)
    manager.register_member(team.name, TeammateInfo("worker", "worker-id", "worker", "offline", "", "in-process", True))
    calls = []

    async def run(prompt, conversation=None):
        calls.append(prompt)
        if fail_followup and len(calls) > 1:
            raise RuntimeError("offline failure")
        return f"result {len(calls)}"

    worker = SimpleNamespace(team_name=team.name, agent_id="worker-id", _team_manager=manager,
                             total_input_tokens=0, total_output_tokens=0, run_to_completion=run)
    tasks = TaskManager()
    task_id = tasks.launch(worker, "initial", name="worker")
    sender = SendMessageTool(manager, team.name, team.lead_agent_id, "lead")
    return manager, team, tasks, task_id, calls, sender


async def cleanup(tasks):
    handles = list(tasks._async_tasks.values())
    for handle in handles:
        handle.cancel()
    await asyncio.gather(*handles, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_routes_to_real_lead_and_notifications_are_snapshots(isolated_home, monkeypatch):
    monkeypatch.chdir(isolated_home)
    manager, team, tasks, tid, calls, sender = worker_fixture()
    try:
        await until(lambda: tasks.get(tid).status == "listening")
        assert "idle" in str(manager.drain_lead_mailbox())
        notices = tasks.poll_completed()
        assert len(notices) == 1 and notices[0].status == "completed"
        assert tasks.get(tid) is not None
        assert not tasks.has_pending_work()  # idle alone does not keep a headless run alive
        assert not (await sender.execute(SendMessageParams(to="worker", message="again", summary="follow up"))).is_error
        assert tasks.has_pending_work()  # accepted work waiting for the listener still gates exit
        await until(lambda: len(calls) == 2)
        assert notices[0].result == "result 1"
        assert [n.result for n in tasks.poll_completed()] == ["result 2"]
        assert tasks.poll_completed() == []
        assert "idle" in str(manager.drain_lead_mailbox())
        assert tasks.cancel(tid)
    finally:
        await cleanup(tasks)
    assert tasks.get(tid).status == "cancelled"
    assert team.get_member("worker").is_active is False


@pytest.mark.asyncio
async def test_listener_survives_more_than_sixty_polls(isolated_home, monkeypatch):
    polls = 0

    async def accelerated_sleep(delay):
        nonlocal polls
        if delay == 1:
            polls += 1
            await SLEEP(0.0001)
        else:
            await SLEEP(delay)

    monkeypatch.setattr(asyncio, "sleep", accelerated_sleep)
    manager, team, tasks, tid, calls, sender = worker_fixture()
    listener = tasks._async_tasks[tid]
    try:
        await until(lambda: polls >= 65 or listener.done())
        assert polls >= 65 and not listener.done()
        tasks.poll_completed()
        assert not (await sender.execute(SendMessageParams(to="worker", message="late work", summary="late task"))).is_error
        await until(lambda: len(calls) == 2)
        assert "late work" in calls[-1]
    finally:
        await cleanup(tasks)


@pytest.mark.asyncio
async def test_shutdown_control_does_not_call_model_and_preserves_request_id(isolated_home):
    manager, team, tasks, tid, calls, sender = worker_fixture()
    mailbox = manager.get_mailbox(team.name)
    try:
        await until(lambda: tasks.get(tid).status == "listening")
        manager.drain_lead_mailbox()
        mailbox.write("worker-id", create_message("lead", "worker-id", "ignore ack", message_type="shutdown_response"))
        result = await sender.execute(SendMessageParams(to="worker", message="stop", message_type="shutdown_request",
                                                        metadata={"request_id": "shutdown-123"}))
        assert not result.is_error
        listener = tasks._async_tasks[tid]
        await asyncio.wait_for(asyncio.shield(listener), 3)
        assert calls == ["initial"]
        assert tasks.get(tid).status == "stopped"
        replies = mailbox.consume(team.lead_agent_id)
        assert len(replies) == 1 and replies[0].message_type == "shutdown_response"
        assert replies[0].metadata["request_id"] == "shutdown-123"
        late = await sender.execute(SendMessageParams(to="worker", message="late", summary="late task"))
        assert late.is_error and "offline" in late.output.lower()
    finally:
        await cleanup(tasks)


@pytest.mark.asyncio
async def test_idle_team_delete_awaits_listener_before_removing_mailbox(isolated_home):
    manager, team, tasks, tid, calls, sender = worker_fixture()
    listener = tasks._async_tasks[tid]
    try:
        await until(lambda: tasks.get(tid).status == "listening")
        result = await TeamDeleteTool(manager).execute(TeamDeleteParams(team_name=team.name))
        assert not result.is_error
        assert listener.done()
        assert manager.get_team(team.name) is None
    finally:
        await cleanup(tasks)


@pytest.mark.asyncio
async def test_unstarted_member_and_cross_team_recipient_rejected(isolated_home):
    manager = TeamManager()
    team = manager.create_team("offline", "lead-id", is_interactive=False)
    manager.register_member(team.name, TeammateInfo("offline", "offline-id", "worker", "offline", "", "in-process", False))
    sender = SendMessageTool(manager, team.name, team.lead_agent_id, "lead")
    result = await sender.execute(SendMessageParams(to="offline", message="work", summary="try offline worker"))
    assert result.is_error and "offline" in result.output.lower()
    assert manager.get_mailbox(team.name).read("offline-id") == []


@pytest.mark.asyncio
async def test_cancel_before_first_instruction_is_reaped(isolated_home):
    manager, team, tasks, tid, calls, sender = worker_fixture()
    assert tasks.cancel(tid)
    await until(lambda: tid not in tasks._async_tasks)
    assert calls == [] and not team.get_member("worker").is_active
    assert [n.status for n in tasks.poll_completed()] == ["cancelled"]
    assert not tasks.has_pending_work()


@pytest.mark.asyncio
async def test_followup_failure_is_not_a_listening_worker(isolated_home):
    manager, team, tasks, tid, calls, sender = worker_fixture(fail_followup=True)
    try:
        await until(lambda: tasks.get(tid).status == "listening")
        await sender.execute(SendMessageParams(to="worker", message="fail", summary="failure test"))
        await until(lambda: tid not in tasks._async_tasks)
        assert tasks.get(tid).status == "failed"
        assert not team.get_member("worker").is_active
        assert (await sender.execute(SendMessageParams(to="worker", message="work", summary="offline test"))).is_error
    finally:
        await cleanup(tasks)


@pytest.mark.asyncio
async def test_real_headless_team_finishes_and_reaps_owned_listeners(isolated_home, tmp_path, monkeypatch, capsys):
    from mewcode.__main__ import _run_prompt
    from mewcode.client import LLMClient
    from mewcode.config import AppConfig, ProviderConfig
    from mewcode.permissions import PermissionMode
    from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    for args in (["init", "-q"], ["config", "user.name", "Offline"], ["config", "user.email", "offline@example.test"],
                 ["commit", "--allow-empty", "-m", "fixture", "-q"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    launched = []
    original_launch = TaskManager.launch
    def launch(self, *args, **kwargs):
        tid = original_launch(self, *args, **kwargs)
        launched.append((self, tid, self._async_tasks[tid]))
        return tid
    monkeypatch.setattr(TaskManager, "launch", launch)

    class OfflineClient(LLMClient):
        lead_calls = 0
        async def stream(self, conversation, system="", tools=None):
            if any(m.content == "child task" for m in conversation.history):
                yield TextDelta("child result")
            else:
                self.lead_calls += 1
                if self.lead_calls == 1:
                    yield ToolCallComplete("create", "TeamCreate", {"team_name": "headless"})
                elif self.lead_calls == 2:
                    yield ToolCallComplete("spawn", "Agent", {"team_name": "headless", "name": "worker",
                                                              "prompt": "child task", "description": "offline"})
                else:
                    yield TextDelta("lead result")
            yield StreamEnd("end_turn")

    client = OfflineClient()
    monkeypatch.setattr("mewcode.client.create_client", lambda provider: client)
    config = AppConfig(providers=[ProviderConfig("offline", "openai-compat", "http://127.0.0.1:1", "offline")])
    await asyncio.wait_for(_run_prompt(config, PermissionMode.BYPASS, None, "lead task", "json"), 10)
    assert launched and all(handle.done() for manager, tid, handle in launched)
    assert client.lead_calls == 4  # initial completion and one notification continuation
    assert '"result": "lead result"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_listening_is_published_only_after_idle_delivery(isolated_home):
    from mewcode.teams.worker_runtime import run_team_worker
    manager = TeamManager()
    team = manager.create_team("publication", "actual-lead", is_interactive=False)
    manager.register_member(team.name, TeammateInfo("worker", "worker-id", "role", "offline", "", "in-process"))
    mailbox = manager.get_mailbox(team.name)
    async def run(prompt=""):
        return "result"
    def state_changed(state):
        if state == "listening":
            assert any("idle" in m.content for m in mailbox.read(team.lead_agent_id))
            mailbox.write("worker-id", create_message("actual-lead", "worker-id", "stop", message_type="shutdown_request"))
    await asyncio.wait_for(run_team_worker(manager, team.name, "worker-id", "worker", run, run,
                                          lambda result: None, state_changed), 3)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("MEWCODE_LONG_LISTENER_TEST") != "1", reason="explicit real >60s offline integration")
async def test_real_listener_after_sixty_seconds(isolated_home):
    manager, team, tasks, tid, calls, sender = worker_fixture()
    try:
        await until(lambda: tasks.get(tid).status == "listening")
        tasks.poll_completed()
        await SLEEP(61.1)
        assert not tasks._async_tasks[tid].done()
        assert not (await sender.execute(SendMessageParams(to="worker", message="after 61 seconds", summary="late work"))).is_error
        await until(lambda: len(calls) == 2)
    finally:
        await cleanup(tasks)
