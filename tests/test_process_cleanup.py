"""Timeout/cancellation must reap descendants, even after their parent exits."""
import asyncio
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mewcode import processes
from mewcode.hooks.executors import execute_command
from mewcode.hooks.models import Action, HookContext
from mewcode.processes import _jobs, create_exec_process, create_shell_process, kill_process_tree, release_process
from mewcode.service.execution import TestRunner as RepositoryTestRunner
from mewcode.service.sandbox import run_host_command
from mewcode.tools.bash import Bash, Params

HELPER = Path(__file__).parent / "helpers" / "process_tree.py"


def is_running(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    try:
        return not (stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")
    except FileNotFoundError:
        return False


async def wait_for_descendants(root):
    deadline = time.monotonic() + 5
    while not (root / "grandchild.pid").exists():
        if time.monotonic() > deadline:
            pytest.fail("descendants did not start")
        await asyncio.sleep(0.01)


async def assert_reaped(root):
    pids = [int(p.read_text()) for p in root.glob("*.pid")]
    assert len(pids) == 3
    deadline = time.monotonic() + 2
    while any(is_running(pid) for pid in pids) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not any(is_running(pid) for pid in pids)
    assert not (root / "late-marker").exists()


async def cleanup(root, task):
    # The red regression must not itself leave orphan test processes behind.
    for path in root.glob("*.pid"):
        pid = int(path.read_text())
        if is_running(pid):
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run, ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=5,
                )
            else:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def start_command(root, mode, kind, timeout):
    argv = [sys.executable, str(HELPER), str(root), mode, "parent"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    if kind == "bash":
        return asyncio.create_task(Bash().execute(Params(command=command, timeout=timeout)))
    if kind == "service":
        return asyncio.create_task(RepositoryTestRunner().run(str(root), command, timeout))
    if kind == "host":
        return asyncio.create_task(run_host_command(argv, timeout))
    return asyncio.create_task(execute_command(Action(type="command", command=command, timeout=timeout), HookContext()))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bash", "hook", "service", "host"])
@pytest.mark.parametrize("mode", ["tree", "orphan"])
async def test_timeout_reaps_descendants(tmp_path, kind, mode):
    task = start_command(tmp_path, mode, kind, 2)
    try:
        await wait_for_descendants(tmp_path)
        result = await asyncio.wait_for(asyncio.shield(task), 8)
        assert "timed out" in (result[1] if kind == "host" else result.output)
        await assert_reaped(tmp_path)
        assert not _jobs
    finally:
        await cleanup(tmp_path, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bash", "hook", "service", "host"])
@pytest.mark.parametrize("mode", ["tree", "orphan"])
async def test_cancellation_reaps_descendants(tmp_path, kind, mode):
    task = start_command(tmp_path, mode, kind, 30)
    try:
        await wait_for_descendants(tmp_path)
        if mode == "orphan":
            deadline = time.monotonic() + 2
            while is_running(int((tmp_path / "parent.pid").read_text())) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert not is_running(int((tmp_path / "parent.pid").read_text()))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        await assert_reaped(tmp_path)
        assert not _jobs
    finally:
        await cleanup(tmp_path, task)


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_interrupt_cleanup(monkeypatch):
    proc = await create_exec_process(sys.executable, "-c", "import time; time.sleep(30)")
    entered = asyncio.Event()
    release = asyncio.Event()
    original = processes._kill_tree

    async def delayed_kill(proc):
        entered.set()
        await release.wait()
        await original(proc)

    monkeypatch.setattr(processes, "_kill_tree", delayed_kill)
    task = asyncio.create_task(kill_process_tree(proc))
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.returncode is not None
        assert not _jobs
        await kill_process_tree(proc)  # 重复回收保持幂等
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await original(proc)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["exec", "shell"])
async def test_normal_output_and_exit_status_are_preserved(kind):
    argv = [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"]
    kwargs = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}
    if kind == "exec":
        proc = await create_exec_process(*argv, **kwargs)
    else:
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        proc = await create_shell_process(command, **kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 5)
        assert proc.returncode == 7
        assert stdout.strip() == b"out"
        assert stderr.strip() == b"err"
        if os.name != "nt":
            assert proc.pid != os.getpgrp()
    finally:
        release_process(proc)
    assert not _jobs
