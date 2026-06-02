from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.cache import FileCache


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to write")
    content: str = Field(description="Content to write to the file")


class WriteFile(Tool):
    name = "WriteFile"
    description = "Write content to a file, creating parent directories if needed. Overwrites existing files."
    params_model = Params
    category = "write"


    def __init__(self, file_cache: FileCache | None = None) -> None:
        self._cache = file_cache


    async def execute(self, params: Params) -> ToolResult:
        path = Path(params.file_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params.content, encoding="utf-8")
            if self._cache:
                self._cache.invalidate(str(path.resolve()))
        except Exception as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)
        return ToolResult(output=f"Successfully wrote to {params.file_path}")
