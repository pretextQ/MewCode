
from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from mewcode.processes import create_shell_process, kill_process_tree, release_process
from mewcode.shell import shell_description
from mewcode.tools.base import Tool, ToolResult

MAX_TIMEOUT = 600


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
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            if proc is not None:
                await kill_process_tree(proc)
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except asyncio.CancelledError:
            if proc is not None:
                await kill_process_tree(proc)
            raise
        except Exception as e:
            return ToolResult(output=f"Error executing command: {e}", is_error=True)
        finally:
            release_process(proc)

        parts: list[str] = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout.decode(errors='replace')}")
        if stderr:
            parts.append(f"STDERR:\n{stderr.decode(errors='replace')}")
        if not parts:
            parts.append("(no output)")

        output = "\n".join(parts)
        return ToolResult(output=output, is_error=proc.returncode != 0)

