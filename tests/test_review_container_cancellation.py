"""Daemon containers remain owned until cancellation cleanup completes."""
import asyncio

import pytest

from mewcode.config import ProviderConfig, SandboxConfig
from mewcode.service.sandbox import DockerSandbox


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_mode", [False, True])
async def test_repeated_cancellation_waits_for_force_removal(tmp_path, agent_mode):
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    removed = []

    class Sandbox(DockerSandbox):
        async def available(self):
            return True

        async def ensure_image(self, *args):
            return "test-image"

        async def _evict_stale_container(self, name):
            pass

        async def _run(self, argv, timeout, cwd=None):
            if argv[1] == "run":
                started.set()
                await asyncio.Event().wait()
            assert argv[1:3] == ["rm", "-f"]
            cleaning.set()
            await release.wait()
            removed.append(argv[3])
            return 0, ""

    sandbox = Sandbox(SandboxConfig(), mewcode_src=str(tmp_path), work_root=str(tmp_path / "sandbox"))
    if agent_mode:
        provider = ProviderConfig("fake", "openai", "http://unused", "fake")
        operation = sandbox.run_agent("cancel-test", str(tmp_path), "prompt", provider, timeout=60)
    else:
        operation = sandbox.run_command("cancel-test", str(tmp_path), "sleep 100", timeout=60)
    task = asyncio.create_task(operation)
    await started.wait()
    task.cancel()
    await cleaning.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert removed == ["mewcode-cancel-test" + ("" if agent_mode else "-cmd")]
    if agent_mode:
        assert not (tmp_path / "sandbox" / "runs" / "mewcode-cancel-test").exists()
