
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.cache import FileCache
    from mewcode.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to edit")
    old_string: str = Field(description="The exact string to find and replace (must be unique in file)")
    new_string: str = Field(description="The replacement string")


class EditFile(Tool[Params]):
    name = "EditFile"
    description = (
        "Replace an exact string in a file. The old_string must appear exactly once in the file.\n"
        "You MUST read the file with ReadFile before editing. This tool will fail otherwise."
    )
    params_model = Params
    category = "write"


    def __init__(
        self,
        file_cache: FileCache | None = None,
        file_history: Any = None,
        file_state_cache: FileStateCache | None = None,
    ) -> None:
        self._cache = file_cache
        self.file_history = file_history
        self._state_cache = file_state_cache


    async def execute(self, params: Params) -> ToolResult:
        target = self._resolve_work_path(params.file_path)
        path = Path(target)
        if not path.exists():
            return ToolResult(output=f"Error: file not found: {params.file_path}", is_error=True)

        if self._state_cache:
            resolved = str(path.resolve())
            ok, err_msg = self._state_cache.check(resolved)
            if not ok:
                return ToolResult(output=err_msg, is_error=True)

        try:
            from mewcode.tools.base import read_text_preserve
            content = read_text_preserve(path)
        except Exception as e:
            return ToolResult(output=f"Error reading file: {e}", is_error=True)

        # CRLF 文件：模型通常发送 LF 形式的 old_string，先归一化匹配，
        # 替换后还原行尾，保证未触碰部分字节级不变
        crlf_file = "\r\n" in content
        if crlf_file and "\r\n" not in params.old_string:
            normalized = content.replace("\r\n", "\n")
            count = normalized.count(params.old_string)
        else:
            count = content.count(params.old_string)
        if count == 0:
            return ToolResult(output="Error: old_string not found in file", is_error=True)
        if count > 1:
            return ToolResult(
                output=f"Error: old_string found {count} times, must be unique",
                is_error=True,
            )

        if crlf_file and "\r\n" not in params.old_string:
            new_content = normalized.replace(params.old_string, params.new_string, 1)
            if "\r\n" not in new_content:
                new_content = new_content.replace("\n", "\r\n")
        else:
            new_content = content.replace(params.old_string, params.new_string, 1)

        # track_edit 在门禁与内容校验通过之后：被拒绝的编辑不应进入撤销历史
        if self.file_history is not None:
            self.file_history.track_edit(target)

        try:
            from mewcode.tools.base import write_text_preserve
            write_text_preserve(path, new_content)
            if self._cache is not None:
                self._cache.invalidate(str(path.resolve()))
            if self._state_cache:
                self._state_cache.update(str(path.resolve()))
        except Exception as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)

        return ToolResult(output=f"Successfully edited {params.file_path}")
