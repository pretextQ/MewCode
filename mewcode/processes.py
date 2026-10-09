"""Command process ownership and bounded process-tree cleanup."""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import signal
import subprocess
import sys
import uuid
from ctypes import wintypes
from typing import Any

REAP_TIMEOUT = 5

# Join the job before spawning the real command. Attaching an already-running
# shell races with child creation, and taskkill cannot find an exited parent.
_WINDOWS_LAUNCHER = """
import ctypes, json, subprocess, sys, time
from ctypes import wintypes
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k.OpenJobObjectW.restype = wintypes.HANDLE
k.GetCurrentProcess.restype = wintypes.HANDLE
k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
k.CloseHandle.argtypes = [wintypes.HANDLE]
class Accounting(ctypes.Structure):
    _fields_ = [('user', ctypes.c_longlong), ('kernel', ctypes.c_longlong),
                ('period_user', ctypes.c_longlong), ('period_kernel', ctypes.c_longlong),
                ('page_faults', wintypes.DWORD), ('total', wintypes.DWORD),
                ('active', wintypes.DWORD), ('terminated', wintypes.DWORD)]
h = k.OpenJobObjectW(5, False, sys.argv[1])
if not h:
    raise ctypes.WinError(ctypes.get_last_error())
try:
    if not k.AssignProcessToJobObject(h, k.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    code = subprocess.call(json.loads(sys.argv[2]), shell=sys.argv[3] == 'shell')
    info = Accounting()
    while True:
        if not k.QueryInformationJobObject(h, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.active <= 1:
            break
        time.sleep(0.01)
finally:
    k.CloseHandle(h)
sys.exit(code)
"""


class _WindowsJob:
    def __init__(self) -> None:
        self.name = "Local\\MewCode-command-" + uuid.uuid4().hex
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        if self.handle and not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


_jobs: dict[asyncio.subprocess.Process, _WindowsJob] = {}


async def _spawn(command: str | list[str], shell: bool, kwargs: dict[str, Any]) -> asyncio.subprocess.Process:
    if os.name != "nt":
        kwargs["start_new_session"] = True
        if isinstance(command, str):
            return await asyncio.create_subprocess_shell(command, **kwargs)
        return await asyncio.create_subprocess_exec(*command, **kwargs)
    job = _WindowsJob()
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _WINDOWS_LAUNCHER, job.name,
            json.dumps(command), "shell" if shell else "exec", **kwargs,
        )
    except BaseException:
        try:
            job.terminate()
        finally:
            job.close()
        raise
    _jobs[proc] = job
    return proc


async def create_shell_process(command: str, **kwargs: Any) -> asyncio.subprocess.Process:
    return await _spawn(command, True, kwargs)


async def create_exec_process(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
    return await _spawn(list(argv), False, kwargs)


def release_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None:
        return
    job = _jobs.pop(proc, None)
    if job is not None:
        job.close()


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        job = _jobs.get(proc)
        if job is not None:
            job.terminate()
            # Cancellation may arrive before the launcher has joined its job.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        elif os.name == "nt":
            if proc.returncode is None:
                result = await asyncio.to_thread(
                    subprocess.run, ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=REAP_TIMEOUT,
                )
                if result.returncode and proc.returncode is None:
                    proc.kill()
        else:
            try:
                # The process group retains its ID even after its leader exits.
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                if proc.returncode is None:
                    proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=REAP_TIMEOUT)
    finally:
        release_process(proc)


async def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Complete cleanup even if the caller receives another cancellation."""
    task = asyncio.create_task(_kill_tree(proc))
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        try:
            task.result()
        finally:
            raise cancellation
    task.result()
