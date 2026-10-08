"""Sandbox failures must not grant host execution implicitly."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mewcode.config import ServiceConfig
from mewcode.service.execution import AgentRunOutcome, HeadlessAgentRunner, SandboxTestRunner
from mewcode.service.jobs import Job
from mewcode.service.sandbox import SandboxError, SandboxUnavailable


def job() -> Job:
    return Job(id="j1", fingerprint="f", repo="demo", severity="warning", status="fixing", payload={})


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_default_never_calls_host_agent(tmp_path: Path, missing: bool):
    sandbox = None if missing else AsyncMock()
    if sandbox is not None:
        sandbox.available.return_value = False
    runner = HeadlessAgentRunner(ServiceConfig(), None, sandbox=sandbox)
    runner._run_direct = AsyncMock()
    with pytest.raises(SandboxUnavailable, match="refusing host"):
        await runner.run(job(), str(tmp_path), "p", lambda e: None)
    runner._run_direct.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_error", [False, True])
async def test_default_never_runs_host_tests(tmp_path: Path, probe_error: bool):
    sandbox = AsyncMock()
    sandbox.available.return_value = False
    if probe_error:
        sandbox.available.side_effect = RuntimeError("probe failed")
    fallback = AsyncMock()
    runner = SandboxTestRunner(sandbox, fallback=fallback)
    with pytest.raises(SandboxUnavailable, match="refusing host"):
        await runner.run(str(tmp_path), "dangerous repository test", 1)
    fallback.run.assert_not_awaited()
    sandbox.run_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_container_execution_error_never_falls_back(tmp_path: Path):
    sandbox = AsyncMock()
    sandbox.available.return_value = True
    sandbox.run_agent.side_effect = SandboxError("image build failed")
    config = ServiceConfig()
    config.sandbox.allow_host_fallback = True
    runner = HeadlessAgentRunner(config, None, sandbox=sandbox)
    runner._run_direct = AsyncMock()
    with pytest.raises(SandboxError, match="image build failed"):
        await runner.run(job(), str(tmp_path), "p", lambda e: None)
    runner._run_direct.assert_not_awaited()
    sandbox.run_command.side_effect = SandboxError("container failed")
    fallback = AsyncMock()
    test_runner = SandboxTestRunner(sandbox, fallback=fallback, allow_host_fallback=True)
    with pytest.raises(SandboxError, match="container failed"):
        await test_runner.run(str(tmp_path), "test", 1)
    fallback.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_host_agent_emits_mode_event(tmp_path: Path):
    config = ServiceConfig()
    config.sandbox.enabled = False
    runner = HeadlessAgentRunner(config, None)
    runner._run_direct = AsyncMock(return_value=AgentRunOutcome(final_text="ok"))
    events = []
    await runner.run(job(), str(tmp_path), "p", events.append)
    assert events[0]["type"] == "execution_mode"
    assert "no OS sandbox" in events[0]["detail"]
