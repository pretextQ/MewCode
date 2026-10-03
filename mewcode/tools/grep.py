
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from pydantic import BaseModel, Field

from mewcode.tools.base import SKIP_DIRS, Tool, ToolResult

GREP_MAX_FILE_BYTES = 1_000_000
GREP_MAX_RESULTS = 200


def _scan_sync(base: Path, regex: re.Pattern, glob_pattern: str) -> list[str]:
    results: list[str] = []
    for file_path in sorted(base.glob(glob_pattern)):
        if len(results) >= GREP_MAX_RESULTS:
            break
        if not file_path.is_file():
            continue
        if any(part in SKIP_DIRS for part in file_path.parts):
            continue
        try:
            if file_path.stat().st_size > GREP_MAX_FILE_BYTES:
                continue
            raw = file_path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            # 二进制文件：整体跳过，替代 errors="ignore" 的伪匹配
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_num, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                rel = file_path.relative_to(base)
                results.append(f"{rel}:{line_num}:{line}")
                if len(results) >= GREP_MAX_RESULTS:
                    break
    return results


class Params(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="Base directory to search from")
    include: str = Field(default="", description="Glob filter for filenames (e.g. '*.py')")


class Grep(Tool):
    name = "Grep"
    description = "Search file contents using a regex pattern, returning file:line:content matches."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    async def execute(self, params: Params) -> ToolResult:
        base = Path(self._resolve_work_path(params.path))
        if not base.exists():
            return ToolResult(output=f"Error: path not found: {params.path}", is_error=True)

        try:
            regex = re.compile(params.pattern)
        except re.error as e:
            return ToolResult(output=f"Error: invalid regex: {e}", is_error=True)

        glob_pattern = params.include if params.include else "**/*"
        if not glob_pattern.startswith("**/"):
            glob_pattern = "**/" + glob_pattern

        # 全仓库扫描是纯阻塞 I/O，放线程池避免冻结事件循环
        results = await asyncio.to_thread(_scan_sync, base, regex, glob_pattern)

        if not results:
            return ToolResult(output="No matches found.")
        output = "\n".join(results)
        if len(results) >= GREP_MAX_RESULTS:
            output += f"\n... (results truncated at {GREP_MAX_RESULTS} matches)"
        return ToolResult(output=output)

