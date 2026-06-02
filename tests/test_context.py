from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from mewcode.context.manager import (
    AGGREGATE_CHAR_LIMIT,
    PERSISTED_TAG,
    SINGLE_RESULT_CHAR_LIMIT,
    CompactCircuitBreaker,
    apply_tool_result_budget,
    build_compact_messages,
    cleanup_tool_results,
    compute_compact_threshold,
    create_replacement_state,
    ensure_session_dir,
    extract_summary,
    make_persisted_preview,
    persist_tool_result,
    should_auto_compact,
)
from mewcode.conversation import (
    ConversationManager,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)

# ---------------------------------------------------------------------------
# persist_tool_result
# ---------------------------------------------------------------------------

class TestPersistToolResult:
    def test_writes_file(self, tmp_path: Path) -> None:
        fp = persist_tool_result("toolu_001", "hello world", tmp_path)
        assert fp.exists()
        assert fp.read_text() == "hello world"

    def test_idempotent(self, tmp_path: Path) -> None:
        persist_tool_result("toolu_002", "first", tmp_path)
        persist_tool_result("toolu_002", "second", tmp_path)
        fp = tmp_path / "toolu_002.txt"
        assert fp.read_text() == "first"

# ---------------------------------------------------------------------------
# make_persisted_preview
# ---------------------------------------------------------------------------

class TestMakePersistedPreview:
    def test_contains_tag_and_path(self, tmp_path: Path) -> None:
        content = "x" * 10_000
        preview = make_persisted_preview(content, tmp_path / "test.txt")
        assert preview.startswith(PERSISTED_TAG)
        assert "test.txt" in preview
        assert "</persisted-output>" in preview

    def test_preview_truncated(self, tmp_path: Path) -> None:
        content = "a" * 5_000
        preview = make_persisted_preview(content, tmp_path / "test.txt")
        lines = preview.split("\n")
        preview_line = [l for l in lines if l.startswith("aaa")]
        assert len(preview_line) == 1
        assert len(preview_line[0]) == 2_000

# ---------------------------------------------------------------------------
# apply_tool_result_budget
# ---------------------------------------------------------------------------

class TestApplyToolResultBudget:
    def test_single_oversized_persisted(self, tmp_path: Path) -> None:
        conv = ConversationManager()
        big_content = "x" * (SINGLE_RESULT_CHAR_LIMIT + 100)
        conv.history.append(
            Message(
                role="user",
                content="",
                tool_results=[
                    ToolResultBlock(
                        tool_use_id="toolu_big",
                        content=big_content,
                    )
                ],
            )
        )
        state = create_replacement_state()

        api_conv, records = apply_tool_result_budget(conv, tmp_path, state)

        tr = api_conv.history[0].tool_results[0]
        assert tr.content.startswith(PERSISTED_TAG)
        assert (tmp_path / "toolu_big.txt").exists()
        assert conv.history[0].tool_results[0].content == big_content  # original untouched
        assert len(records) == 1 and records[0].tool_use_id == "toolu_big"

    def test_under_limit_untouched(self, tmp_path: Path) -> None:
        conv = ConversationManager()
        small_content = "x" * 100
        conv.history.append(
            Message(
                role="user",
                content="",
                tool_results=[
                    ToolResultBlock(tool_use_id="toolu_sm", content=small_content)
                ],
            )
        )
        state = create_replacement_state()

        api_conv, records = apply_tool_result_budget(conv, tmp_path, state)

        tr = api_conv.history[0].tool_results[0]
        assert tr.content == small_content
        assert not (tmp_path / "toolu_sm.txt").exists()
        assert records == []
        assert "toolu_sm" in state.seen_ids
        assert "toolu_sm" not in state.replacements

    def test_aggregate_limit(self, tmp_path: Path) -> None:
        conv = ConversationManager()
        results = []
        for i in range(5):
            results.append(
                ToolResultBlock(
                    tool_use_id=f"toolu_agg_{i}",
                    content="x" * (AGGREGATE_CHAR_LIMIT // 4),
                )
            )
        conv.history.append(Message(role="user", content="", tool_results=results))
        state = create_replacement_state()

        api_conv, _ = apply_tool_result_budget(conv, tmp_path, state)

        total = sum(len(tr.content) for tr in api_conv.history[0].tool_results)
        assert total <= AGGREGATE_CHAR_LIMIT
        # Original untouched
        orig_total = sum(len(tr.content) for tr in conv.history[0].tool_results)
        assert orig_total == 5 * (AGGREGATE_CHAR_LIMIT // 4)

    def test_already_persisted_skipped(self, tmp_path: Path) -> None:
        conv = ConversationManager()
        persisted_content = f"{PERSISTED_TAG}\nalready persisted\n</persisted-output>"
        conv.history.append(
            Message(
                role="user",
                content="",
                tool_results=[
                    ToolResultBlock(tool_use_id="toolu_done", content=persisted_content)
                ],
            )
        )
        state = create_replacement_state()

        api_conv, _ = apply_tool_result_budget(conv, tmp_path, state)

        tr = api_conv.history[0].tool_results[0]
        assert tr.content == persisted_content
        # An external-pre-tagged result is recorded in state.replacements too,
        # so subsequent re-applies stay byte-identical.
        assert state.replacements["toolu_done"] == persisted_content

# ---------------------------------------------------------------------------
# compute_compact_threshold
# ---------------------------------------------------------------------------

class TestComputeCompactThreshold:
    def test_auto_threshold(self) -> None:
        assert compute_compact_threshold(200_000) == 167_000

    def test_manual_threshold(self) -> None:
        assert compute_compact_threshold(200_000, manual=True) == 177_000

    def test_smaller_window(self) -> None:
        assert compute_compact_threshold(128_000) == 95_000

# ---------------------------------------------------------------------------
# should_auto_compact
# ---------------------------------------------------------------------------

class TestShouldAutoCompact:
    def test_below_threshold(self) -> None:
        assert not should_auto_compact(100_000, 200_000)

    def test_at_threshold(self) -> None:
        assert should_auto_compact(167_000, 200_000)

    def test_above_threshold(self) -> None:
        assert should_auto_compact(180_000, 200_000)

# ---------------------------------------------------------------------------
# extract_summary
# ---------------------------------------------------------------------------

class TestExtractSummary:
    def test_extracts_between_tags(self) -> None:
        output = "<analysis>blah</analysis>\n<summary>\nthe summary\n</summary>"
        assert extract_summary(output) == "the summary"

    def test_no_tags_returns_full(self) -> None:
        output = "no tags here"
        assert extract_summary(output) == output

    def test_only_summary_tag(self) -> None:
        output = "<summary>just this</summary>"
        assert extract_summary(output) == "just this"

# ---------------------------------------------------------------------------
# CompactCircuitBreaker
# ---------------------------------------------------------------------------

class TestCompactCircuitBreaker:
    def test_starts_closed(self) -> None:
        breaker = CompactCircuitBreaker()
        assert not breaker.is_open()

    def test_opens_after_max_failures(self) -> None:
        breaker = CompactCircuitBreaker(max_failures=3)
        breaker.record_failure()
        breaker.record_failure()
        assert not breaker.is_open()
        breaker.record_failure()
        assert breaker.is_open()

    def test_success_resets(self) -> None:
        breaker = CompactCircuitBreaker(max_failures=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        assert not breaker.is_open()
        breaker.record_failure()
        assert not breaker.is_open()

# ---------------------------------------------------------------------------
# build_compact_messages
# ---------------------------------------------------------------------------

class TestBuildCompactMessages:
    def test_basic_structure(self) -> None:
        msgs = build_compact_messages("the summary")
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert "[摘要]" in msgs[0].content
        assert "the summary" in msgs[0].content
        assert msgs[1].role == "assistant"
        assert "ReadFile" in msgs[1].content

# ---------------------------------------------------------------------------
# Session directory management
# ---------------------------------------------------------------------------

class TestSessionDir:
    def test_ensure_creates_dir(self, tmp_path: Path) -> None:
        session_dir = ensure_session_dir(str(tmp_path))
        assert session_dir.exists()
        assert session_dir.is_dir()

    def test_cleanup(self, tmp_path: Path) -> None:
        session_dir = ensure_session_dir(str(tmp_path))
        (session_dir / "test.txt").write_text("data")
        assert len(list(session_dir.iterdir())) == 1

        cleanup_tool_results(session_dir)
        assert session_dir.exists()
        assert len(list(session_dir.iterdir())) == 0
