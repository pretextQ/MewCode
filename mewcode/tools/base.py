from __future__ import annotations

from abc import ABC, abstractmethod
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}

MAX_OUTPUT_CHARS = 10000

ToolCategory = Literal["read", "write", "command"]


@dataclass
class ToolResult:
    output: str
    is_error: bool = False


class Tool(ABC):
    name: str
    description: str
    params_model: type[BaseModel]
    category: ToolCategory = "read"
    is_concurrency_safe: bool = False
    is_system_tool: bool = False
    should_defer: bool = False
    _work_dir: str | None = None

    @property
    def is_read_only(self) -> bool:
        return self.category == "read"


    def bind(self, work_dir: str) -> "Tool":
        """返回绑定 work_dir 的浅拷贝代理：相对路径与子进程 cwd 以 work_dir
        为基准（in-process 子代理运行在 worktree 时使用）。未绑定工具保持
        原有进程 CWD 行为。"""
        proxy = copy.copy(self)
        proxy._work_dir = work_dir
        return proxy

    def _resolve_work_path(self, path: str) -> str:
        """相对路径解析到绑定的 work_dir；绝对路径或未绑定时原样返回。"""
        if not path or not self._work_dir:
            return path
        p = Path(path)
        if p.is_absolute():
            return path
        return str(Path(self._work_dir) / p)

    def get_schema(self) -> dict[str, Any]:
        schema = self.params_model.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }

    @abstractmethod
    async def execute(self, params: BaseModel) -> ToolResult: ...


# --- 流式事件 ---


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallStart:
    tool_name: str
    tool_id: str


@dataclass
class ToolCallDelta:
    text: str


@dataclass
class ToolCallComplete:
    tool_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ThinkingDelta:
    text: str


@dataclass
class ThinkingComplete:
    thinking: str
    signature: str


@dataclass
class StreamEnd:
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    # API 返回的 prompt cache 用量。Anthropic 把缓存前缀 token 分为
    # "read"（cache 命中，按 10% 计费）和 "creation"（cache 写入）。
    # input_tokens 已排除这两部分，因此实际 prompt 大小 =
    # input + cache_read + cache_creation。OpenAI 系列只暴露
    # cache_read（通过 *_tokens_details.cached_tokens），没有 creation
    # 计数，所以 cache_creation 在那边始终为 0。
    cache_read: int = 0
    cache_creation: int = 0


StreamEvent = TextDelta | ThinkingDelta | ThinkingComplete | ToolCallStart | ToolCallDelta | ToolCallComplete | StreamEnd
