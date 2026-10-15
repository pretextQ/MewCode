"""Shared test runners must retain each job's container and repository identity."""
import asyncio
from types import SimpleNamespace

import pytest

from mewcode.config import RepoConfig, ServiceConfig
from mewcode.service.execution import ExecutionChain, SandboxTestRunner
from mewcode.service.sandbox import SandboxRunResult


@pytest.mark.asyncio
async def test_shared_runner_uses_distinct_job_and_repo_ids():
    calls = []

    class Sandbox:
        async def available(self):
            return True

        async def run_command(self, job_id, work_dir, command, **kwargs):
            calls.append((job_id, work_dir, kwargs["repo_name"]))
            await asyncio.sleep(0)
            return SandboxRunResult(0)

    class Store:
        async def add_event(self, *args):
            pass

    runner = SandboxTestRunner(Sandbox())
    chain = ExecutionChain(ServiceConfig(), Store(), None, test_runner=runner)
    await asyncio.gather(*(
        chain._run_tests(
            SimpleNamespace(id=f"job-{name}", repo=name),
            RepoConfig(name=name, path=f"/{name}", test_command="test"), f"/{name}", "verify"
        ) for name in ("a", "b")
    ))
    assert sorted(calls) == [("job-a", "/a", "a"), ("job-b", "/b", "b")]
    calls.clear()
    await asyncio.gather(runner.run("/a", "test", 10), runner.run("/a", "test", 10))
    assert calls[0][0] != calls[1][0]
