from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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

    @property
    def is_read_only(self) -> bool:
        return self.category == "read"


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


# --- Stream Events ---


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


StreamEvent = TextDelta | ThinkingDelta | ThinkingComplete | ToolCallStart | ToolCallDelta | ToolCallComplete | StreamEnd
