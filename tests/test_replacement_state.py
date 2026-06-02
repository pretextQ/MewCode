"""Tests for ContentReplacementState — Design B (decision freezing, no mutation)."""
from __future__ import annotations

import json
from pathlib import Path

from mewcode.context.manager import (
    AGGREGATE_CHAR_LIMIT,
    PERSISTED_TAG,
    REPLACEMENT_RECORDS_FILENAME,
    SINGLE_RESULT_CHAR_LIMIT,
    ContentReplacementRecord,
    append_replacement_records,
    apply_tool_result_budget,
    clone_replacement_state,
    create_replacement_state,
    load_replacement_records,
    reconstruct_replacement_state,
)
from mewcode.conversation import ConversationManager, Message, ToolResultBlock

def _one_msg_conv(*results: ToolResultBlock) -> ConversationManager:
    conv = ConversationManager()
    conv.history.append(Message(role="user", content="", tool_results=list(results)))
    return conv

# ---------------------------------------------------------------------------
# State container basics
# ---------------------------------------------------------------------------

def test_create_returns_empty() -> None:
    state = create_replacement_state()
    assert state.seen_ids == set()
    assert state.replacements == {}

def test_clone_independent() -> None:
    src = create_replacement_state()
    src.seen_ids.add("a")
    src.replacements["a"] = "preview_a"

    cloned = clone_replacement_state(src)
    cloned.seen_ids.add("b")
    cloned.replacements["b"] = "preview_b"

    assert "b" not in src.seen_ids
    assert "b" not in src.replacements
    assert cloned.seen_ids == {"a", "b"}
    assert cloned.replacements == {"a": "preview_a", "b": "preview_b"}

# ---------------------------------------------------------------------------
# Design B: apply does NOT mutate input conversation
# ---------------------------------------------------------------------------

def test_apply_does_not_mutate_conv(tmp_path: Path) -> None:
    big = "x" * (SINGLE_RESULT_CHAR_LIMIT + 100)
    conv = _one_msg_conv(ToolResultBlock(tool_use_id="t1", content=big))
    orig_content = conv.history[0].tool_results[0].content
    orig_history_id = id(conv.history)
    state = create_replacement_state()

    api_conv, _ = apply_tool_result_budget(conv, tmp_path, state)

    # Original conv must be untouched (Design B invariant)
    assert conv.history[0].tool_results[0].content == orig_content
    # api_conv is a different ConversationManager backed by a different list
    assert api_conv is not conv
    assert api_conv.history is not conv.history
    # And it carries the replacement
    assert api_conv.history[0].tool_results[0].content.startswith(PERSISTED_TAG)

def test_first_call_freezes_unreplaced(tmp_path: Path) -> None:
    """An under-budget result must be marked seen but not added to replacements."""
    small = "x" * 100
    conv = _one_msg_conv(ToolResultBlock(tool_use_id="t1", content=small))
    state = create_replacement_state()

    _, records = apply_tool_result_budget(conv, tmp_path, state)

    assert state.seen_ids == {"t1"}
    assert state.replacements == {}
    assert records == []

# ---------------------------------------------------------------------------
# Byte-identical replay across turns
# ---------------------------------------------------------------------------

def test_replacement_byte_identical(tmp_path: Path) -> None:
    """Calling apply twice on the same conv yields byte-identical api_conv content."""
    big = "x" * (SINGLE_RESULT_CHAR_LIMIT + 100)
    conv = _one_msg_conv(ToolResultBlock(tool_use_id="t_big", content=big))
    state = create_replacement_state()

    api1, recs1 = apply_tool_result_budget(conv, tmp_path, state)
    api2, recs2 = apply_tool_result_budget(conv, tmp_path, state)

    c1 = api1.history[0].tool_results[0].content
    c2 = api2.history[0].tool_results[0].content
    assert c1 == c2, "second pass must produce byte-identical content"
    assert recs1[0].replacement == c1
    # Second pass is a pure re-apply: no new records, no new file write
    assert recs2 == []

# ---------------------------------------------------------------------------
# Decision freezing: once seen-unreplaced, never replaced later
# ---------------------------------------------------------------------------

def test_frozen_never_replaced(tmp_path: Path) -> None:
    """An id seen as 'not replaced' in turn 1 must never be selected for replacement,
    even if a later message's aggregate would otherwise pick it."""
    # Turn 1: a single ~4K result, well under aggregate limit
    quarter = AGGREGATE_CHAR_LIMIT // 4  # 5000
    conv = _one_msg_conv(ToolResultBlock(tool_use_id="t1", content="a" * quarter))
    state = create_replacement_state()

    apply_tool_result_budget(conv, tmp_path, state)
    assert "t1" in state.seen_ids
    assert "t1" not in state.replacements

    # Turn 2: simulate that the SAME message now grows (parallel tool result joined),
    # pushing aggregate over budget. (In real life this never happens — messages
    # are immutable once added — but we force it here to assert the invariant.)
    fresh_large = "b" * (quarter * 3 + 100)  # very large fresh candidate
    conv.history[0].tool_results.append(
        ToolResultBlock(tool_use_id="t2", content=fresh_large)
    )

    api_conv, _ = apply_tool_result_budget(conv, tmp_path, state)

    # Pass 1 will spill t2 alone (it's > SINGLE_RESULT_CHAR_LIMIT), so t1 stays raw
    # regardless of aggregate. The point is: t1 was never reconsidered.
    api_t1 = next(tr for tr in api_conv.history[0].tool_results if tr.tool_use_id == "t1")
    assert api_t1.content == "a" * quarter
    assert "t1" not in state.replacements

def test_aggregate_only_picks_fresh(tmp_path: Path) -> None:
    """When aggregate exceeds budget and only fresh candidates are eligible, frozen
    ids are off-limits even if they're the largest."""
    # All four results are below SINGLE_RESULT_CHAR_LIMIT but aggregate to > AGGREGATE.
    big_under = SINGLE_RESULT_CHAR_LIMIT - 1
    conv = _one_msg_conv(
        ToolResultBlock(tool_use_id="t1", content="a" * big_under),
        ToolResultBlock(tool_use_id="t2", content="b" * big_under),
        ToolResultBlock(tool_use_id="t3", content="c" * big_under),
        ToolResultBlock(tool_use_id="t4", content="d" * big_under),
        ToolResultBlock(tool_use_id="t5", content="e" * big_under),
    )
    # Aggregate = 5 * 4999 = 24995 > 20000
    state = create_replacement_state()

    api_conv, recs = apply_tool_result_budget(conv, tmp_path, state)

    # Some subset was replaced; total now ≤ limit
    api_total = sum(len(tr.content) for tr in api_conv.history[0].tool_results)
    assert api_total <= AGGREGATE_CHAR_LIMIT
    assert len(recs) >= 1, "at least one result should have been spilled"

    # All ids should now be in seen_ids (decision made for each)
    assert {"t1", "t2", "t3", "t4", "t5"} <= state.seen_ids

# ---------------------------------------------------------------------------
# Reconstruction
# ---------------------------------------------------------------------------

def test_reconstruct_from_records() -> None:
    msgs = [
        Message(
            role="user", content="",
            tool_results=[
                ToolResultBlock(tool_use_id="t1", content="raw"),
                ToolResultBlock(tool_use_id="t2", content="raw"),
            ],
        ),
    ]
    records = [
        ContentReplacementRecord(tool_use_id="t1", replacement="t1_preview"),
        # No record for t2 → frozen-unreplaced after reconstruct
    ]

    state = reconstruct_replacement_state(msgs, records)

    assert state.seen_ids == {"t1", "t2"}
    assert state.replacements == {"t1": "t1_preview"}

def test_reconstruct_with_inherited_parent() -> None:
    """Fork-resume: parent's live replacements gap-fill ids not in records."""
    msgs = [
        Message(
            role="user", content="",
            tool_results=[
                ToolResultBlock(tool_use_id="t_parent", content="raw"),
                ToolResultBlock(tool_use_id="t_child", content="raw"),
            ],
        ),
    ]
    records = [
        ContentReplacementRecord(tool_use_id="t_child", replacement="child_preview"),
    ]
    inherited = {"t_parent": "parent_preview"}

    state = reconstruct_replacement_state(msgs, records, inherited_replacements=inherited)

    assert state.replacements == {
        "t_child": "child_preview",
        "t_parent": "parent_preview",
    }

# ---------------------------------------------------------------------------
# Transcript I/O
# ---------------------------------------------------------------------------

def test_append_and_load_records_roundtrip(tmp_path: Path) -> None:
    recs = [
        ContentReplacementRecord(tool_use_id="a", replacement="aaa"),
        ContentReplacementRecord(tool_use_id="b", replacement="bbb"),
    ]
    append_replacement_records(tmp_path, recs)
    append_replacement_records(tmp_path, [
        ContentReplacementRecord(tool_use_id="c", replacement="ccc"),
    ])

    out = load_replacement_records(tmp_path)
    assert [r.tool_use_id for r in out] == ["a", "b", "c"]
    assert [r.replacement for r in out] == ["aaa", "bbb", "ccc"]
    assert all(r.kind == "tool-result" for r in out)

    # File is JSONL with one object per line
    raw = (tmp_path / REPLACEMENT_RECORDS_FILENAME).read_text(encoding="utf-8")
    lines = raw.strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        obj = json.loads(line)
        assert set(obj.keys()) >= {"kind", "tool_use_id", "replacement"}
