
from __future__ import annotations

import asyncio
import os
from typing import Any

from pydantic import BaseModel, Field

from mewcode.git_safety import FILTER_QUERY, filter_overrides
from mewcode.processes import create_exec_process, create_shell_process, kill_process_tree, release_process
from mewcode.shell import shell_description
from mewcode.tools.base import Tool, ToolResult, decode_output
from mewcode.tools.shell_policy import concurrent_shell, git_status_words, semantic_nonerror

MAX_TIMEOUT = 600
MAX_CAPTURE_BYTES = 2_097_152


async def capture_bounded(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    total = 0

    async def read(stream: asyncio.StreamReader | None) -> bytes:
        nonlocal total
        chunks = []
        if stream is None:
            return b""
        while chunk := await stream.read(65536):
            total += len(chunk)
            if total > MAX_CAPTURE_BYTES:
                raise ValueError(f"command exceeds {MAX_CAPTURE_BYTES} byte output limit")
            chunks.append(chunk)
        return b"".join(chunks)

    tasks = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr))]
    try:
        out, err = await asyncio.gather(*tasks)
        await proc.wait()
        return out, err
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")


def query_env() -> dict[str, str]:
    return {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '', 'GIT_OPTIONAL_LOCKS': '0',
            'GIT_PAGER': 'cat', 'PAGER': 'cat', 'GIT_EXTERNAL_DIFF': ''}


async def guarded_status(words: list[str], work_dir: str | None, timeout: float) -> list[str]:
    proc = None
    try:
        proc = await create_exec_process('git', '--no-pager', '-c', 'core.fsmonitor=false', *FILTER_QUERY,
                                         cwd=work_dir, env=query_env(), stdin=asyncio.subprocess.DEVNULL,
                                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            data, _ = await asyncio.wait_for(capture_bounded(proc), timeout=min(3.0, timeout))
        except TimeoutError as exc:
            raise OSError('Git filter inspection timed out') from exc
        overrides = filter_overrides(data, proc.returncode if proc.returncode is not None else -1)
        return ['git', '--no-pager', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                '-c', 'diff.external=', '-c', 'submodule.recurse=false', *overrides,
                *words[1:], '--ignore-submodules=all']
    finally:
        if proc is not None and proc.returncode is None:
            await kill_process_tree(proc)
        release_process(proc)


class Bash(Tool[Params]):
    name = "Bash"
    description = "Execute a command and return stdout and stderr. Shell: " + shell_description()
    params_model = Params
    category = "command"

    def can_run_concurrently(self, arguments: dict[str, Any]) -> bool:
        return concurrent_shell(arguments)

    async def execute(self, params: Params) -> ToolResult:
        timeout = min(params.timeout, MAX_TIMEOUT)
        proc = None
        deadline = asyncio.get_running_loop().time() + timeout

        try:
            words = git_status_words(params.command)
            kwargs: dict[str, Any] = {'cwd': self._work_dir or None, 'stdout': asyncio.subprocess.PIPE,
                                      'stderr': asyncio.subprocess.PIPE}
            if words is not None:
                argv = await guarded_status(words, self._work_dir or None, timeout)
                proc = await create_exec_process(*argv, **kwargs, env=query_env(), stdin=asyncio.subprocess.DEVNULL)
            else:
                proc = await create_shell_process(params.command, **kwargs)
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            stdout, stderr = await asyncio.wait_for(capture_bounded(proc), timeout=remaining)
        except TimeoutError:
            if proc is not None:
                await kill_process_tree(proc)
            code = proc.returncode if proc is not None else 'unavailable'
            return ToolResult(output=f"Exit code: {code} (timeout termination)\n"
                                     f"Error: command timed out after {timeout}s",
                              is_error=True)
        except asyncio.CancelledError:
            if proc is not None:
                await kill_process_tree(proc)
            raise
        except Exception as e:
            if proc is not None:
                await kill_process_tree(proc)
            code = proc.returncode if proc is not None else 'unavailable'
            return ToolResult(output=f"Exit code: {code}\nError executing command: {e}", is_error=True)
        finally:
            release_process(proc)

        parts: list[str] = []
        if stdout:
            parts.append(f"STDOUT:\n{decode_output(stdout)}")
        if stderr:
            parts.append(f"STDERR:\n{decode_output(stderr)}")
        if not parts:
            parts.append("(no output)")

        code = proc.returncode
        output = f"Exit code: {code}\n" + "\n".join(parts)
        return ToolResult(output=output, is_error=code != 0 and not semantic_nonerror(params.command, code or 0))

