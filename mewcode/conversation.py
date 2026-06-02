from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolUseBlock:
    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str
    tool_uses: list[ToolUseBlock] = field(default_factory=list)
    tool_results: list[ToolResultBlock] = field(default_factory=list)
    thinking_blocks: list[ThinkingBlock] = field(default_factory=list)


@dataclass
class ConversationManager:
    history: list[Message] = field(default_factory=list)
    env_injected: bool = field(default=False, init=False)
    ltm_injected: bool = field(default=False, init=False)
    last_input_tokens: int = field(default=0, init=False)

    def add_user_message(self, content: str) -> None:
        self.history.append(Message(role="user", content=content))

    def add_assistant_message(
        self,
        content: str,
        tool_uses: list[ToolUseBlock] | None = None,
        thinking_blocks: list[ThinkingBlock] | None = None,
    ) -> None:
        self.history.append(
            Message(
                role="assistant",
                content=content,
                tool_uses=tool_uses or [],
                thinking_blocks=thinking_blocks or [],
            )
        )

    def add_system_reminder(self, content: str) -> None:
        self.history.append(
            Message(
                role="user",
                content=f"<system-reminder>\n{content}\n</system-reminder>",
            )
        )

    def add_tool_results_message(self, tool_results: list[ToolResultBlock]) -> None:
        self.history.append(
            Message(role="user", content="", tool_results=tool_results)
        )


    def inject_environment(self, context: str) -> None:
        if not self.env_injected:
            self.history.insert(0, Message(role="user", content=context))
            self.env_injected = True

    def inject_long_term_memory(
        self, instructions: str, memories: str
    ) -> None:
        if self.ltm_injected:
            return
        sections: list[str] = []
        if instructions:
            sections.append(
                "# mewcodeMd\n"
                "Codebase and user instructions are shown below. "
                "Be sure to adhere to these instructions. "
                "IMPORTANT: These instructions OVERRIDE any default behavior "
                "and you MUST follow them exactly as written.\n\n" + instructions
            )
        if memories:
            sections.append("# autoMemory\n" + memories)
        if not sections:
            return
        from datetime import date

        sections.append(f"# currentDate\nToday's date is {date.today().isoformat()}.")
        body = "\n\n".join(sections)
        wrapped = (
            "<system-reminder>\n"
            "As you answer the user's questions, you can use the following context:\n"
            + body
            + "\n\n      IMPORTANT: this context may or may not be relevant to your tasks."
            " You should not respond to this context unless it is highly relevant to your task.\n"
            "</system-reminder>"
        )
        pos = 1 if self.env_injected else 0
        self.history.insert(pos, Message(role="user", content=wrapped))
        self.ltm_injected = True

    def replace_history(self, new_messages: list[Message]) -> None:
        self.history = new_messages
        self.env_injected = False
        self.ltm_injected = False


    def get_messages(self) -> list[Message]:
        return list(self.history)

    def serialize(self, protocol: str = "anthropic") -> list[dict[str, Any]]:
        if protocol == "openai":
            return self._serialize_openai()
        if protocol == "openai-compat":
            return self._serialize_openai_compat()
        return self._serialize_anthropic()

    def _serialize_anthropic(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for m in self.history:
            if m.tool_uses or m.thinking_blocks:
                content: list[dict[str, Any]] = []
                for tb in m.thinking_blocks:
                    content.append({
                        "type": "thinking",
                        "thinking": tb.thinking,
                        "signature": tb.signature,
                    })
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tu in m.tool_uses:
                    content.append({
                        "type": "tool_use",
                        "id": tu.tool_use_id,
                        "name": tu.tool_name,
                        "input": tu.arguments,
                    })
                if not content:
                    content.append({"type": "text", "text": ""})
                result.append({"role": "assistant", "content": content})
            elif m.tool_results:
                content = []
                for tr in m.tool_results:
                    content.append({
                        "type": "tool_result",
                        "tool_use_id": tr.tool_use_id,
                        "content": tr.content,
                        "is_error": tr.is_error,
                    })
                result.append({"role": "user", "content": content})
            else:
                is_reminder = m.content.startswith("<system-reminder>")
                if is_reminder and result and result[-1]["role"] == "user":
                    prev = result[-1]
                    if isinstance(prev["content"], str):
                        prev["content"] = prev["content"] + "\n" + m.content
                    elif isinstance(prev["content"], list):
                        prev["content"].append({"type": "text", "text": m.content})
                else:
                    result.append({"role": m.role, "content": m.content})
        return result

    def _serialize_openai(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for m in self.history:
            if m.tool_uses:
                if m.content:
                    result.append({"role": "assistant", "content": m.content})
                for tu in m.tool_uses:
                    result.append({
                        "type": "function_call",
                        "name": tu.tool_name,
                        "call_id": tu.tool_use_id,
                        "arguments": json.dumps(tu.arguments),
                    })
            elif m.tool_results:
                for tr in m.tool_results:
                    result.append({
                        "type": "function_call_output",
                        "call_id": tr.tool_use_id,
                        "output": tr.content,
                    })
            else:
                result.append({"role": m.role, "content": m.content})
        return result

    def _serialize_openai_compat(self) -> list[dict[str, Any]]:
        """Serialize for OpenAI Chat Completions API format.

        - User messages: {"role": "user", "content": "..."}
        - Assistant text+tool_calls: {"role": "assistant", "content": "...", "tool_calls": [...]}
        - Tool results: {"role": "tool", "tool_call_id": "...", "content": "..."}
        - Thinking blocks are skipped (Chat Completions does not support them).
        """
        result: list[dict[str, Any]] = []
        for m in self.history:
            if m.tool_uses:
                tool_calls = []
                for tu in m.tool_uses:
                    tool_calls.append({
                        "id": tu.tool_use_id,
                        "type": "function",
                        "function": {
                            "name": tu.tool_name,
                            "arguments": json.dumps(tu.arguments),
                        },
                    })
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": tool_calls,
                }
                result.append(msg)
            elif m.tool_results:
                for tr in m.tool_results:
                    result.append({
                        "role": "tool",
                        "tool_call_id": tr.tool_use_id,
                        "content": tr.content,
                    })
            else:
                result.append({"role": m.role, "content": m.content})
        return result
