"""Crash boundaries and policy changes, without real models or remote writes."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from mewcode.config import NotifyConfig, RepoConfig, SandboxConfig, ServiceConfig
from mewcode.service.api import create_app
from mewcode.service.execution import AgentRunOutcome, ExecutionChain, HeadlessAgentRunner, TokenBudgetExceeded
from mewcode.service.jobs import JobStore
from mewcode.service.metrics import collect_metrics, job_report, render_prometheus
from mewcode.service.notify import NullNotifier, WebhookNotifier
from mewcode.service.outbox import NotificationOutbox
from mewcode.service.policy import RepoPolicy, RepoPolicyLoader, effective_policy
from mewcode.service.runtime import ServiceRuntime
from mewcode.service.triggers.base import JobDraft
from mewcode.service.worker import WorkerPool


@pytest_asyncio.fixture
async def store(tmp_path):
    value = JobStore(tmp_path / "jobs.db")
    await value.connect()
    try:
        yield value
    finally:
        await value.close()


def webhook(calls, codes=None, *, kind="slack"):
    codes = list(codes or [200])

    def respond(request):
        calls.append({"url": str(request.url), "body": json.loads(request.content)})
        code = codes.pop(0) if len(codes) > 1 else codes[0]
        return httpx.Response(code, json={"errcode": 0}, headers={"retry-after": "2"})

    return WebhookNotifier(
        NotifyConfig(type=kind, webhook_url="https://notify.invalid/secret-original"),
        transport=httpx.MockTransport(respond),
    )


async def due_now(store):
    await store.transact(lambda conn: conn.execute("UPDATE notification_outbox SET due=0").rowcount)


@pytest.mark.asyncio
async def test_transition_and_notification_roll_back_together(store):
    job = await store.create_job("fp", "demo", status="triaging")
    await store.transact(lambda conn: conn.execute(
        "CREATE TRIGGER fail_notification BEFORE INSERT ON notification_outbox "
        "BEGIN SELECT RAISE(ABORT,'disk failure'); END",
    ).rowcount)
    with pytest.raises(sqlite3.IntegrityError):
        await store.transition(job.id, "escalate", reason="boom")
    assert (await store.get_or_raise(job.id)).status == "triaging"
    assert [e.kind for e in await store.events(job.id)] == ["created"]
    assert await store.journal.notifications(job.id) == []
    await store.transact(lambda conn: conn.execute("DROP TRIGGER fail_notification").rowcount)
    await store.transition(job.id, "escalate", reason="boom")
    assert len(await store.journal.notifications(job.id)) == 1


@pytest.mark.asyncio
async def test_intake_rolls_back_if_notification_cannot_be_saved(store):
    await store.transact(lambda conn: conn.execute(
        "CREATE TRIGGER fail_notification BEFORE INSERT ON notification_outbox "
        "BEGIN SELECT RAISE(ABORT,'disk failure'); END",
    ).rowcount)
    with pytest.raises(sqlite3.IntegrityError):
        await store.accept_job("fp", "demo", 300)
    assert await store.list_jobs() == []
    await store.transact(lambda conn: conn.execute("DROP TRIGGER fail_notification").rowcount)
    _, created = await store.accept_job("fp", "demo", 300)
    assert created


@pytest.mark.asyncio
@pytest.mark.parametrize("send_before_restart", [False, True])
async def test_waiting_review_notice_recovers_without_rerunning_agent(tmp_path, send_before_restart):
    calls = []
    notifier = webhook(calls, [500, 200] if send_before_restart else [200])
    path = tmp_path / "jobs.db"
    store = JobStore(path)
    await store.connect()
    job = await store.create_job("fp", "demo", status="ci_gate")
    await store.transition(job.id, "human_review", pr_url="https://example.invalid/pr/1")
    if send_before_restart:
        await NotificationOutbox(store, notifier).process_once()
        assert (await store.journal.notifications(job.id))[0]["status"] == "retry"
    await store.close()
    reopened = JobStore(path)
    await reopened.connect()
    await due_now(reopened)
    handler = AsyncMock()
    runtime = ServiceRuntime(ServiceConfig(), handler, store=reopened, notifier=notifier,
                             worktree_cleanup_cutoff_hours=None)
    await runtime.start()
    try:
        await asyncio.wait_for(runtime.pool._queue.join(), 2)
        await runtime.outbox.flush()
        handler.assert_not_awaited()
        final = await reopened.get_or_raise(job.id)
        assert final.status == "human_review" and final.attempts == 0
        rows = await reopened.journal.notifications(job.id)
        assert rows[0]["status"] == "sent"
        assert rows[0]["attempts"] == (2 if send_before_restart else 1)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_remote_success_without_local_receipt_may_repeat_stable_event(store, monkeypatch):
    calls = []
    job = await store.create_job("fp", "demo", status="ci_gate")
    await store.transition(job.id, "human_review")
    outbox = NotificationOutbox(store, webhook(calls))
    settle = store.journal.settle_notification
    monkeypatch.setattr(store.journal, "settle_notification", AsyncMock(side_effect=RuntimeError("crash")))
    with pytest.raises(RuntimeError, match="crash"):
        await outbox.process_once()
    assert (await store.journal.notifications(job.id))[0]["status"] == "pending"
    monkeypatch.setattr(store.journal, "settle_notification", settle)
    await outbox.flush()
    assert len(calls) == 2
    assert calls[0]["body"] == calls[1]["body"]
    assert "通知事件" in calls[0]["body"]["text"]


@pytest.mark.asyncio
async def test_failed_job_notice_preserves_order_without_blocking_other_jobs(store):
    calls = []
    first = await store.create_job("one", "demo", status="pr_opened")
    await store.transition(first.id, "ci_gate")
    await store.transition(first.id, "human_review")
    second = await store.create_job("two", "demo")
    outbox = NotificationOutbox(store, webhook(calls, [500, 200]))
    await outbox.flush()
    rows = await store.journal.notifications(first.id)
    assert [r["status"] for r in rows] == ["retry", "pending"]
    assert (await store.journal.notifications(second.id))[0]["status"] == "sent"
    await due_now(store)
    await outbox.flush()
    assert [r["status"] for r in await store.journal.notifications(first.id)] == ["sent", "sent"]


@pytest.mark.asyncio
async def test_route_changes_need_explicit_retry_and_do_not_persist_credentials(store):
    calls = []
    notifier = webhook(calls, [500, 200])
    job = await store.create_job("fp", "demo")
    outbox = NotificationOutbox(store, notifier)
    await outbox.flush()
    seq = (await store.journal.notifications(job.id))[0]["event_seq"]
    notifier.config = replace(notifier.config, webhook_url="https://notify.invalid/secret-new")
    await due_now(store)
    await outbox.flush()
    assert len(calls) == 1
    assert (await store.journal.notifications(job.id))[0]["status"] == "blocked"
    assert await store.journal.retry_notification(job.id, seq, accept_route_change=True)
    await outbox.flush()
    assert calls[-1]["url"].endswith("secret-new")
    dump = await store.transact(lambda conn: "\n".join(conn.iterdump()))
    assert "secret-original" not in dump and "secret-new" not in dump


@pytest.mark.asyncio
async def test_disabled_notifications_settle_as_skipped(store):
    job = await store.create_job("fp", "demo")
    await NotificationOutbox(store, NullNotifier()).flush()
    assert (await store.journal.notifications(job.id))[0]["status"] == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["wecom", "dingtalk"])
@pytest.mark.parametrize("body", [{"errcode": 93000, "errmsg": "secret"}, {"ok": True}, []])
async def test_http200_is_not_a_provider_receipt(kind, body):
    notifier = WebhookNotifier(
        NotifyConfig(type=kind, webhook_url="https://notify.invalid/secret"),
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)),
    )
    result = await notifier.send_result("t", "b")
    assert result.status == "retry" and "secret" not in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [(401, "blocked"), (302, "blocked"), (429, "retry"), (503, "retry")])
async def test_http_failures_have_explicit_results(code, status):
    result = await webhook([], [code]).send_result("t", "b")
    assert result.status == status
    assert result.error == f"HTTP {code}"


@pytest.mark.asyncio
async def test_worker_timeout_creates_escalation_notice_and_execution(store):
    job = await store.create_job("fp", "demo")

    async def hang(job):
        await store.transition(job.id, "triaging")
        await asyncio.Event().wait()

    pool = WorkerPool(store, hang, concurrency=1, job_timeout=0.03)
    await pool.start()
    try:
        await pool.submit(job.id)
        await asyncio.wait_for(pool._queue.join(), 2)
    finally:
        await pool.stop()
    records = await store.journal.executions(job.id)
    assert records[0]["outcome"] == "timeout" and records[0]["end_state"] == "escalate"
    calls = []
    await NotificationOutbox(store, webhook(calls)).flush()
    rows = await store.journal.notifications(job.id)
    assert [r["phase"] for r in rows] == ["received", "escalated"]
    assert all(r["status"] == "sent" for r in rows)
    assert (await store.get_or_raise(job.id)).attempts == 0


@pytest.mark.asyncio
async def test_slow_notification_does_not_hold_intake_or_worker(store):
    entered = asyncio.Event()

    async def slow_http(request):
        entered.set()
        await asyncio.Event().wait()

    notifier = WebhookNotifier(NotifyConfig(type="slack", webhook_url="https://notify.invalid/x"),
                               transport=httpx.MockTransport(slow_http))

    async def handler(job):
        await store.transition(job.id, "escalate")

    runtime = ServiceRuntime(ServiceConfig(), handler, store=store, notifier=notifier,
                             worktree_cleanup_cutoff_hours=None)
    await runtime.start()
    try:
        await asyncio.wait_for(runtime.intake([JobDraft("one", "demo")]), 2)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(runtime.intake([JobDraft("two", "demo")]), 2)
        await asyncio.wait_for(runtime.pool._queue.join(), 2)
        jobs = await store.list_jobs()
        assert len(jobs) == 2 and all(j.status == "escalate" for j in jobs)
    finally:
        await asyncio.wait_for(runtime.stop(), 2)
    reopened = JobStore(store.db_path)
    await reopened.connect()
    try:
        calls = []
        # The same destination is necessary to release the pinned rows.
        healthy = webhook(calls)
        healthy.config = notifier.config
        await NotificationOutbox(reopened, healthy).flush()
        assert len(calls) == 4
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_manual_rework_has_distinct_execution_without_spending_retry_budget(store):
    async def handler(job):
        if job.status == "received":
            await store.transition(job.id, "triaging")
            await store.transition(job.id, "reproducing")
        await store.transition(job.id, "fixing")
        await store.add_usage(job.id, 10, 5)
        await store.add_event(job.id, "agent_finished", "attempt=1 tokens_in=10 tokens_out=5")
        for state in ("verifying", "pr_opened", "ci_gate", "human_review"):
            await store.transition(job.id, state)

    pool = WorkerPool(store, handler, concurrency=1)
    await pool.start()
    try:
        job = await store.create_job("fp", "demo")
        await pool.submit(job.id)
        await asyncio.wait_for(pool._queue.join(), 2)
        await store.transition(job.id, "changes_requested", last_error="more coverage")
        await pool.submit(job.id)
        await asyncio.wait_for(pool._queue.join(), 2)
    finally:
        await pool.stop()
    report = await job_report(store, job.id)
    runs = report["executions"]
    assert [r["origin"] for r in runs] == ["initial", "manual_rework"]
    assert [r["tokens_in"] for r in runs] == [10, 10]
    assert report["job"]["attempts"] == 1
    assert {a["execution_id"] for a in report["agent"]["attempts"]} == {r["id"] for r in runs}
    reviews = [r for r in report["notifications"] if r["phase"] == "human_review"]
    assert len(reviews) == 2 and reviews[0]["status"] == "superseded"
    assert reviews[0]["event_seq"] != reviews[1]["event_seq"]


@pytest.mark.asyncio
async def test_crash_recovery_closes_old_execution_and_preserves_attempt_budget(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    job = await store.create_job("fp", "demo", status="fixing")
    await store.journal.start_execution(job)
    await store.add_usage(job.id, 8, 4)
    await store.close()  # Simulate a process loss: no finish_execution.
    reopened = JobStore(tmp_path / "jobs.db")
    await reopened.connect()

    async def handler(job):
        assert job.status == "received" and job.attempts == 1
        for state in ("triaging", "reproducing", "fixing", "cant_repro"):
            await reopened.transition(job.id, state)
        await reopened.add_usage(job.id, 3, 2)

    pool = WorkerPool(reopened, handler, concurrency=1)
    await pool.start()
    try:
        assert await pool.requeue_unfinished() == 1
        await asyncio.wait_for(pool._queue.join(), 2)
        runs = await reopened.journal.executions(job.id)
        assert [r["origin"] for r in runs] == ["initial", "recovery"]
        assert [r["outcome"] for r in runs] == ["interrupted", "completed"]
        assert [r["tokens_in"] for r in runs] == [8, 3]
        assert (await reopened.get_or_raise(job.id)).attempts == 1
    finally:
        await pool.stop()
        await reopened.close()


@pytest.mark.asyncio
async def test_changed_branch_escalates_before_worktree_or_agent(store, tmp_path):
    root = tmp_path / "repo"
    (root / ".mewcode").mkdir(parents=True)
    path = root / ".mewcode" / "policy.yaml"
    path.write_text("target_branch: main\n", encoding="utf-8")
    config = ServiceConfig(repos={"demo": RepoConfig(name="demo", path=str(root))})
    loader = RepoPolicyLoader(config.repos)
    job, _ = await store.accept_job("fp", "demo", 300,
                                    policy_snapshot=effective_policy(config, "demo", loader.load("demo")))
    path.write_text("target_branch: develop\n", encoding="utf-8")
    runner = AsyncMock()
    manager = AsyncMock()
    chain = ExecutionChain(config, store, runner, worktree_manager_factory=lambda root: manager)
    await chain(job)
    final = await store.get_or_raise(job.id)
    assert final.status == "escalate" and "changed" in final.last_error
    runner.run.assert_not_awaited()
    manager.create.assert_not_awaited()
    original = await store.journal.policy(job.id)
    assert original["pr_base"] == "main"


@pytest.mark.asyncio
@pytest.mark.parametrize("original,current,expected", [(100, 200, 100), (200, 100, 100), (0, 100, 100), (100, 0, 100)])
async def test_budget_changes_use_the_stricter_limit(store, original, current, expected):
    config = ServiceConfig(token_budget=original, sandbox=SandboxConfig(enabled=False))
    job = await store.create_job("fp", "demo")
    await store.journal.apply_policy(job, effective_policy(config, "demo", RepoPolicy()))
    config.token_budget = current
    runner = HeadlessAgentRunner(config, None, policy_loader=RepoPolicyLoader({}), store=store)
    runner._run_direct = AsyncMock(return_value=AgentRunOutcome(final_text="done"))
    await runner.run(job, ".", "probe", lambda event: None)
    assert runner._run_direct.await_args.args[-1] == expected
    await store.add_usage(job.id, expected, 0)
    with pytest.raises(TokenBudgetExceeded):
        await runner.run(await store.get_or_raise(job.id), ".", "probe", lambda event: None)
    assert runner._run_direct.await_count == 1


@pytest.mark.asyncio
async def test_notifications_are_visible_in_metrics(store):
    await store.create_job("fp", "demo")
    text = render_prometheus(await collect_metrics(store))
    assert 'mewcode_notifications{status="pending"} 1' in text
    assert "mewcode_notification_oldest_pending_seconds " in text


@pytest.mark.asyncio
async def test_retry_endpoint_requires_auth_and_validates_scope(store):
    job = await store.create_job("fp", "demo")
    outbox = NotificationOutbox(store, webhook([], [401]))
    await outbox.flush()
    row = (await store.journal.notifications(job.id))[0]
    runtime = ServiceRuntime(ServiceConfig(webhook_token="test-token"), AsyncMock(), store=store)
    path = f"/jobs/{job.id}/notifications/{row['event_seq']}/retry"
    async with TestClient(TestServer(create_app(runtime, {}))) as client:
        assert (await client.post(path, json={})).status == 401
        headers = {"X-MewCode-Token": "test-token"}
        assert (await client.post(path, json={"accept_route_change": "yes"}, headers=headers)).status == 400
        assert (await client.post(path, json={}, headers=headers)).status == 200
        assert (await client.post(path, json={}, headers=headers)).status == 409


@pytest.mark.asyncio
async def test_cancelled_transition_keeps_connection_until_atomic_commit(store, monkeypatch):
    import mewcode.service.jobs as jobs

    job = await store.create_job("fp", "demo", status="ci_gate")
    entered, release = threading.Event(), threading.Event()
    enqueue = jobs.enqueue_notification

    def held(conn, current, seq, detail=""):
        entered.set()
        assert release.wait(5)
        return enqueue(conn, current, seq, detail)

    monkeypatch.setattr(jobs, "enqueue_notification", held)
    transition = asyncio.create_task(store.transition(job.id, "human_review"))
    assert await asyncio.to_thread(entered.wait, 5)
    transition.cancel()
    read = asyncio.create_task(store.get_or_raise(job.id))
    try:
        await asyncio.sleep(0)
        assert not read.done()  # connection remains owned by the committing thread
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await transition
    assert (await read).status == "human_review"
    assert len(await store.journal.notifications(job.id)) == 1


@pytest.mark.asyncio
async def test_intake_pins_route_before_first_send(store):
    calls = []
    notifier = webhook(calls)
    runtime = ServiceRuntime(ServiceConfig(), AsyncMock(), store=store, notifier=notifier)
    runtime.pool = SimpleNamespace(running=True, submit=AsyncMock())
    runtime.accepting = True
    result = await runtime.intake([JobDraft("fp", "demo")])
    notifier.config = replace(notifier.config, webhook_url="https://notify.invalid/new-destination")
    await runtime.outbox.flush()
    assert calls == []
    assert (await store.journal.notifications(result.accepted[0].id))[0]["status"] == "blocked"


@pytest.mark.asyncio
async def test_tightened_budget_blocks_publication_without_publisher_call(store):
    config = ServiceConfig(token_budget=100)
    job = await store.create_job("fp", "demo", status="verifying")
    await store.journal.apply_policy(job, effective_policy(config, "demo", RepoPolicy()))
    await store.add_usage(job.id, 50, 0)
    config.token_budget = 25
    publisher = AsyncMock()
    chain = ExecutionChain(config, store, AsyncMock(), publisher=publisher)
    await chain._publish(job, None)
    publisher.publish.assert_not_awaited()
    final = await store.get_or_raise(job.id)
    assert final.status == "escalate" and "budget tightened" in final.last_error


@pytest.mark.asyncio
async def test_revoked_severity_does_not_start_execution_chain(store, tmp_path):
    root = tmp_path / "repo"
    (root / ".mewcode").mkdir(parents=True)
    (root / ".mewcode" / "policy.yaml").write_text("triggers:\n  severities: [critical]\n", encoding="utf-8")
    config = ServiceConfig(repos={"demo": RepoConfig(name="demo", path=str(root))})
    job = await store.create_job("fp", "demo", severity="warning")
    runner = AsyncMock()
    await ExecutionChain(config, store, runner)(job)
    runner.run.assert_not_awaited()
    assert (await store.get_or_raise(job.id)).status == "escalate"


@pytest.mark.asyncio
async def test_recovery_keeps_manual_feedback_entry_and_closes_waiting_execution(store):
    job = await store.create_job("fp", "demo", status="human_review")
    await store.journal.start_execution(job)  # process stopped after persisting human_review
    runtime = ServiceRuntime(ServiceConfig(), AsyncMock(), store=store, worktree_cleanup_cutoff_hours=None)
    await runtime.start()
    try:
        await asyncio.wait_for(runtime.pool._queue.join(), 2)
        runtime._handler.assert_not_awaited()
        assert (await store.journal.executions(job.id))[0]["outcome"] == "interrupted"
        await store.transition(job.id, "changes_requested", last_error="please add coverage")
        await runtime.pool.requeue_unfinished()
        await asyncio.wait_for(runtime.pool._queue.join(), 2)
        passed_job = runtime._handler.await_args.args[0]
        assert passed_job.status == "changes_requested" and passed_job.last_error == "please add coverage"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_superseded_batch_row_is_not_sent(store):
    job = await store.create_job("fp", "demo", status="ci_gate")
    await store.transition(job.id, "human_review")
    row = (await store.journal.ready_notifications())[0]
    await store.transition(job.id, "changes_requested")
    calls = []
    await NotificationOutbox(store, webhook(calls))._deliver(row)
    assert calls == []
    assert (await store.journal.notifications(job.id))[0]["status"] == "superseded"
