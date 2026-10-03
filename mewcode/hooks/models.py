from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mewcode.hooks.conditions import ConditionGroup


@dataclass
class Action:
    type: str
    command: str = ""
    message: str = ""
    url: str = ""
    method: str = "POST"
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    prompt: str = ""
    timeout: int = 30


@dataclass
class ActionResult:
    output: str = ""
    success: bool = True


@dataclass
class Hook:
    id: str
    event: str
    action: Action
    condition: ConditionGroup | None = None
    reject: bool = False
    once: bool = False
    async_exec: bool = False
    executed: bool = False


    def should_run(self) -> bool:
        if self.once and self.executed:
            return False
        return True


    def mark_executed(self) -> None:
        self.executed = True


@dataclass
class HookContext:
    event_name: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    file_path: str = ""
    message: str = ""
    error: str = ""

    def get_field(self, name: str) -> str:
        if name == "tool":
            return self.tool_name
        if name == "event":
            return self.event_name
        if name.startswith("args."):
            key = name[5:]
            value = self.tool_args.get(key, "")
            return str(value) if value else ""
        return ""

    def expand(self, template: str) -> str:
        """普通展开（prompt/http 等非 shell 场景）。

        占位符按最长 key 优先替换，防止 $TOOL_ARGS.path 吃掉
        $TOOL_ARGS.path_extra 的前缀；dict/list 值用 JSON 渲染而非 Python repr。
        """
        result = template
        result = result.replace("$EVENT", self.event_name)
        result = result.replace("$TOOL_NAME", self.tool_name)
        result = result.replace("$FILE_PATH", self.file_path)
        result = result.replace("$MESSAGE", self.message)
        result = result.replace("$ERROR", self.error)
        for key in sorted(self.tool_args, key=len, reverse=True):
            value = self.tool_args[key]
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False)
            else:
                rendered = str(value)
            result = result.replace(f"$TOOL_ARGS.{key}", rendered)
        return result

    def expand_shellsafe(self, template: str) -> str:
        """shell 场景展开（POSIX）：所有内插值经 shlex.quote，杜绝注入。

        值的完整上下文应改由 stdin JSON 传入；$FIELD 内插仅为兼容保留。
        """
        import shlex

        result = template
        result = result.replace("$EVENT", shlex.quote(self.event_name))
        result = result.replace("$TOOL_NAME", shlex.quote(self.tool_name))
        result = result.replace("$FILE_PATH", shlex.quote(self.file_path))
        result = result.replace("$MESSAGE", shlex.quote(self.message))
        result = result.replace("$ERROR", shlex.quote(self.error))
        for key in sorted(self.tool_args, key=len, reverse=True):
            value = self.tool_args[key]
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False)
            else:
                rendered = str(value)
            result = result.replace(f"$TOOL_ARGS.{key}", shlex.quote(rendered))
        return result

    def expand_system_only(self, template: str) -> str:
        """shell 场景展开（Windows）：cmd 引号规则不可靠，仅展开系统控制的
        $EVENT/$TOOL_NAME；LLM 可控的值一律不经命令行传递，走 stdin JSON。"""
        result = template
        result = result.replace("$EVENT", self.event_name)
        result = result.replace("$TOOL_NAME", self.tool_name)
        return result

    def to_payload(self) -> dict[str, Any]:
        """stdin JSON 上下文：hook 脚本从这里拿到全部字段。"""
        return {
            "event": self.event_name,
            "tool": self.tool_name,
            "file_path": self.file_path,
            "message": self.message,
            "error": self.error,
            "args": self.tool_args,
        }


class ToolRejectedError(Exception):
    def __init__(self, tool: str, reason: str, hook_id: str) -> None:
        self.tool = tool
        self.reason = reason
        self.hook_id = hook_id
        super().__init__(f"Tool '{tool}' rejected by hook '{hook_id}': {reason}")
