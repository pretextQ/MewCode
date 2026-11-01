"""A view of existing task/tool facts, never a second execution state machine."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from functools import partial
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static, TabbedContent, TabPane
from textual.worker import Worker

from mewcode.tui.git import display_text, workspace_changes
from mewcode.tui.tools import ToolCallBlock, _tool_title


@dataclass
class ExecutionRecord:
    tool_id: str
    tool_name: str
    arguments: dict[str, Any]
    state: str = "执行中"
    output: str = ""
    is_error: bool = False
    elapsed: float | None = None


class ExecutionHistory:
    def __init__(self) -> None:
        self.records: deque[ExecutionRecord] = deque(maxlen=100)
        self.current: dict[str, ExecutionRecord] = {}
        self.request = ""
        self.revision = 0

    def begin(self, request: str) -> None:
        self.current.clear()
        if request:
            self.request = display_text(request[:500])
        self.revision += 1

    def started(self, tool_id: str, name: str, arguments: dict[str, Any]) -> None:
        # Never retain WriteFile content, arbitrary MCP payloads or unlimited arguments.
        target: dict[str, Any] = {
            key: str(arguments[key])[:1000] for key in ("command", "file_path", "pattern") if key in arguments
        }
        record = ExecutionRecord(tool_id, name, target)
        self.records.append(record)
        self.current[tool_id] = record
        if len(self.current) > 100:
            self.current.pop(next(iter(self.current)))
        self.revision += 1

    def finished(self, tool_id: str, output: str, is_error: bool, elapsed: float) -> None:
        if record := self.current.pop(tool_id, None):
            record.output = display_text(output[:12000]) + ("\n… 输出已截断" if len(output) > 12000 else "")
            record.is_error = is_error
            record.elapsed = elapsed
            record.state = "工具失败" if is_error else "工具已结束"
            self.revision += 1

    def interrupted(self, state: str = "已停止 / 结果未知") -> None:
        for record in self.current.values():
            record.state = state
        self.current.clear()
        self.revision += 1


class DetailsPanel(Vertical):
    def __init__(self) -> None:
        super().__init__(id="details-panel")
        self._facts_lock = asyncio.Lock()
        self._last_snapshot: tuple[Any, ...] = ()
        self._git_busy = False
        self._git_worker: Worker[None] | None = None
        self._shown_records: dict[int, ToolCallBlock] = {}
        self._shown_states: dict[int, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("当前任务", id="details-title", markup=False)
        yield Static("", id="task-facts", markup=False)
        with TabbedContent():
            with TabPane("文件改动", id="changes-tab"):
                with Horizontal(id="git-actions"):
                    yield Button("刷新状态", id="git-refresh")
                    yield Button("读取 diff", id="git-diff")
                with VerticalScroll(id="changes-scroll"):
                    yield Static("按刷新读取 Git 状态。", id="workspace-changes", markup=False)
            with TabPane("执行记录", id="execution-tab"), VerticalScroll(id="execution-scroll"):
                yield Static("", id="execution-facts", markup=False)
                yield Vertical(id="execution-records")
        yield Static("F2 显示 / 隐藏详情", id="details-footer", markup=False)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in {"git-refresh", "git-diff"}:
            event.stop()
            # A worker leaves F2, Esc, resize and permission responses responsive.
            self._git_worker = self.run_worker(
                partial(self.refresh_git, event.button.id == "git-diff"), group="git-read", exclusive=False,
            )

    async def refresh_git(self, include_diff: bool = False) -> None:
        if self._git_busy:
            return
        self._git_busy = True
        for button in self.query(Button):
            button.disabled = True
        output = self.query_one("#workspace-changes", Static)
        output.update("读取 Git…")
        app: Any = self.app
        work_dir = app.agent.work_dir if app.agent else ""
        try:
            result = await workspace_changes(work_dir, include_diff)
            # A worktree switch while awaiting cannot label the old result as current.
            if app.agent and app.agent.work_dir == work_dir:
                output.update(Text(result))
            else:
                output.update("工作区已切换，请刷新。")
        finally:
            self._git_busy = False
            for button in self.query(Button):
                button.disabled = False

    async def refresh_facts(self, history: ExecutionHistory, task_manager: Any, agent: Any, mcp: str) -> None:
        async with self._facts_lock:
            await self._refresh_facts(history, task_manager, agent, mcp)

    async def _refresh_facts(self, history: ExecutionHistory, task_manager: Any, agent: Any, mcp: str) -> None:
        if not self.display:
            return
        tasks = task_manager.list_tasks()[-20:]
        lines = ["当前请求：" + (history.request or "无当前请求；历史任务信息未知。")]
        lines.extend(f"{task.id} · {task.status} · {display_text(task.name[:100])}" for task in tasks)
        if not tasks:
            lines.append("无结构化任务 / 计划数据。")
        task_lines = list(lines)
        lines.extend([mcp, "验证覆盖：未知；工具结束不代表验证通过。"])
        if agent:
            lines.append(f"工作区：{display_text(agent.work_dir)}")
            lines.append(
                f"已知累计 token（Agent 统计）：输入 {agent.total_input_tokens} / 输出 {agent.total_output_tokens}"
            )
        snapshot = (history.revision, *lines)
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot
        self.query_one("#task-facts", Static).update(Text("\n".join(task_lines)))
        self.query_one("#execution-facts", Static).update(Text("\n".join(lines[len(task_lines):])))
        records = self.query_one("#execution-records", Vertical)
        ids = {id(record) for record in history.records}
        for key in list(self._shown_records):
            if key not in ids:
                await self._shown_records.pop(key).remove()
                self._shown_states.pop(key, None)
        if not history.records and not records.children:
            await records.mount(Static("本次打开应用以来无执行记录；历史耗时未知。", id="no-executions", markup=False))
        if history.records:
            await records.query("#no-executions").remove()
        for record in history.records:
            key = id(record)
            block = self._shown_records.get(key)
            if block is None:
                block = ToolCallBlock(record.tool_name, record.arguments, classes="tool-block")
                self._shown_records[key] = block
                await records.mount(block)
            if self._shown_states.get(key) == record.state:
                continue
            self._shown_states[key] = record.state
            if record.elapsed is not None:
                block.set_result(record.output, record.is_error, record.elapsed)
            elif record.state != "执行中":
                block.set_cancelled()
            block.tooltip = Text(f"{_tool_title(record.tool_name, record.arguments)} · {record.state}")
