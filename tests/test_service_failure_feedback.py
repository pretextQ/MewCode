"""Pytest contract plus the real bounded execution/worker feedback path; no LLM."""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_service_execution import (
    CALC_BUGGY,
    FakePublisher,
    FakeRunner,
    _git,
    chain_env,
    make_job,
)
from test_service_execution import (
    demo_repo as demo_repo,
)

from mewcode.service.execution import ExecutionChain
from mewcode.service.execution import TestOutcome as Outcome
from mewcode.service.execution import TestRunner as CommandTestRunner
from mewcode.service.failure_feedback import REMINDER, identify, normalize
from mewcode.service.jobs import JobStore
from mewcode.service.metrics import job_report
from mewcode.service.worker import WorkerPool


def failure(value="2", *, temp="/tmp/pytest-of-user/pytest-3", seconds="0.03", node="test_calc"):
    return Outcome("pytest -q", 1, f"""F                                                                        [100%]
================================== FAILURES ===================================
________________________________ {node} __________________________________

tmp_path = PosixPath('{temp}/{node}0')

    def {node}(tmp_path):
>       assert actual == 1
E       assert {value} == 1

test_calc.py:5: AssertionError
=========================== short test summary info ===========================
FAILED test_calc.py::{node} - assert {value} == 1
1 failed in {seconds}s
""")


class SequenceTests:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = 0

    async def run(self, work_dir, command, timeout):
        index = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        return replace(self.outcomes[index], command=command)


PASSED = Outcome("pytest -q", 0, "1 passed in 0.01s")


@pytest.mark.parametrize("change", [
    {"temp": "/tmp/pytest-of-user/pytest-91"},
    {"temp": r"C:\Users\test\AppData\Local\Temp\pytest-of-user\pytest-91"},
    {"seconds": "51.92"},
])
def test_dynamic_pytest_fields(change):
    original = identify(failure(), "")
    candidate = identify(failure(**change), "")
    assert original.classification == candidate.classification == "pytest_failure"
    assert original.fingerprint == candidate.fingerprint


@pytest.mark.parametrize("candidate", [failure("3"), failure(node="test_calc2"),
    replace(failure(), output=failure().output.replace("test_calc.py:5", "test_calc.py:8")),
    replace(failure(), output=failure().output.replace("actual == 1", "other == 1")),
    replace(failure(), output=failure().output.replace("test_calc0", "test_calc20")),
])
def test_semantic_changes_are_preserved(candidate):
    assert identify(candidate, "").fingerprint != identify(failure(), "").fingerprint


@pytest.mark.parametrize("outcome,classification", [
    (replace(failure(), command="npm test"), "unknown"),
    (replace(failure(), command="pytest | tee result.txt"), "unknown"),
    (replace(failure(), output="AssertionError: failure"), "unknown"),
    (replace(failure(), output=failure().output.rsplit("1 failed", 1)[0]), "unknown"),
    (replace(failure(), output=failure().output.replace("1 failed", "2 failed")), "unknown"),
    (replace(failure(), output=failure().output.replace("E       assert 2 == 1", "")), "unknown"),
    (replace(failure(), output=failure().output.replace("FAILED test_calc", "ERROR test_calc")), "unknown"),
    (replace(failure(), output=failure().output.replace("::test_calc", "::test_calc...")), "unknown"),
    (replace(failure(), output=failure().output.replace("1 failed", "9" * 4500 + " failed")), "unknown"),
    (replace(failure(), timed_out=True), "timeout"),
    (replace(failure(), output="E   Failed: Timeout >30.0s"), "timeout"),
    (replace(failure(), output="E   ModuleNotFoundError: No module named 'db'"), "environment"),
    (replace(failure(), exit_code=127, output="pytest: command not found"), "environment"),
    (replace(failure(), exit_code=3, output="INTERNALERROR pytest crashed"), "infrastructure"),
    (replace(failure(), exit_code=4, output="ERROR: bad argument"), "infrastructure"),
    (replace(failure(), exit_code=2), "unknown"),
])
def test_incomplete_or_excluded_output_never_has_a_fingerprint(outcome, classification):
    evidence = identify(outcome, "")
    assert evidence.classification == classification
    assert not evidence.fingerprint


def test_known_worktree_normalization_has_path_boundaries():
    assert normalize("/work/job/a.py /work/job-extra/a.py", "/work/job") == (
        "<worktree>/a.py /work/job-extra/a.py"
    )
    assert normalize(">       assert path == '/tmp/pytest-of-user/pytest-3/data'", "") == (
        ">       assert path == '/tmp/pytest-of-user/pytest-3/data'"
    )


def test_all_failure_blocks_participate_not_only_first_three():
    def multiple(last):
        outcomes = [failure(str(i) if i < 3 else last, node=f"test_case{i}") for i in range(4)]
        blocks = [o.output.split("FAILURES ===================================\n")[1].split(
            "=========================== short test summary info")[0] for o in outcomes]
        nodes = [o.output.split("short test summary info ===========================\n")[1].splitlines()[0]
                 for o in outcomes]
        return Outcome("pytest", 1, "=== FAILURES ===\n" + "".join(blocks)
                       + "=== short test summary info ===\n" + "\n".join(nodes) + "\n4 failed in 0.12s")

    original, changed = identify(multiple("7"), ""), identify(multiple("8"), "")
    assert original.failures == changed.failures == 4
    assert original.fingerprint and original.fingerprint != changed.fingerprint


def test_ansi_crlf_and_standard_timing_sections_are_not_failure_semantics():
    outcome = failure()
    decorated = replace(outcome, output="\x1b[31m" + outcome.output.replace("\n", "\r\n") + "\x1b[0m")
    assert identify(decorated, "").fingerprint == identify(outcome, "").fingerprint
    with_duration = replace(outcome, output=outcome.output.replace(
        "=========================== short test summary info",
        "=== slowest 1 durations ===\n14.03s call test_calc.py::test_calc\n"
        "=========================== short test summary info"))
    assert identify(with_duration, "").fingerprint == identify(outcome, "").fingerprint
    narrow = replace(outcome, output=outcome.output.replace("________________________________", "____"))
    assert identify(narrow, "").fingerprint == identify(outcome, "").fingerprint


def test_timeout_plugin_name_in_source_does_not_imply_timeout():
    outcome = replace(failure(), output=failure().output.replace("def test_calc(tmp_path):", "def test_pytest_timeout(tmp_path):"))
    assert identify(outcome, "").classification == "pytest_failure"


def test_captured_application_heading_does_not_discard_semantic_output():
    original = failure()
    def with_capture(value):
        return replace(original, output=original.output.replace(
            "=========================== short test summary info",
            "----------------------------- Captured stdout call -----------------------------\n"
            f"=== response body ===\n{value}\n"
            "=========================== short test summary info"))
    first, second = identify(with_capture("first error"), ""), identify(with_capture("second error"), "")
    assert first.fingerprint and first.fingerprint != second.fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("outputs,relations,reminder", [
    ([failure(), failure(), PASSED], ["first", "same", "uncompared"], True),
    ([failure(), failure(temp="/tmp/pytest-of-user/pytest-98", seconds="14.05"), PASSED],
     ["first", "same", "uncompared"], True),
    ([failure(), failure("3"), failure("4")], ["first", "changed", "changed"], False),
    ([failure(), replace(failure(), output="unrecognized"), failure()], ["first", "uncompared", "first"], False),
    ([failure(), replace(failure(), timed_out=True), failure()], ["first", "uncompared", "first"], False),
    ([failure(), replace(failure(), output="ModuleNotFoundError: db"), failure()],
     ["first", "uncompared", "first"], False),
    ([failure(), replace(failure(), exit_code=3), failure()], ["first", "uncompared", "first"], False),
])
async def test_execution_feedback_and_unchanged_attempt_budget(tmp_path, demo_repo, outputs, relations, reminder):
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"],
                         publisher=FakePublisher(), test_command_override="pytest -q") as (store, chain, runner, cfg):
        cfg.enable_supervision = True
        chain.test_runner = SequenceTests([failure(), *outputs])
        job = await make_job(store)
        pool = WorkerPool(store, chain, concurrency=1)
        await pool.start()
        try:
            await pool.submit(job.id)
            await asyncio.wait_for(pool._queue.join(), 15)
        finally:
            await pool.stop()
        report = await job_report(store, job.id)
        evidence = report["verification"]["feedback"]
        observed = [e for e in evidence if e["kind"] == "verification_feedback"]
        prepared = [e for e in evidence if e["kind"] == "dead_end_reminder"]
        assert [e["relation"] for e in observed] == relations
        assert bool(prepared) == reminder
        assert (REMINDER in runner.calls[2]["prompt"]) == reminder
        assert len(runner.calls) == report["job"]["attempts"] == 3
        assert report["job"]["status"] == ("pr_opened" if outputs[-1].passed else "escalate")
        assert report["job"]["tokens"]["total"] == 600
        assert len(report["executions"]) == 1
        assert all(e["execution_id"] == report["executions"][0]["id"] for e in evidence)
        assert [e["attempt"] for e in observed] == [1, 2, 3]
        raw = {e["event_seq"]: e for e in report["verification"]["tests"]}
        assert all(raw[e["raw_event_seq"]]["execution_id"] == e["execution_id"] for e in observed)
        assert any("assert 2 == 1" in e["detail"] for e in report["verification"]["tests"])
        assert len([e for e in await store.events(job.id) if e.kind == "verify_tests"]) == 3


@pytest.mark.asyncio
async def test_supervision_off_keeps_exact_legacy_feedback(tmp_path, demo_repo):
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"],
                         test_command_override="pytest -q") as (store, chain, runner, cfg):
        assert not cfg.enable_supervision
        chain.test_runner = SequenceTests([failure()])
        job = await make_job(store)
        await chain(job)
        events = await store.events(job.id)
        assert not any(e.kind in {"verification_feedback", "dead_end_reminder"} for e in events)
        assert not any(REMINDER in c["prompt"] for c in runner.calls)
        assert failure().output in runner.calls[2]["prompt"]
        assert len(runner.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["npm test", "pytest --tb=no", ""])
async def test_service_unknown_command_format_or_not_run_has_no_reminder(tmp_path, demo_repo, command):
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"],
                         test_command_override=command) as (store, chain, runner, cfg):
        cfg.enable_supervision = True
        output = failure() if command == "npm test" else replace(failure(), output="1 failed in 0.03s")
        chain.test_runner = SequenceTests([output])
        job = await make_job(store)
        await chain(job)
        observations = (await job_report(store, job.id))["verification"]["feedback"]
        assert observations
        assert all(e["classification"] == ("unknown" if command else "not_run") for e in observations)
        assert not any(REMINDER in c["prompt"] for c in runner.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("long_command", [False, True])
async def test_evidence_json_and_actual_raw_truncation(tmp_path, demo_repo, long_command):
    command = "pytest " + "x" * 4100 if long_command else "pytest -q"
    outcome = failure("x" * 5000) if not long_command else failure()
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"],
                         test_command_override=command) as (store, chain, runner, cfg):
        cfg.enable_supervision = True
        store.max_fix_attempts = 1
        chain.test_runner = SequenceTests([outcome])
        job = await make_job(store)
        await chain(job)
        evidence = (await job_report(store, job.id))["verification"]["feedback"][0]
        assert evidence["raw_truncated"]
        assert evidence["normalized_truncated"] == (not long_command)
        assert evidence["output_chars"] == len(outcome.output)
        assert len(evidence["normalized_excerpt"]) <= 400
        assert evidence["fingerprint"]


@pytest.mark.asyncio
async def test_test_changes_are_possible_tdd_and_do_not_warn(tmp_path, demo_repo):
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"],
                         test_command_override="pytest -q") as (store, chain, runner, cfg):
        cfg.enable_supervision = True
        original = runner.run

        async def edit_test(job, work_dir, prompt, on_event):
            (Path(work_dir) / "test_new.py").write_text("def test_new(): pass\n", encoding="utf-8")
            return await original(job, work_dir, prompt, on_event)

        runner.run = edit_test
        chain.test_runner = SequenceTests([failure()])
        job = await make_job(store)
        await chain(job)
        evidence = (await job_report(store, job.id))["verification"]["feedback"]
        assert len(evidence) == 3
        assert all(e["possible_tdd"] and e["relation"] == "uncompared" for e in evidence)
        assert not any(REMINDER in c["prompt"] for c in runner.calls)


@pytest.mark.asyncio
async def test_baseline_is_not_a_fix_attempt_and_ceiling_has_no_prepared_reminder(tmp_path, demo_repo):
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"],
                         test_command_override="pytest -q") as (store, chain, runner, cfg):
        cfg.enable_supervision = True
        store.max_fix_attempts = 2
        chain.test_runner = SequenceTests([failure()])
        job = await make_job(store)
        await chain(job)
        evidence = (await job_report(store, job.id))["verification"]["feedback"]
        assert [e["relation"] for e in evidence] == ["first", "same"]
        assert len(runner.calls) == 2
        assert not any(REMINDER in c["prompt"] for c in runner.calls)


@pytest.mark.asyncio
async def test_real_pytest_output_through_service(tmp_path, demo_repo, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    (demo_repo / "test_calc.py").write_text(
        "from calc import add\ndef test_calc(tmp_path):\n    assert add(2, 3) == 5\n", encoding="utf-8")
    _git(demo_repo, "add", ".")
    _git(demo_repo, "commit", "-m", "pytest fixture")
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"],
                         test_command_override=f'"{sys.executable}" -m pytest -q') as (store, chain, runner, cfg):
        cfg.enable_supervision = True
        assert isinstance(chain.test_runner, CommandTestRunner)
        job = await make_job(store)
        await chain(job)
        evidence = (await job_report(store, job.id))["verification"]["feedback"]
        observed = [e for e in evidence if e["kind"] == "verification_feedback"]
        assert [e["relation"] for e in observed] == ["first", "same", "same"]
        assert REMINDER in runner.calls[2]["prompt"]


@pytest.mark.asyncio
async def test_handler_cancellation_resets_comparison_in_new_execution(tmp_path, demo_repo):
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"],
                         test_command_override="pytest -q") as (store, chain, runner, cfg):
        cfg.enable_supervision = True
        chain.test_runner = SequenceTests([failure()])
        original = runner.run
        cancelled = False

        async def cancel_once(job, work_dir, prompt, on_event):
            nonlocal cancelled
            if len(runner.calls) == 2 and not cancelled:
                cancelled = True
                raise asyncio.CancelledError("after repeated failure evidence")
            return await original(job, work_dir, prompt, on_event)

        runner.run = cancel_once
        job = await make_job(store)
        pool = WorkerPool(store, chain, concurrency=1)
        await pool.start()
        try:
            await pool.submit(job.id)
            await asyncio.wait_for(pool._queue.join(), 15)
        finally:
            await pool.stop()
        report = await job_report(store, job.id)
        runs = report["executions"]
        assert [r["origin"] for r in runs] == ["initial", "handler_retry"]
        observations = [e for e in report["verification"]["feedback"] if e["kind"] == "verification_feedback"]
        assert [e["relation"] for e in observations if e["execution_id"] == runs[1]["id"]] == ["first"]
        assert REMINDER not in runner.calls[2]["prompt"]
        assert report["job"]["attempts"] == 3


@pytest.mark.asyncio
async def test_restart_preserves_evidence_without_replaying_streak(tmp_path, demo_repo):
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"],
                         test_command_override="pytest -q") as (store, chain, runner, cfg):
        cfg.enable_supervision = True
        chain.test_runner = SequenceTests([failure()])
        original = runner.run

        async def interrupt(job, work_dir, prompt, on_event):
            if runner.calls:
                raise asyncio.CancelledError()
            return await original(job, work_dir, prompt, on_event)

        runner.run = interrupt
        job = await make_job(store)
        await store.journal.start_execution(job)
        with pytest.raises(asyncio.CancelledError):
            await chain(job)
    reopened = JobStore(tmp_path / "jobs.db")
    await reopened.connect()
    runner = FakeRunner([CALC_BUGGY + "# recovered\n"])
    chain = ExecutionChain(cfg, reopened, runner, test_runner=SequenceTests([failure()]))
    pool = WorkerPool(reopened, chain, concurrency=1)
    await pool.start()
    try:
        assert await pool.requeue_unfinished() == 1
        await asyncio.wait_for(pool._queue.join(), 15)
        report = await job_report(reopened, job.id)
        assert [r["origin"] for r in report["executions"]] == ["initial", "recovery"]
        observed = [e for e in report["verification"]["feedback"] if e["kind"] == "verification_feedback"]
        assert [e["relation"] for e in observed] == ["first", "first", "same"]
        assert REMINDER not in runner.calls[0]["prompt"]
        assert report["job"]["attempts"] == 3
    finally:
        await pool.stop()
        await reopened.close()


@pytest.mark.asyncio
async def test_manual_rework_has_separate_evidence_and_no_baseline_streak(tmp_path, demo_repo):
    async with chain_env(tmp_path, demo_repo, scripts=[CALC_BUGGY + "# edit\n"], publisher=FakePublisher(),
                         test_command_override="pytest -q") as (store, chain, runner, cfg):
        cfg.enable_supervision = True
        chain.test_runner = SequenceTests([failure(), failure(), PASSED, failure(), failure(), PASSED])
        pool = WorkerPool(store, chain, concurrency=1)
        await pool.start()
        try:
            job = await make_job(store)
            await pool.submit(job.id)
            await asyncio.wait_for(pool._queue.join(), 15)
            for state in ("ci_gate", "human_review", "changes_requested"):
                await store.transition(job.id, state, last_error="please inspect again")
            await pool.submit(job.id)
            await asyncio.wait_for(pool._queue.join(), 15)
        finally:
            await pool.stop()
        report = await job_report(store, job.id)
        assert [r["origin"] for r in report["executions"]] == ["initial", "manual_rework"]
        observed = report["verification"]["feedback"]
        assert [e["relation"] for e in observed] == ["first", "uncompared", "first", "uncompared"]
        assert [e["attempt"] for e in observed] == [1, 2, 2, 3]
        assert len({e["execution_id"] for e in observed}) == 2
        assert "please inspect again" in runner.calls[2]["prompt"]
        assert report["job"]["status"] == "pr_opened"
        for e in await store.events(job.id):
            if e.kind == "verification_feedback":
                assert isinstance(json.loads(e.detail), dict)
