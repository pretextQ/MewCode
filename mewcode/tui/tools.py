from __future__ import annotations

import os
from typing import Any

from textual.binding import Binding
from textual.widgets import Static

from mewcode.tui.git import display_text

MAX_TRUNCATED_LINES = 20
MAX_DETAIL_CHARS = 12000


def _is_subagent_tool(tool_name: str) -> bool:
    return tool_name == "Agent"


def _tool_title(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "ReadFile":
        path = os.path.basename(arguments.get("file_path", ""))
        return f"Read {path}" if path else "Read"
    if tool_name == "WriteFile":
        path = os.path.basename(arguments.get("file_path", ""))
        content = arguments.get("content", "")
        lines = content.count("\n") + 1 if content else 0
        return f"Write {path} ({lines} lines)" if path else "Write"
    if tool_name == "EditFile":
        path = os.path.basename(arguments.get("file_path", ""))
        return f"Edit {path}" if path else "Edit"
    if tool_name == "Bash":
        cmd = arguments.get("command", "")
        short = cmd[:50] + "…" if len(cmd) > 50 else cmd
        return f"Bash: {short}" if short else "Bash"
    if tool_name == "Glob":
        return f"Glob: {arguments.get('pattern', '')}"
    if tool_name == "Grep":
        return f"Grep: {arguments.get('pattern', '')}"
    return tool_name


def _format_detail(tool_name: str, arguments: dict[str, Any], output: str) -> str:
    parts: list[str] = []
    truncated = len(output) > MAX_DETAIL_CHARS
    output = output[:MAX_DETAIL_CHARS]

    if tool_name == "Bash":
        parts.append(f"  IN   {arguments.get('command', '')}")
        parts.append("")
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            parts.append(f"  OUT  {line}")
    elif tool_name in ("ReadFile", "WriteFile", "EditFile"):
        parts.append(f"  {arguments.get('file_path', '')}")
        parts.append("")
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            parts.append(f"  {line}")
        total = output.count("\n") + 1
        if total > MAX_TRUNCATED_LINES:
            parts.append(f"  … ({total - MAX_TRUNCATED_LINES} more lines)")
    else:
        for line in output.splitlines()[:MAX_TRUNCATED_LINES]:
            parts.append(f"  {line}")
        total = output.count("\n") + 1
        if total > MAX_TRUNCATED_LINES:
            parts.append(f"  … ({total - MAX_TRUNCATED_LINES} more lines)")

    if truncated or len(output.splitlines()) > MAX_TRUNCATED_LINES:
        parts.append("  … 输出已截断")
    return display_text("\n".join(parts)[:MAX_DETAIL_CHARS + 200])


class ToolCallBlock(Static, can_focus=True):
    BINDINGS = [Binding("enter,space", "expand_details", "展开工具")]

    def __init__(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(markup=False, **kwargs)
        self.tool_name = tool_name
        self._arguments = arguments
        self._title = display_text(_tool_title(tool_name, arguments))[:240]
        self._full_output = ""
        self._is_error = False
        self._elapsed = 0.0
        self._interrupted_reason = "已停止"
        self._cancelled = False
        self._history_state = ""
        self._collapsed = True
        self._loading = True
        self._render_loading()

    def _render_loading(self) -> None:
        self.update(f"  ● {self._title} …")
        self.add_class("tool-block-loading")

    def set_result(self, output: str, is_error: bool, elapsed: float) -> None:
        self._full_output = display_text(output[:MAX_DETAIL_CHARS])
        if len(output) > MAX_DETAIL_CHARS:
            self._full_output += "\n… 输出已截断"
        self._is_error = is_error
        self._elapsed = elapsed
        self._loading = False
        self._collapsed = True
        self.remove_class("tool-block-loading")
        if is_error:
            self.add_class("tool-block-error")
        self._render_collapsed()

    def set_history(self, output: str | None = None, is_error: bool = False) -> None:
        self._history_state = "历史调用 · 结果未知 · 耗时未知" if output is None else "历史结果 · 耗时未知"
        self.set_result(output or "历史记录未保存结果。", is_error, 0)

    def _header(self) -> str:
        if self._cancelled:
            return f"  ■ {self._title} · {self._interrupted_reason}（无完成结果）"
        if self._history_state:
            return f"  {'✗' if self._is_error else '·'} {self._title} · {self._history_state}"
        return f"  {'✗' if self._is_error else '✓'} {self._title} ({self._elapsed:.1f}s)"

    def _render_collapsed(self) -> None:
        self.update(self._header())

    def _render_expanded(self) -> None:
        detail = _format_detail(self.tool_name, self._arguments, self._full_output)
        self.update(f"{self._header()}\n{detail}")

    def set_cancelled(self, reason: str = "已停止") -> None:
        if self._loading:
            self._interrupted_reason = reason
            self._cancelled = True
            self._loading = False
            self.update(f"  ■ {self._title} · {self._interrupted_reason}（无完成结果）")

    def action_expand_details(self) -> None:
        self.on_click()

    def on_click(self) -> None:
        if self._loading:
            return
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._render_collapsed()
        else:
            self._render_expanded()


class ToolGroupSummary(Static, can_focus=True):
    BINDINGS = [Binding("enter,space", "expand_details", "展开工具组")]


    def __init__(self, count: int, total_elapsed: float, failure_count: int = 0, **kwargs: Any) -> None:
        self._failures = failure_count
        label = f"● 工具已结束 · 失败 {failure_count} ({count} tool uses · {total_elapsed:.1f}s)  (ctrl+o to expand)"
        super().__init__(label, markup=False, **kwargs)
        self._count = count
        self._total = total_elapsed
        self._expanded = False

    def _refresh_display(self) -> None:
        if self._expanded:
            self.update(f"▼ 工具已结束 · 失败 {self._failures} ({self._count} tool uses · {self._total:.1f}s)")
        else:
            self.update(
                f"● 工具已结束 · 失败 {self._failures} ({self._count} tool uses · {self._total:.1f}s)"
                "  (ctrl+o to expand)"
            )

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._refresh_display()
        if self.parent:
            for child in self.parent.children:
                if isinstance(child, ToolCallBlock) and child.tool_name in {"ReadFile", "Glob", "Grep", "ToolSearch"}:
                    child.display = self._expanded

    def action_expand_details(self) -> None:
        self.toggle()

    def on_click(self) -> None:
        self.toggle()


class SubAgentBlock(Static, can_focus=True):
    BINDINGS = [Binding("enter,space", "expand_details", "展开子任务")]

    def __init__(self, agent_type: str, description: str, **kwargs: Any) -> None:
        super().__init__(markup=False, **kwargs)
        self._agent_type = display_text(agent_type[:100]) or "agent"
        self._description = display_text(description[:60]) if description else ""
        self._interrupted_reason = "已停止"
        self._cancelled = False
        self._done = False
        self._is_error = False
        self._elapsed = 0.0
        self._collapsed = True
        self._result_preview = ""
        self._tool_count = 0
        self._render_running()

    def _render_running(self) -> None:
        desc = f"({self._description})" if self._description else ""
        self.update(f"● {self._agent_type}{desc}\n     Running…")

    def set_result(self, output: str, is_error: bool, elapsed: float) -> None:
        self._done = True
        self._is_error = is_error
        self._elapsed = elapsed
        self._result_preview = display_text(output[:300]) if output else ""
        self._render_done()

    def _parse_stats(self, output: str) -> None:
        import re
        m = re.search(r"(\d+)\s+tool", output[:200])
        if m:
            self._tool_count = int(m.group(1))

    def _render_done(self) -> None:
        if self._cancelled:
            self.update(f"■ {self._agent_type} · {self._interrupted_reason}（无完成结果）")
            return
        desc = f"({self._description})" if self._description else ""
        tool_info = f"{self._tool_count} tool uses · " if self._tool_count else ""
        if self._collapsed:
            self.update(
                f"● {self._agent_type}{desc}\n"
                f"    ⎿  {'失败' if self._is_error else '工具已结束'} ({tool_info}{self._elapsed:.1f}s)"
                "  (Enter / Ctrl+O 展开)"
            )
        else:
            self.update(
                f"● {self._agent_type}{desc}\n"
                f"    ⎿  {'失败' if self._is_error else '工具已结束'} ({tool_info}{self._elapsed:.1f}s)\n"
                f"  {self._result_preview}"
            )

    def set_cancelled(self, reason: str = "已停止") -> None:
        if not self._done:
            self._interrupted_reason = reason
            self._cancelled = True
            self._done = True
            self.update(f"■ {self._agent_type} · {self._interrupted_reason}（无完成结果）")

    def action_expand_details(self) -> None:
        self.on_click()

    def on_click(self) -> None:
        if not self._done:
            return
        self._collapsed = not self._collapsed
        self._render_done()
