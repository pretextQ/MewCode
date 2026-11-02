from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mewcode.atomic import atomic_write_text
from mewcode.conversation import ConversationManager, Message
from mewcode.memory.typed_view import MANIFEST, entries, memory_transaction, sync_view

log = logging.getLogger(__name__)

USER_MEMORIES_RELPATH = ".mewcode/memories.md"
PROJECT_MEMORIES_RELPATH = ".mewcode/memories.md"

MEMORY_EXTRACTION_PROMPT = """\
你是一个记忆提取助手。分析下面的对话，提取值得长期记忆的信息，更新 memories.md。

分类规则：
- **用户偏好**：用户的编码习惯和风格要求（如缩进、命名规范、语言偏好）
- **纠正反馈**：用户明确指出的错误和正确做法
- **项目知识**：当前项目的具体技术信息（技术栈、目录结构、部署方式）
- **参考资料**：外部链接和文档地址

规则：
1. 已有相同含义的条目不要重复添加
2. 没有值得记忆的内容，该分类下留空（不要写任何条目，不要写占位符）
3. 每条记忆用一行 `- ` 开头，必须是具体内容，不要用 `...` 占位
4. 只输出新增条目；更正旧知识时追加明确的纠正条目，不要删除或回显已有内容

输出格式（严格遵守，没有内容的分类下不写任何条目）：
### 用户偏好
- 用户偏好简洁代码风格

### 纠正反馈

### 项目知识
- 项目使用 PostgreSQL 15

### 参考资料

不要输出任何其他内容，不要调用任何工具。"""

_USER_LEVEL_HEADERS = {"用户偏好", "纠正反馈"}
_PROJECT_LEVEL_HEADERS = {"项目知识", "参考资料"}


class MemoryManager:
    def __init__(self, project_root: str) -> None:
        self._user_path = Path.home() / USER_MEMORIES_RELPATH
        self._project_path = Path(project_root).resolve() / PROJECT_MEMORIES_RELPATH
        self._last_extraction_msg_count = 0
        self.view_errors: list[str] = []
        if any(p.exists() or (p.parent / "memory" / MANIFEST).exists() for p in self._sources()):
            self.refresh_derived()

    def _sources(self) -> dict[Path, set[str]]:
        sources = {self._user_path: {"user", "feedback"}}
        sources.setdefault(self._project_path, set()).update({"project", "reference"})
        return sources

    def _refresh_locked(self) -> None:
        self.view_errors.clear()
        for source, kinds in self._sources().items():
            try:
                sync_view(source, kinds)
            except Exception as exc:
                self.view_errors.append(f"{source}: {type(exc).__name__}")
                log.warning("Derived memory view refresh failed for %s (%s); flat source retained",
                            source, type(exc).__name__)

    def refresh_derived(self) -> None:
        try:
            with memory_transaction(self._sources()):
                self._refresh_locked()
        except Exception as exc:
            self.view_errors = [type(exc).__name__]
            log.warning("Derived memory refresh could not acquire its transaction (%s)", type(exc).__name__)

    def injected_keys(self, conversation: ConversationManager) -> set[str]:
        if not conversation.ltm_injected:
            # The Agent will inject this complete flat source before its first request.
            return set(entries(self.load()))
        text = "\n".join(m.content.split("# autoMemory\n", 1)[1].split("\n\n# currentDate", 1)[0]
                         for m in conversation.history if m.role == "user"
                         and m.content.startswith("<system-reminder>") and "# autoMemory\n" in m.content)
        return set(entries(text))


    @property
    def user_path(self) -> Path:
        return self._user_path


    @property
    def project_path(self) -> Path:
        return self._project_path

    @property
    def user_mem_dir(self) -> Path:
        """User-level memory directory (~/.mewcode/memory/).

        This is where .md memory files with frontmatter (type user/feedback)
        live. Distinct from ``user_path`` which points at the flat
        ``memories.md`` file.
        """
        return Path.home() / ".mewcode" / "memory"

    @property
    def project_mem_dir(self) -> Path:
        """Project-level memory directory (<project>/.mewcode/memory/).

        This is where .md memory files with frontmatter (type
        project/reference) live. Distinct from ``project_path`` which
        points at the flat ``memories.md`` file.
        """
        return self._project_path.parent / "memory"

    def load(self) -> str:
        sections: list[str] = []

        if self._user_path.exists():
            content = self._user_path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(content)

        if self._project_path.exists():
            content = self._project_path.read_text(encoding="utf-8").strip()
            if content:
                sections.append(content)

        return "\n\n".join(sections)

    def reset_extraction_cursor(self) -> None:
        """auto_compact 整体替换 history 后调用：旧的消息计数已失义
        （计数器可能大于新 history 长度导致提取永久停摆），重置为 0。"""
        self._last_extraction_msg_count = 0

    async def extract(
        self,
        client: Any,
        conversation: ConversationManager,
        protocol: str,
    ) -> None:
        from mewcode.tools.base import StreamEnd, TextDelta

        current_memories = self.load()

        recent = conversation.history[self._last_extraction_msg_count :]
        if not recent:
            return

        conv_lines: list[str] = []
        for msg in recent:
            if msg.role == "user" and msg.content:
                conv_lines.append(f"用户: {msg.content}")
            elif msg.role == "assistant" and msg.content:
                conv_lines.append(f"助手: {msg.content}")

        if not conv_lines:
            return

        prompt = (
            f"## 当前 memories.md\n"
            f"{current_memories if current_memories else '(空)'}\n\n"
            f"## 最近对话\n"
            f"{chr(10).join(conv_lines)}\n\n"
            f"请只输出需要新增的分类和条目。已有记忆由程序保留。"
        )

        extract_conv = ConversationManager()
        extract_conv.history = [Message(role="user", content=prompt)]

        collected = ""
        try:
            async for event in client.stream(
                extract_conv, system=MEMORY_EXTRACTION_PROMPT
            ):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    pass
        except Exception:
            return

        collected = collected.strip()
        if not collected:
            self._last_extraction_msg_count = len(conversation.history)
            return

        self._write_memories(collected)
        self._last_extraction_msg_count = len(conversation.history)

    def _write_memories(self, content: str) -> None:
        user_sections: list[str] = []
        project_sections: list[str] = []

        current_header = ""
        current_lines: list[str] = []

        for line in content.split("\n"):
            if line.startswith("### "):
                if current_header:
                    self._assign_section(
                        current_header, current_lines, user_sections, project_sections
                    )
                current_header = line
                current_lines = []
            else:
                current_lines.append(line)

        if current_header:
            self._assign_section(
                current_header, current_lines, user_sections, project_sections
            )

        with memory_transaction(self._sources()):
            for path, additions in ((self._user_path, user_sections), (self._project_path, project_sections)):
                if not additions:
                    continue
                existing = path.read_text(encoding="utf-8") if path.exists() else ""
                sections: dict[str, list[str]] = {}
                for text in (existing, "\n".join(additions)):
                    header = ""
                    for line in text.splitlines():
                        if line.startswith("### "):
                            header = line
                        else:
                            lines = sections.setdefault(header, [])
                            if line not in lines:
                                lines.append(line)
                merged = "\n\n".join(
                    "\n".join(([header] if header else []) + lines).strip()
                    for header, lines in sections.items()
                ).strip() + "\n"
                atomic_write_text(path, merged)
            self._refresh_locked()

    @staticmethod
    def _is_placeholder(line: str) -> bool:
        stripped = line.strip().lstrip("- ").strip()
        return stripped in {"", "...", "…", "无", "暂无", "N/A"}


    @staticmethod
    def _assign_section(
        header: str,
        lines: list[str],
        user_sections: list[str],
        project_sections: list[str],
    ) -> None:
        real_lines = [ln for ln in lines if ln.strip().startswith("- ") and not MemoryManager._is_placeholder(ln)]
        if not real_lines:
            return

        section_text = header + "\n" + "\n".join(real_lines)

        for keyword in _USER_LEVEL_HEADERS:
            if keyword in header:
                user_sections.append(section_text)
                return

        for keyword in _PROJECT_LEVEL_HEADERS:
            if keyword in header:
                project_sections.append(section_text)
                return


    def clear(self) -> None:
        with memory_transaction(self._sources()):
            for path in self._sources():
                if path.exists():
                    atomic_write_text(path, "")
            self._refresh_locked()

    def get_display_text(self) -> str:
        parts: list[str] = []

        if self._user_path.exists():
            content = self._user_path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"[用户级] {self._user_path}\n{content}")

        if self._project_path.exists():
            content = self._project_path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"[项目级] {self._project_path}\n{content}")

        if self.view_errors:
            parts.append("自动记忆源文件已保留，召回索引更新失败；重新启动应用可重试。")
        if not parts:
            return "当前没有任何自动记忆。"

        return "\n\n".join(parts)
