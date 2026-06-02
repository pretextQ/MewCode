from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mewcode.conversation import (
    ConversationManager,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)
from mewcode.memory.auto_memory import MemoryManager
from mewcode.memory.instructions import (
    MAX_INCLUDE_DEPTH,
    load_instructions,
    process_includes,
)
from mewcode.memory.session import (
    RecordType,
    ResumeResult,
    Session,
    SessionManager,
    SessionMeta,
    SessionRecord,
    build_time_gap_message,
    records_to_messages,
    validate_message_chain,
)

# =========================================================================
# A. Instructions (MEWCODE.md)
# =========================================================================

class TestProcessIncludes:
    def test_no_includes(self, tmp_path: Path) -> None:
        content = "line1\nline2\nline3"
        result = process_includes(content, tmp_path, tmp_path)
        assert result == content

    def test_basic_include(self, tmp_path: Path) -> None:
        child = tmp_path / "child.md"
        child.write_text("included content", encoding="utf-8")
        content = "before\n@include ./child.md\nafter"
        result = process_includes(content, tmp_path, tmp_path)
        assert "included content" in result
        assert "before" in result
        assert "after" in result

    def test_recursive_include(self, tmp_path: Path) -> None:
        grandchild = tmp_path / "grandchild.md"
        grandchild.write_text("deep content", encoding="utf-8")
        child = tmp_path / "child.md"
        child.write_text("@include ./grandchild.md", encoding="utf-8")
        content = "@include ./child.md"
        result = process_includes(content, tmp_path, tmp_path)
        assert "deep content" in result

    def test_depth_limit(self, tmp_path: Path) -> None:
        content = "should stop"
        result = process_includes(content, tmp_path, tmp_path, depth=MAX_INCLUDE_DEPTH)
        assert result == content

    def test_path_outside_project_blocked(self, tmp_path: Path) -> None:
        content = "@include ../../etc/passwd"
        result = process_includes(content, tmp_path, tmp_path)
        assert "blocked: path outside project" in result

    def test_file_not_found(self, tmp_path: Path) -> None:
        content = "@include ./nonexistent.md"
        result = process_includes(content, tmp_path, tmp_path)
        assert "skipped: file not found" in result

class TestLoadInstructions:
    def test_single_layer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mewcode_md = tmp_path / "MEWCODE.md"
        mewcode_md.write_text("project instructions", encoding="utf-8")
        result = load_instructions(str(tmp_path))
        assert "project instructions" in result

    def test_multi_layer_priority(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root_md = tmp_path / "MEWCODE.md"
        root_md.write_text("root level", encoding="utf-8")
        dotdir = tmp_path / ".mewcode"
        dotdir.mkdir()
        dot_md = dotdir / "MEWCODE.md"
        dot_md.write_text("dotdir level", encoding="utf-8")
        result = load_instructions(str(tmp_path))
        assert result.index("root level") < result.index("dotdir level")
        assert "\n---\n" in result

    def test_no_files_returns_empty(self, tmp_path: Path) -> None:
        result = load_instructions(str(tmp_path))
        assert result == ""

# =========================================================================
# B. SessionRecord
# =========================================================================

class TestSessionRecord:
    def test_user_message_roundtrip(self) -> None:
        msg = Message(role="user", content="hello world")
        records = SessionRecord.from_message(msg)
        assert len(records) == 1
        assert records[0].type == RecordType.USER
        assert records[0].content == "hello world"

        line = records[0].to_jsonl()
        restored = SessionRecord.from_jsonl(line)
        assert restored is not None
        assert restored.type == RecordType.USER
        assert restored.content == "hello world"

    def test_assistant_with_tool_uses(self) -> None:
        msg = Message(
            role="assistant",
            content="Let me check",
            tool_uses=[
                ToolUseBlock(tool_use_id="t1", tool_name="ReadFile", arguments={"path": "/a"})
            ],
        )
        records = SessionRecord.from_message(msg)
        assert len(records) == 1
        assert records[0].type == RecordType.ASSISTANT
        assert isinstance(records[0].content, list)
        assert records[0].content[0]["type"] == "text"
        assert records[0].content[1]["type"] == "tool_use"

    def test_tool_results_multiple_records(self) -> None:
        msg = Message(
            role="user",
            content="",
            tool_results=[
                ToolResultBlock(tool_use_id="t1", content="result1"),
                ToolResultBlock(tool_use_id="t2", content="result2", is_error=True),
            ],
        )
        records = SessionRecord.from_message(msg)
        assert len(records) == 2
        assert records[0].type == RecordType.TOOL_RESULT
        assert records[0].tool_use_id == "t1"
        assert records[1].is_error is True

    def test_malformed_jsonl_returns_none(self) -> None:
        assert SessionRecord.from_jsonl("{bad json") is None
        assert SessionRecord.from_jsonl('{"type":"unknown","content":"x","timestamp":"2025-01-01T00:00:00"}') is None

    def test_plain_assistant_message(self) -> None:
        msg = Message(role="assistant", content="done")
        records = SessionRecord.from_message(msg)
        assert len(records) == 1
        assert records[0].content == "done"

# =========================================================================
# C. Session & SessionManager
# =========================================================================

class TestSession:
    def test_append_writes_jsonl_and_updates_meta(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / ".mewcode" / "sessions"
        sessions_dir.mkdir(parents=True)
        meta = SessionMeta(id="test_session")
        meta.save(sessions_dir / "test_session.meta")
        jsonl_path = sessions_dir / "test_session.jsonl"

        with open(jsonl_path, "a", encoding="utf-8") as f:
            session = Session("test_session", f, meta, sessions_dir)
            session.append(Message(role="user", content="hello"))
            session.append(Message(role="assistant", content="hi"))

        lines = jsonl_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert meta.message_count == 2
        assert meta.title == "hello"

    def test_title_set_from_first_user_message(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / ".mewcode" / "sessions"
        sessions_dir.mkdir(parents=True)
        meta = SessionMeta(id="test_session")
        jsonl_path = sessions_dir / "test_session.jsonl"

        with open(jsonl_path, "a", encoding="utf-8") as f:
            session = Session("test_session", f, meta, sessions_dir)
            session.append(Message(role="assistant", content="welcome"))
            assert meta.title == ""
            session.append(Message(role="user", content="my first question"))
            assert meta.title == "my first question"

class TestSessionManager:

    def test_create_and_list(self, tmp_path: Path) -> None:
        mgr = SessionManager(str(tmp_path))
        s1 = mgr.create()
        s1.append(Message(role="user", content="test"))
        s1.close()

        s2 = mgr.create()
        s2.append(Message(role="user", content="test2"))
        s2.close()

        metas = mgr.list()
        assert len(metas) == 2
        assert metas[0].last_active >= metas[1].last_active

    def test_delete(self, tmp_path: Path) -> None:
        mgr = SessionManager(str(tmp_path))
        s = mgr.create()
        sid = s.session_id
        s.close()

        assert mgr.delete(sid) is True
        assert mgr.delete(sid) is False
        assert len(mgr.list()) == 0

    def test_cleanup_removes_old_sessions(self, tmp_path: Path) -> None:
        mgr = SessionManager(str(tmp_path))
        s = mgr.create()
        s.meta.last_active = datetime.now(timezone.utc) - timedelta(days=31)
        s.meta.save(mgr._sessions_dir / f"{s.session_id}.meta")
        s.close()

        removed = mgr.cleanup(max_age_days=30)
        assert removed == 1
        assert len(mgr.list()) == 0

    def test_create_generates_valid_id(self, tmp_path: Path) -> None:
        mgr = SessionManager(str(tmp_path))
        s = mgr.create()
        assert s.session_id.startswith("session_")
        assert len(s.session_id.split("_")) == 4
        s.close()

# =========================================================================
# D. Message chain validation & resume
# =========================================================================

class TestValidateMessageChain:
    def test_complete_chain(self) -> None:
        now = datetime.now(timezone.utc)
        records = [
            SessionRecord(type=RecordType.USER, content="hi", timestamp=now),
            SessionRecord(
                type=RecordType.ASSISTANT,
                content=[
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "t1", "name": "ReadFile", "input": {}},
                ],
                timestamp=now,
            ),
            SessionRecord(
                type=RecordType.TOOL_RESULT,
                content="file content",
                timestamp=now,
                tool_use_id="t1",
            ),
            SessionRecord(type=RecordType.ASSISTANT, content="done", timestamp=now),
        ]
        assert validate_message_chain(records) == 4

    def test_truncate_at_missing_tool_result(self) -> None:
        now = datetime.now(timezone.utc)
        records = [
            SessionRecord(type=RecordType.USER, content="hi", timestamp=now),
            SessionRecord(type=RecordType.ASSISTANT, content="ok", timestamp=now),
            SessionRecord(
                type=RecordType.ASSISTANT,
                content=[
                    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}},
                ],
                timestamp=now,
            ),
        ]
        assert validate_message_chain(records) == 2

    def test_empty_records(self) -> None:
        assert validate_message_chain([]) == 0

class TestRecordsToMessages:
    def test_basic_roundtrip(self) -> None:
        now = datetime.now(timezone.utc)
        records = [
            SessionRecord(type=RecordType.USER, content="hello", timestamp=now),
            SessionRecord(type=RecordType.ASSISTANT, content="world", timestamp=now),
        ]
        messages = records_to_messages(records)
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"

    def test_tool_result_grouping(self) -> None:
        now = datetime.now(timezone.utc)
        records = [
            SessionRecord(type=RecordType.USER, content="go", timestamp=now),
            SessionRecord(
                type=RecordType.ASSISTANT,
                content=[
                    {"type": "tool_use", "id": "t1", "name": "ReadFile", "input": {}},
                    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}},
                ],
                timestamp=now,
            ),
            SessionRecord(
                type=RecordType.TOOL_RESULT, content="r1", timestamp=now, tool_use_id="t1"
            ),
            SessionRecord(
                type=RecordType.TOOL_RESULT, content="r2", timestamp=now, tool_use_id="t2"
            ),
            SessionRecord(type=RecordType.ASSISTANT, content="done", timestamp=now),
        ]
        messages = records_to_messages(records)
        assert len(messages) == 4
        assert messages[0].role == "user"
        assert messages[1].role == "assistant"
        assert len(messages[1].tool_uses) == 2
        assert messages[2].role == "user"
        assert len(messages[2].tool_results) == 2
        assert messages[3].role == "assistant"

    def test_system_prompt_skipped(self) -> None:
        now = datetime.now(timezone.utc)
        records = [
            SessionRecord(type=RecordType.SYSTEM_PROMPT, content="system", timestamp=now),
            SessionRecord(type=RecordType.USER, content="hi", timestamp=now),
        ]
        messages = records_to_messages(records)
        assert len(messages) == 1
        assert messages[0].content == "hi"

class TestSessionResume:
    def test_resume_restores_messages(self, tmp_path: Path) -> None:
        mgr = SessionManager(str(tmp_path))
        s = mgr.create()
        sid = s.session_id
        s.append(Message(role="user", content="hello"))
        s.append(Message(role="assistant", content="hi"))
        s.close()

        result = mgr.resume(sid)
        assert result is not None
        assert len(result.messages) == 2
        assert result.messages[0].content == "hello"
        assert result.messages[1].content == "hi"
        result.session.close()

    def test_resume_nonexistent_returns_none(self, tmp_path: Path) -> None:
        mgr = SessionManager(str(tmp_path))
        assert mgr.resume("nonexistent") is None

    def test_resume_truncates_incomplete_chain(self, tmp_path: Path) -> None:
        mgr = SessionManager(str(tmp_path))
        s = mgr.create()
        sid = s.session_id
        s.append(Message(role="user", content="start"))
        s.append(Message(role="assistant", content="ok"))
        s.append(
            Message(
                role="assistant",
                content="checking",
                tool_uses=[
                    ToolUseBlock(tool_use_id="t1", tool_name="Bash", arguments={"command": "ls"})
                ],
            )
        )
        s.close()

        result = mgr.resume(sid)
        assert result is not None
        assert len(result.messages) == 2
        result.session.close()

# =========================================================================
# E. Time gap message
# =========================================================================

class TestTimeGapMessage:
    def test_no_gap_returns_none(self) -> None:
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        assert build_time_gap_message(recent) is None

    def test_gap_returns_message(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        msg = build_time_gap_message(old)
        assert msg is not None
        assert "代码可能有变更" in msg.content

# =========================================================================
# F. SessionMeta
# =========================================================================

class TestSessionMeta:
    def test_save_and_load(self, tmp_path: Path) -> None:
        meta = SessionMeta(
            id="test_123",
            title="Test session",
            summary="A test",
            message_count=10,
            total_tokens=5000,
        )
        path = tmp_path / "test.meta"
        meta.save(path)

        loaded = SessionMeta.load(path)
        assert loaded is not None
        assert loaded.id == "test_123"
        assert loaded.title == "Test session"
        assert loaded.message_count == 10

    def test_load_invalid_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.meta"
        path.write_text("not json", encoding="utf-8")
        assert SessionMeta.load(path) is None

# =========================================================================
# G. MemoryManager
# =========================================================================

class TestMemoryManager:
    def test_load_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        mgr = MemoryManager(str(tmp_path / "project"))
        assert mgr.load() == ""

    def test_load_merges_user_and_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        user_mem = fake_home / ".mewcode" / "memories.md"
        user_mem.parent.mkdir(parents=True)
        user_mem.write_text("### 用户偏好\n- prefer spaces", encoding="utf-8")

        project_mem = tmp_path / "project" / ".mewcode" / "memories.md"
        project_mem.parent.mkdir(parents=True)
        project_mem.write_text("### 项目知识\n- uses PostgreSQL", encoding="utf-8")

        mgr = MemoryManager(str(tmp_path / "project"))
        result = mgr.load()
        assert "prefer spaces" in result
        assert "uses PostgreSQL" in result

    def test_clear(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        user_mem = fake_home / ".mewcode" / "memories.md"
        user_mem.parent.mkdir(parents=True)
        user_mem.write_text("### 用户偏好\n- something", encoding="utf-8")

        project_mem = tmp_path / "project" / ".mewcode" / "memories.md"
        project_mem.parent.mkdir(parents=True)
        project_mem.write_text("### 项目知识\n- something", encoding="utf-8")

        mgr = MemoryManager(str(tmp_path / "project"))
        mgr.clear()
        assert mgr.load() == ""

    def test_get_display_text_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        mgr = MemoryManager(str(tmp_path / "project"))
        assert "没有任何自动记忆" in mgr.get_display_text()

    def test_write_memories_splits_correctly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

        mgr = MemoryManager(str(tmp_path / "project"))
        mgr._write_memories(
            "### 用户偏好\n- use spaces\n\n"
            "### 纠正反馈\n- use mutex not channel\n\n"
            "### 项目知识\n- uses PostgreSQL\n\n"
            "### 参考资料\n- docs at example.com\n"
        )

        user_content = mgr._user_path.read_text(encoding="utf-8")
        assert "use spaces" in user_content
        assert "use mutex" in user_content
        assert "PostgreSQL" not in user_content

        project_content = mgr._project_path.read_text(encoding="utf-8")
        assert "uses PostgreSQL" in project_content
        assert "docs at example.com" in project_content
        assert "use spaces" not in project_content

# =========================================================================
# H. Conversation inject_long_term_memory
# =========================================================================

class TestConversationInjection:
    def test_inject_long_term_memory(self) -> None:
        conv = ConversationManager()
        conv.inject_environment("env info")
        conv.inject_long_term_memory("project rules", "user prefs")

        assert len(conv.history) == 2
        assert conv.history[0].content == "env info"
        assert "<system-reminder>" in conv.history[1].content
        assert "mewcodeMd" in conv.history[1].content
        assert "project rules" in conv.history[1].content
        assert "autoMemory" in conv.history[1].content
        assert "user prefs" in conv.history[1].content
        assert "currentDate" in conv.history[1].content
        assert conv.ltm_injected is True

    def test_inject_idempotent(self) -> None:
        conv = ConversationManager()
        conv.inject_long_term_memory("rules", "mems")
        conv.inject_long_term_memory("rules2", "mems2")
        assert sum(1 for m in conv.history if "<system-reminder>" in m.content) == 1

    def test_inject_instructions_only(self) -> None:
        conv = ConversationManager()
        conv.inject_long_term_memory("rules", "")
        assert len(conv.history) == 1
        assert "<system-reminder>" in conv.history[0].content
        assert "mewcodeMd" in conv.history[0].content
        assert "rules" in conv.history[0].content

    def test_inject_memories_only(self) -> None:
        conv = ConversationManager()
        conv.inject_long_term_memory("", "mems")
        assert len(conv.history) == 1
        assert "<system-reminder>" in conv.history[0].content
        assert "autoMemory" in conv.history[0].content
        assert "mems" in conv.history[0].content

    def test_inject_nothing(self) -> None:
        conv = ConversationManager()
        conv.inject_long_term_memory("", "")
        assert len(conv.history) == 0
        assert conv.ltm_injected is False

    def test_replace_history_resets_ltm(self) -> None:
        conv = ConversationManager()
        conv.inject_long_term_memory("rules", "mems")
        assert conv.ltm_injected is True
        conv.replace_history([])
        assert conv.ltm_injected is False

# =========================================================================
# I. Memory extraction prompt construction
# =========================================================================

class TestMemoryExtraction:
    def test_extraction_prompt_contains_categories(self, tmp_path: Path) -> None:
        from mewcode.memory.auto_memory import MEMORY_EXTRACTION_PROMPT

        assert "用户偏好" in MEMORY_EXTRACTION_PROMPT
        assert "纠正反馈" in MEMORY_EXTRACTION_PROMPT
        assert "项目知识" in MEMORY_EXTRACTION_PROMPT
        assert "参考资料" in MEMORY_EXTRACTION_PROMPT
        assert "不要重复添加" in MEMORY_EXTRACTION_PROMPT
