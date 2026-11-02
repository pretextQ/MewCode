"""Real worker CLI/SDK/subprocess and optional real tmux, entirely offline."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from mewcode.teams.spawn_tmux import build_cli_command

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def offline_environment(isolated_home, monkeypatch):
    monkeypatch.chdir(isolated_home)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("USERPROFILE", str(isolated_home))


def test_generated_command_reaches_real_worker_cli_not_argparse_failure(tmp_path):
    command = build_cli_command("probe", "worker", str(tmp_path), "hi", model="offline")
    words = shlex.split(command)
    if words[0] == "exec":
        words = words[1:]
    if "mewcode" in words:  # Historical broken command, run its actual arguments too.
        words = [sys.executable, "-m", "mewcode", *words[words.index("mewcode") + 1:]]
    result = subprocess.run(words, cwd=tmp_path, capture_output=True, text=True, timeout=10,
                            env={**os.environ, "PYTHONPATH": str(ROOT), "MEWCODE_LOG_FILE": str(tmp_path / "cli.log")})
    assert result.returncode == 1
    assert "unrecognized arguments" not in result.stderr and "expected one argument" not in result.stderr


def test_all_command_values_are_literal_arguments(tmp_path):
    values = ["中文 team", "worker's $(echo nope)", str(tmp_path / "目录 ' $(nope)"), "--prompt\n'; touch nope", "role ' x", "model ' x"]
    words = shlex.split(build_cli_command(*values))
    assert "-p" not in words
    for flag, value in zip(("--team-name", "--name", "--work-dir", "--prompt", "--agent-type", "--model"), values, strict=True):
        assert f"{flag}={value}" in words


def test_real_parser_roundtrips_empty_and_special_values(tmp_path):
    from mewcode.teams.spawn_cli import build_worker_argv, worker_parser
    for prompt in ("", "--starts-with-option", "中文\n' $() ; *"):
        argv = build_worker_argv("team", "名字 ' x", str(tmp_path), prompt, "role", "model", agent_id="worker-id")
        parsed = worker_parser().parse_args(argv[4:])
        assert parsed.prompt == prompt and parsed.agent_id == "worker-id"
    with pytest.raises(SystemExit):
        worker_parser().parse_args([])


def test_iterm2_nested_shell_is_literal_and_session_id_is_returned(tmp_path):
    from mewcode.teams.spawn_cli import worker_parser
    from mewcode.teams.spawn_iterm2 import spawn_iterm2_teammate
    with patch("mewcode.teams.spawn_iterm2._run_script", return_value="session-123") as script:
        info = spawn_iterm2_teammate("team", "名字 ' ;x", str(tmp_path), "$(touch nope)\n'hello")
    assert info.session_id == "session-123"
    command = shlex.split(script.call_args.args[1])[2]
    argv = shlex.split(command)
    parsed = worker_parser().parse_args(argv[5:])
    assert parsed.name == "名字 ' ;x" and parsed.prompt == "$(touch nope)\n'hello"
    assert not (tmp_path / "nope").exists()


@pytest.mark.asyncio
async def test_reused_pane_id_is_not_terminated(tmp_path):
    from mewcode.teams.mailbox import Mailbox
    from mewcode.teams.pane_handle import PaneHandle
    pane = PaneHandle("%0", "tmux", tmp_path / "state.json", owner_pid="original-pid")
    with patch("mewcode.teams.spawn_tmux._run_tmux", return_value="0 different-pid") as native:
        assert await pane.stop(Mailbox(tmp_path / "mail"), "worker-id", "lead")
    assert all(call.args[0] != "kill-pane" for call in native.call_args_list)


async def until(predicate, timeout=12):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.03)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_pane,stop_running", [(False, False), (True, False), (True, True)],
                         ids=["subprocess", "tmux", "tmux-taskstop-running"])
async def test_worker_real_sdk_followup_shutdown(native_pane, stop_running, tmp_path, monkeypatch):
    from aiohttp import web

    from mewcode.config import ProviderConfig
    from mewcode.teams.mailbox import create_message
    from mewcode.teams.manager import TeamManager
    from mewcode.teams.models import TeammateInfo
    from mewcode.teams.pane_handle import PaneHandle
    from mewcode.teams.spawn_cli import bootstrap_server, build_worker_argv, state_path
    from mewcode.tools.send_message import SendMessageParams, SendMessageTool

    if native_pane and (os.environ.get("MEWCODE_TMUX_TEST") != "1" or not shutil.which("tmux")):
        pytest.skip("explicit isolated Linux tmux integration")
    manager = TeamManager()
    team = manager.create_team("offline", "parent-id", is_interactive=False)
    member = TeammateInfo("中文 worker 'x", "worker-id", "role", "offline-model", str(tmp_path), "tmux", True)
    manager.register_member(team.name, member)
    requests = []
    release_response = asyncio.Event()

    async def reply(request):
        requests.append(await request.json())
        assert request.headers["Authorization"] == "Bearer offline-test-token"
        if stop_running:
            await release_response.wait()
        event = {"id": "offline", "object": "chat.completion.chunk", "created": 0, "model": "offline-model",
                 "choices": [{"index": 0, "delta": {"content": "offline result"}, "finish_reason": "stop"}]}
        return web.Response(text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n", content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/v1/chat/completions", reply)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    provider = ProviderConfig("parent-provider", "openai-compat", f"http://127.0.0.1:{port}/v1", "offline-model",
                              api_key="offline-test-token")
    payload = dict(agent_id=member.agent_id, name=member.name, team_config=team.config_path, work_dir=str(tmp_path),
                   agent_type="role", provider=asdict(provider), parent_id="parent-id", trace_id="trace-id",
                   tools=["ReadFile", "SendMessage"], permission_mode="dontAsk", rule_paths=["", "", ""],
                   max_iterations=3, instructions="offline role policy")
    state_file = state_path(team.config_path, member.agent_id)
    process, pane, server_name = None, None, "mewcode-a1-" + uuid.uuid4().hex[:12]
    try:
        async with bootstrap_server(payload) as (bootstrap_port, token):
            options = dict(team_name=team.name, teammate_name=member.name, worktree_path=str(tmp_path), prompt="initial",
                           agent_type="role", model="offline-model", team_config=team.config_path,
                           agent_id=member.agent_id, bootstrap_port=bootstrap_port, bootstrap_token=token)
            argv = build_worker_argv(**options)
            assert "offline-test-token" not in str(argv)
            if native_pane:
                from mewcode.teams import spawn_tmux
                def run_tmux(*args):
                    result = subprocess.run(["tmux", "-L", server_name, *args], capture_output=True, text=True, timeout=10)
                    if result.returncode:
                        raise spawn_tmux.TmuxSpawnError(result.stderr)
                    return result.stdout.strip()
                monkeypatch.setattr(spawn_tmux, "_run_tmux", run_tmux)
                monkeypatch.setenv("PYTHONPATH", str(ROOT))
                info = spawn_tmux.spawn_tmux_teammate(**options)
                pane = PaneHandle(info.pane_id, "tmux", state_file, info.owner_pid)
                manager.register_pane_handle(member.agent_id, pane)
            else:
                process = await asyncio.create_subprocess_exec(*argv, cwd=tmp_path, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE, env={**os.environ, "PYTHONPATH": str(ROOT)},
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            def state_is(value):
                try:
                    return json.loads(state_file.read_text(encoding="utf-8"))["state"] == value
                except (OSError, ValueError):
                    # Like PaneHandle.status: replacement may briefly deny a
                    # Windows read. Keep polling; persistent errors still time out.
                    return False
            if stop_running:
                await until(lambda: len(requests) == 1 and state_is("running"))
            else:
                await until(lambda: state_is("listening"))
        identity = json.loads(state_file.read_text(encoding="utf-8"))
        assert identity["agent_id"] == "worker-id" and identity["parent_id"] == "parent-id"
        assert identity["trace_id"] == "trace-id" and identity["model"] == "offline-model"
        assert requests[0]["model"] == "offline-model" and "offline role policy" in str(requests[0])
        if stop_running:
            from mewcode.tools.task_stop import TaskStopParams, TaskStopTool
            stopper = TaskStopTool(manager, team.lead_agent_id, timeout=3)
            with patch("mewcode.teams.spawn_tmux.kill_pane") as force_kill:
                stopped = await stopper.execute(TaskStopParams(agent_id=member.agent_id))
                assert not stopped.is_error, stopped.output
                assert not pane.exists()
                force_kill.assert_not_called()
            assert not (await stopper.execute(TaskStopParams(agent_id=member.agent_id))).is_error
            assert len(requests) == 1
            return
        mailbox = manager.get_mailbox(team.name)
        assert "idle" in str(manager.drain_lead_mailbox())
        mailbox.write(member.agent_id, create_message("parent-id", member.agent_id, "follow-up"))
        await until(lambda: len(requests) == 2 and state_is("listening"))
        assert "follow-up" in str(requests[-1])
        assert "initial" in str(requests[-1])  # persistent worker conversation
        mailbox.write(member.agent_id, create_message("parent-id", member.agent_id, "stop", message_type="shutdown_request",
                                                       metadata={"request_id": "stop-id"}))
        await until(lambda: state_is("stopped"))
        if process:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
            assert process.returncode == 0, stderr.decode()
            assert b"offline-test-token" not in stdout + stderr
        if pane:
            await until(lambda: not pane.exists())
            assert (await SendMessageTool(manager, team.name, team.lead_agent_id).execute(
                SendMessageParams(to=member.name, message="late", summary="late test"))).is_error
        assert len(requests) == 2
        assert any(m.metadata.get("request_id") == "stop-id" for m in mailbox.consume(team.lead_agent_id))
        assert all("offline-test-token" not in p.read_text(encoding="utf-8")
                   for p in Path(team.config_path).parent.rglob("*.json"))
    finally:
        release_response.set()
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        if pane:
            await pane.stop(manager.get_mailbox(team.name), member.agent_id, team.lead_agent_id, timeout=0)
        if native_pane:
            subprocess.run(["tmux", "-L", server_name, "kill-server"], capture_output=True)
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
async def test_agent_tool_startup_failure_removes_only_owned_clean_resources(dirty, timeout, tmp_path, monkeypatch):
    from mewcode.agent import Agent
    from mewcode.agents.loader import AgentLoader
    from mewcode.agents.task_manager import TaskManager
    from mewcode.agents.trace import TraceManager
    from mewcode.client import LLMClient
    from mewcode.config import ProviderConfig
    from mewcode.teams.manager import TeamManager
    from mewcode.teams.models import BackendType
    from mewcode.teams.pane_launch import worktree_name
    from mewcode.teams.registry import AgentNameRegistry
    from mewcode.teams.spawn_tmux import TmuxPaneInfo, TmuxSpawnError
    from mewcode.tools import create_default_registry
    from mewcode.tools.agent_tool import AgentTool, AgentToolParams
    from mewcode.tools.base import StreamEnd
    from mewcode.worktree import WorktreeManager

    for args in (["init", "-q"], ["config", "user.name", "Offline"], ["config", "user.email", "offline@example.test"],
                 ["commit", "--allow-empty", "-m", "fixture", "-q"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    class OfflineClient(LLMClient):
        model = "offline"
        async def stream(self, conversation, system="", tools=None):
            raise AssertionError("Startup failure must not invoke a model")
            yield StreamEnd("end_turn")
    parent = Agent(OfflineClient(), create_default_registry(), "openai-compat", work_dir=str(tmp_path))
    worktrees, traces = WorktreeManager(str(tmp_path)), TraceManager()
    manager = TeamManager(worktrees, traces)
    team = manager.create_team("failure", parent.agent_id, is_interactive=False)
    manager._detected_backend = BackendType.TMUX
    paths = []
    alive = True
    def fail(**options):
        path = Path(options["worktree_path"])
        paths.append(path)
        if dirty:
            (path / "keep-user-edit.txt").write_text("keep", encoding="utf-8")
        if timeout:
            return TmuxPaneInfo("%test-pane", "test-session")
        raise TmuxSpawnError("offline startup failure")
    def kill(pane_id):
        nonlocal alive
        assert pane_id == "%test-pane"
        alive = False
    monkeypatch.setattr("mewcode.teams.pane_launch.STARTUP_TIMEOUT", 0.05)
    monkeypatch.setattr("mewcode.teams.pane_handle.PaneHandle.exists", lambda self, live_only=False: alive)
    monkeypatch.setattr("mewcode.teams.spawn_tmux.kill_pane", kill)
    monkeypatch.setattr("mewcode.teams.spawn_tmux.spawn_tmux_teammate", fail)
    AgentNameRegistry.instance().register("名字 ' ; x", "pre-existing-other-team")
    tool = AgentTool(AgentLoader(str(tmp_path)), TaskManager(), traces, parent,
                     provider_config=ProviderConfig("offline", "openai-compat", "http://127.0.0.1:1", "offline", "dummy"),
                     worktree_manager=worktrees, team_manager=manager)
    result = await tool.execute(AgentToolParams(team_name=team.name, name="名字 ' ; x", prompt="work", description="offline"))
    assert result.is_error and "Resources reaped: True" in result.output
    assert team.members == [] and traces._nodes == {}
    assert AgentNameRegistry.instance().resolve("名字 ' ; x") == "pre-existing-other-team"
    assert paths[0].exists() == dirty
    if dirty:
        assert (paths[0] / "keep-user-edit.txt").read_text() == "keep"
    assert worktree_name(team.name, "名字 ' ; x") != worktree_name(team.name, "名字 ' ; y")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["unknown", "path", "provider"])
async def test_worker_initialization_failure_has_no_false_ready_state(invalid, tmp_path):
    from mewcode.config import ProviderConfig
    from mewcode.teams.manager import TeamManager
    from mewcode.teams.models import TeammateInfo
    from mewcode.teams.spawn_cli import bootstrap_server, build_worker_argv, state_path
    manager = TeamManager()
    team = manager.create_team("bad", "lead-id", is_interactive=False)
    manager.register_member(team.name, TeammateInfo("worker", "worker-id", "role", "offline", str(tmp_path), "tmux"))
    provider = ProviderConfig("offline", "invalid", "http://127.0.0.1:1", "offline", api_key="dummy-secret")
    payload = dict(agent_id="worker-id", name="worker", team_config=team.config_path, work_dir=str(tmp_path),
                   agent_type="role", provider=asdict(provider))
    async with bootstrap_server(payload) as (port, token):
        argv = build_worker_argv(team.name, "worker", str(tmp_path / "wrong") if invalid == "path" else str(tmp_path), "",
                                 "role", "offline", team_config=team.config_path,
                                 agent_id="unknown" if invalid == "unknown" else "worker-id",
                                 bootstrap_port=port, bootstrap_token=token)
        process = await asyncio.create_subprocess_exec(*argv, cwd=tmp_path, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE,
                                                       env={**os.environ, "PYTHONPATH": str(ROOT)},
                                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 1 and b"dummy-secret" not in stdout + stderr
    path = state_path(team.config_path, "worker-id")
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8"))["state"] == "failed"
