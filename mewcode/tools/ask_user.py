from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolCategory, ToolResult


class QuestionItem(BaseModel):
    type: str = Field(description="Question type: text, radio, select, checkbox")
    name: str = Field(description="Question identifier")
    message: str = Field(description="Question text to display")
    options: list[str] = Field(
        default_factory=list,
        description="Options for radio/select/checkbox types",
    )


class AskUserParams(BaseModel):
    questions: list[QuestionItem] = Field(
        description="List of questions to ask the user"
    )


class AskUserEvent:


    def __init__(
        self,
        questions: list[dict[str, Any]],
        future: asyncio.Future[dict[str, str]],
    ) -> None:
        self.questions = questions
        self.future = future


class AskUserTool(Tool[AskUserParams]):
    name = "AskUserQuestion"
    description = (
        "Ask the user one or more questions when you need information "
        "that cannot be determined from code or context alone. Supports "
        "text input, radio (single select), select, and checkbox (multi select) "
        "question types."
    )
    params_model = AskUserParams
    category: ToolCategory = "read"
    is_system_tool = True
    should_defer = True


    def __init__(self) -> None:
        self._pending_event: AskUserEvent | None = None

    def current_question(self) -> AskUserEvent | None:
        """当前等待用户回答的事件（供 UI 挂弹窗，替代私有字段直读）。"""
        return self._pending_event

    def prepare(self, questions_data: list[dict[str, Any]]) -> AskUserEvent:
        """在工具执行前预创建等待事件。

        await future 期间 agent 不产出任何事件，UI 必须在 ToolUseEvent
        阶段就拿到事件挂弹窗，否则永远轮询不到。
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, str]] = loop.create_future()
        event = AskUserEvent(questions=questions_data, future=future)
        self._pending_event = event
        return event

    async def execute(self, params: AskUserParams) -> ToolResult:
        questions_data = [q.model_dump() for q in params.questions]

        if self._pending_event is None:
            # 无 UI 预创建（非交互路径）时的兜底
            self.prepare(questions_data)
        event = self._pending_event
        if event is None:  # pragma: no cover — prepare() 必然设置 _pending_event
            return ToolResult(output="AskUser failed to prepare event", is_error=True)

        try:
            answers = await asyncio.wait_for(event.future, timeout=300)
        except TimeoutError:
            return ToolResult(
                output="User did not respond within 5 minutes", is_error=True
            )
        finally:
            self._pending_event = None

        lines = []
        for q in params.questions:
            answer = answers.get(q.name, "(no answer)")
            lines.append(f"{q.name}: {answer}")

        return ToolResult(output="\n".join(lines))
