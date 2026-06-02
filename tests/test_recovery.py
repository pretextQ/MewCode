from __future__ import annotations

import time

import pytest

from mewcode.context.manager import (
    RECOVERY_FILE_LIMIT,
    RECOVERY_SKILLS_BUDGET,
    RECOVERY_TOKENS_PER_FILE,
    RECOVERY_TOKENS_PER_SKILL,
    RecoveryState,
    _RECOVERY_CHARS_PER_TOKEN,
    build_recovery_attachment,
)

def test_recovery_attachment_empty_when_nothing_recorded():
    assert build_recovery_attachment(None, None) == ""
    assert build_recovery_attachment(RecoveryState(), None) == ""

def test_recovery_attachment_emits_all_sections():
    state = RecoveryState()
    state.record_file_read("/tmp/a.py", "print('hi')\n")
    state.record_skill_invocation("planner", "step 1\nstep 2\n")
    schemas = [
        {"name": "ReadFile", "description": "Read a file and return contents.\nWith line numbers."},
        {"name": "Bash", "description": ""},
    ]
    out = build_recovery_attachment(state, schemas)
    assert "/tmp/a.py" in out
    assert "planner" in out
    assert "- ReadFile — Read a file and return contents." in out
    assert "- Bash" in out
    assert "提示" in out  # closing note section header

def test_recovery_file_limit_and_order():
    state = RecoveryState()
    # Record 7 files spread in time; only 5 newest should appear.
    for i in range(7):
        state.record_file_read(f"/f{i}", "x")
        # Force timestamps so order is deterministic
        rec = state._files[f"/f{i}"]
        rec.timestamp = 1000.0 + i

    files = state.snapshot_files(RECOVERY_FILE_LIMIT)
    assert len(files) == 5
    assert files[0].path == "/f6"  # newest first
    assert files[-1].path == "/f2"

def test_recovery_truncates_per_file():
    huge = "x" * int(RECOVERY_TOKENS_PER_FILE * _RECOVERY_CHARS_PER_TOKEN * 3)
    state = RecoveryState()
    state.record_file_read("/big", huge)
    out = build_recovery_attachment(state, None)
    assert "内容已截断" in out

def test_recovery_skills_budget():
    state = RecoveryState()
    body = "y" * int(RECOVERY_TOKENS_PER_SKILL * _RECOVERY_CHARS_PER_TOKEN)
    for i in range(6):
        name = f"skill-{i}"
        state.record_skill_invocation(name, body)
        rec = state._skills[name]
        rec.timestamp = 1000.0 + i

    out = build_recovery_attachment(state, None)
    emitted = out.count("### skill-")
    # 25K / 5K per skill ⇒ at most 5
    assert 1 <= emitted <= 5
