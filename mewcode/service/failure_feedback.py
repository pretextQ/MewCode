"""Conservative pytest evidence and advisory-only service retry feedback.

Only complete ordinary pytest failure sections are comparable. Unknown output
breaks the streak; baseline runs and different executions never seed it.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .execution import TestOutcome
    from .jobs import JobEvent, JobStore

CONTRACT = "pytest-long-v1"
REMINDER = (
    "Re-diagnosis suggestion: consecutive service verification attempts produced "
    "the same recognized pytest failure evidence. This is a signal, not proof of "
    "no progress. Re-read the failing assertion and relevant implementation, inspect "
    "the diff, and reconsider the root-cause hypothesis before another edit. "
    "Keep the existing permissions, attempt limit and token budget."
)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SUMMARY = re.compile(r"^FAILED (\S+::\S+)(?: - (.*))?$", re.MULTILINE)
_BLOCK = re.compile(r"^_{3,} .+ _{3,}$", re.MULTILINE)
_END = re.compile(r"^(?:=+ )?(\d+) failed(?:, [\w ,.-]+)? in \d+(?:\.\d+)?s(?: \([^\n]+\))?(?: =+)?$")
_ENV = re.compile(r"(?:ModuleNotFoundError|ImportError|No module named|command not found|not recognized as)")
_PYTEST_TMP = re.compile(
    r"(?:[A-Za-z]:)?(?:[/\\][^\s'\"<>:/\\]+)*[/\\]pytest-of-[^/\\\s'\"]+"
    r"[/\\]pytest-\d+(?=[/\\\s'\"]|$)"
)


def pytest_command(command: str) -> bool:
    """Accept a direct pytest executable or python -m pytest, no shell composition."""
    if any(c in command for c in "|&;<>`\n\r") or "$" in command:
        return False
    try:
        args = shlex.split(command.replace("\\", "/"))
    except ValueError:
        return False
    if not args:
        return False
    exe = PurePosixPath(args[0]).name.lower()
    return exe in {"pytest", "pytest.exe"} or bool(
        re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", exe)
        and args[1:3] == ["-m", "pytest"]
    )


def normalize(text: str, work_dir: str) -> str:
    # Preserve numbers, line locations, object values, assertions and path suffixes.
    # Only known worktree roots and pytest's numbered temp namespace are dynamic.
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    lines = []
    root = work_dir.rstrip("/\\")
    for line in text.strip().splitlines():
        # Pytest renders source with indentation or a leading '>'. Keep source
        # literals intact even when they happen to look like a temporary path.
        if not line.startswith(("    ", ">")):
            if root:
                for variant in {root, root.replace("\\", "/"), root.replace("/", "\\")}:
                    line = re.sub(re.escape(variant) + r"(?=[/\\\s'\"]|$)", "<worktree>", line)
            line = _PYTEST_TMP.sub("<pytest-tmp>", line)
        lines.append(line.rstrip())
    return "\n".join(lines)


@dataclass(frozen=True)
class FailureEvidence:
    classification: str
    reason: str
    fingerprint: str = ""
    normalized: str = ""
    failures: int = 0


def identify(outcome: TestOutcome | None, work_dir: str) -> FailureEvidence:
    if outcome is None:
        return FailureEvidence("not_run", "no test command")
    if outcome.timed_out:
        return FailureEvidence("timeout", "runner timeout")
    if outcome.passed:
        return FailureEvidence("passed", "exit zero")
    text = _ANSI.sub("", outcome.output).replace("\r\n", "\n").strip()
    if outcome.exit_code in {126, 127, 9009}:
        return FailureEvidence("environment", "command unavailable")
    if not pytest_command(outcome.command):
        return FailureEvidence("unknown", "unsupported command")
    if _ENV.search(text):
        return FailureEvidence("environment", "possible import/dependency failure")
    if re.search(r"(?:^E\s+Failed: Timeout >|Timeout \(>\d[^\n]*from pytest-timeout)", text, re.MULTILINE):
        return FailureEvidence("timeout", "pytest timeout signal")
    if outcome.exit_code in {3, 4}:
        return FailureEvidence("infrastructure", "pytest internal/usage error")
    if outcome.exit_code != 1 or not text:
        return FailureEvidence("unknown", "no complete ordinary failure result")
    end = _END.fullmatch(text.splitlines()[-1])
    sections = re.split(r"^=+ short test summary info =+$", text, flags=re.MULTILINE)
    if end is None or len(sections) != 2 or re.search(r"\b\d+ errors?\b", text.splitlines()[-1]):
        return FailureEvidence("unknown", "missing/unsupported final summary")
    nodes = _SUMMARY.findall(sections[1])
    if any("..." in node or "…" in node for node, _ in nodes):
        return FailureEvidence("unknown", "possibly truncated node identifier")
    failure_section = re.split(r"^=+ FAILURES =+$", sections[0], flags=re.MULTILINE)
    if len(failure_section) != 2:
        return FailureEvidence("unknown", "missing failure details")
    # Exclude trailing warnings/timing sections; retain captured stdout and stderr.
    details = re.split(r"^=+ (?:warnings summary|slowest \d+ durations) =+$",
                       failure_section[1], maxsplit=1, flags=re.MULTILINE)[0]
    headers = list(_BLOCK.finditer(details))
    blocks = [details[header.start():headers[i + 1].start() if i + 1 < len(headers) else len(details)]
              for i, header in enumerate(headers)]
    # Terminal width changes separator padding, not the test title.
    blocks = [re.sub(r"^_{3,} (.+) _{3,}$", r"\1", block, count=1, flags=re.MULTILINE) for block in blocks]
    if len(end[1]) > 9:
        return FailureEvidence("unknown", "unsupported failure count")
    count = int(end[1])
    if (not nodes or len(nodes) != count or len(set(node for node, _ in nodes)) != count or len(blocks) != count
            or any(not re.search(r"^E\s+\S", block, re.MULTILINE) for block in blocks)):
        return FailureEvidence("unknown", "incomplete failure blocks or node list")
    canonical = json.dumps({
        "contract": CONTRACT,
        "nodes": sorted(normalize(node + " - " + error, work_dir) for node, error in nodes),
        "blocks": sorted(normalize(block, work_dir) for block in blocks),
    }, ensure_ascii=False, sort_keys=True)
    return FailureEvidence("pytest_failure", "complete supported pytest output",
                           hashlib.sha256(canonical.encode()).hexdigest(), canonical, count)


def test_files_changed(paths: list[str]) -> bool:
    """Conservative possible-TDD signal, not an assertion about developer intent."""
    for path in paths:
        p = PurePosixPath(path.replace("\\", "/"))
        if ("tests" in p.parts or p.name in {"conftest.py", "tests.py", "test.py"}
                or p.name.startswith("test_") or p.name.endswith("_test.py")):
            return True
    return False


class FailureFeedback:
    def __init__(self, store: JobStore, job_id: str, enabled: bool, work_dir: str) -> None:
        self.store, self.job_id, self.enabled, self.work_dir = store, job_id, enabled, work_dir
        self.previous = ""
        self.streak = 0
        self.repeated = False
        self.attempt = 0

    async def observe(self, outcome: TestOutcome | None, attempt: int, paths: list[str]) -> None:
        if not self.enabled:
            return
        evidence = identify(outcome, self.work_dir)
        tdd = test_files_changed(paths)
        fingerprint = evidence.fingerprint
        comparable = bool(fingerprint and not tdd)
        relation = "uncompared"
        if comparable:
            relation = "same" if fingerprint == self.previous else "changed" if self.previous else "first"
        self.streak = self.streak + 1 if relation == "same" else int(comparable)
        self.previous = fingerprint if comparable else ""
        self.repeated = relation == "same"
        self.attempt = attempt
        raw = await self.store.transact(lambda conn: conn.execute(
            "SELECT seq,detail FROM job_events WHERE job_id=? AND kind='verify_tests' ORDER BY seq DESC LIMIT 1",
            (self.job_id,),
        ).fetchone())
        # Bound fields before serializing; never truncate serialized JSON.
        await self.store.add_event(self.job_id, "verification_feedback", json.dumps({
            "contract": CONTRACT, "attempt": attempt,
            "classification": evidence.classification, "reason": evidence.reason,
            "fingerprint": fingerprint, "failure_count": evidence.failures,
            "relation": relation, "consecutive": self.streak, "possible_tdd": tdd,
            "normalized_excerpt": evidence.normalized[:400],
            "normalized_truncated": len(evidence.normalized) > 400,
            "raw_evidence_kind": "verify_tests", "raw_tail_chars": 2000,
            "raw_event_seq": raw[0] if raw else None,
            "output_chars": len(outcome.output) if outcome else 0,
            "raw_truncated": bool(outcome and outcome.output and (raw is None or not raw[1].endswith(outcome.output))),
        }, ensure_ascii=False))

    async def retry_feedback(self, output: str) -> str:
        if not self.enabled or not self.repeated:
            return output
        await self.store.add_event(self.job_id, "dead_end_reminder", json.dumps({
            "attempt": self.attempt, "next_attempt": self.attempt + 1,
            "fingerprint": self.previous, "consecutive": self.streak,
            "status": "prepared", "text": REMINDER,
        }))
        return output + "\n\n[Service supervision advisory]\n" + REMINDER


def feedback_reports(events: list[JobEvent]) -> list[dict]:
    reports = []
    for event in events:
        if event.kind not in {"verification_feedback", "dead_end_reminder"}:
            continue
        try:
            data = json.loads(event.detail)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            reports.append({**data, "kind": event.kind, "event_seq": event.seq,
                            "execution_id": event.execution_id, "ts": event.ts})
    return reports
