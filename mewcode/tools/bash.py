
from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from mewcode.processes import create_shell_process, kill_process_tree, release_process
from mewcode.shell import shell_description
from mewcode.tools.base import Tool, ToolResult, decode_output

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


class Bash(Tool[Params]):
    name = "Bash"
    description = "Execute a command and return stdout and stderr. Shell: " + shell_description()
    params_model = Params
    category = "command"


    async def execute(self, params: Params) -> ToolResult:
        timeout = min(params.timeout, MAX_TIMEOUT)
        proc = None

        try:
            proc = await create_shell_process(
                params.command,
                cwd=self._work_dir or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(capture_bounded(proc), timeout=timeout)
        except TimeoutError:
            if proc is not None:
                await kill_process_tree(proc)
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except asyncio.CancelledError:
            if proc is not None:
                await kill_process_tree(proc)
            raise
        except Exception as e:
            if proc is not None:
                await kill_process_tree(proc)
            return ToolResult(output=f"Error executing command: {e}", is_error=True)
        finally:
            release_process(proc)

        parts: list[str] = []
        if stdout:
            parts.append(f"STDOUT:\n{decode_output(stdout)}")
        if stderr:
            parts.append(f"STDERR:\n{decode_output(stderr)}")
        if not parts:
            parts.append("(no output)")

        output = "\n".join(parts)
        return ToolResult(output=output, is_error=proc.returncode != 0)

