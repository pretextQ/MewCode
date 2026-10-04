from __future__ import annotations

import copy
import locale
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".tox", ".mypy_cache"}

MAX_OUTPUT_CHARS = 10000

ToolCategory = Literal["read", "write", "command"]

ParamsT = TypeVar("ParamsT", bound=BaseModel)


def detect_encoding(path: Path) -> str:
    """编码探测：BOM → utf-8 → locale 编码 → win32 ANSI 代码页。

    只嗅探前 64KB。UTF-8 模式下 locale.getpreferredencoding 也返回 utf-8，
    因此 win32 额外尝试 mbcs（真实 ANSI 代码页，中文系统为 GBK）。
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(65536)
    except OSError:
        return "utf-8"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    import os

    candidates = [locale.getpreferredencoding(False)]
    if os.name == "nt":
        candidates.append("mbcs")
    for enc in candidates:
        if not enc or enc.lower() == "utf-8":
            continue
        try:
            raw.decode(enc)
            return enc
        except (UnicodeDecodeError, LookupError):
            continue
    return "utf-8"


def read_text_preserve(path: Path) -> str:
    """读取文本：newline='' 保持行尾原样，编码走探测回退。

    全部候选失败时按 utf-8 宽松解码兜底（不可解码字节→U+FFFD），
    不让工具在混合编码文件上直接崩掉。
    """
    enc = detect_encoding(path)
    try:
        with open(path, encoding=enc, newline="") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            return f.read()


def write_text_preserve(path: Path, content: str) -> str:
    """写入文本：newline='' 不做 \\n→os.linesep 转换，避免 LF 文件被
    整体改写成 CRLF；已存在文件沿用其探测编码。

    返回实际使用的编码名。
    """
    enc = detect_encoding(path) if path.exists() else "utf-8"
    with open(path, "w", encoding=enc, newline="") as f:
        f.write(content)
    return enc


@dataclass
class ToolResult:
    output: str
    is_error: bool = False


class Tool(ABC, Generic[ParamsT]):
    name: str
    description: str
    params_model: type[ParamsT]
    category: ToolCategory = "read"
    is_concurrency_safe: bool = False
    is_system_tool: bool = False
    should_defer: bool = False
    _work_dir: str | None = None

    @property
    def is_read_only(self) -> bool:
        return self.category == "read"


    def bind(self, work_dir: str) -> Tool:
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
    async def execute(self, params: ParamsT) -> ToolResult: ...


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


StreamEvent = (
    TextDelta
    | ThinkingDelta
    | ThinkingComplete
    | ToolCallStart
    | ToolCallDelta
    | ToolCallComplete
    | StreamEnd
)
