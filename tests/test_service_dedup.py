"""Atomic alert intake, including independent connections to the same database."""
import asyncio
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.config import ServiceConfig
from mewcode.service.jobs import JobStore
from mewcode.service.runtime import ServiceRuntime
from mewcode.service.triggers.base import JobDraft


@pytest.mark.asyncio
async def test_concurrent_intake_enqueues_and_notifies_once(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    runtime = ServiceRuntime(ServiceConfig(), AsyncMock(), store=store, notifier=AsyncMock())
    runtime.pool = SimpleNamespace(running=True, submit=AsyncMock())
    try:
        draft = JobDraft(fingerprint="same", repo="demo", title="incident")
        results = await asyncio.gather(*(runtime.intake([draft]) for _ in range(20)))
        assert sum(len(r.accepted) for r in results) == 1
        assert sum(len(r.deduped) for r in results) == 19
        jobs = await store.list_jobs()
        assert len(jobs) == 1
        events = await store.events(jobs[0].id)
        assert sum(e.kind == "created" for e in events) == 1
        assert sum(e.kind == "deduped" for e in events) == 19
        runtime.pool.submit.assert_awaited_once_with(jobs[0].id)
        runtime.notifier.notify_job_event.assert_awaited_once()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_independent_connections_deduplicate(tmp_path):
    stores = [JobStore(tmp_path / "jobs.db") for _ in range(2)]
    for store in stores:
        await store.connect()
    try:
        results = await asyncio.gather(*(
            stores[i % 2].accept_job("same", "demo", 1800) for i in range(20)
        ))
        assert sum(created for _, created in results) == 1
        assert len({job.id for job, _ in results}) == 1
        assert len(await stores[0].list_jobs()) == 1
        assert len(await stores[0].events(results[0][0].id)) == 20
    finally:
        for store in stores:
            await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["repo", "fingerprint", "empty", "terminal", "expired"])
async def test_new_incidents_preserve_dedup_semantics(tmp_path, case):
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    try:
        fp = "" if case == "empty" else "same"
        first = await store.create_job(fp, "demo", status="escalate" if case == "terminal" else "received")
        if case == "expired":
            store._require_conn().execute("UPDATE jobs SET updated_at='2000-01-01T00:00:00Z'")
            store._require_conn().commit()
        second, created = await store.accept_job(
            "other" if case == "fingerprint" else fp, "other" if case == "repo" else "demo", 1800,
        )
        assert created
        assert first.id != second.id
        assert len(await store.list_jobs()) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failed_audit_insert_rolls_back_and_releases_transaction(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    try:
        conn = store._require_conn()
        conn.execute(
            "CREATE TRIGGER reject_audit BEFORE INSERT ON job_events "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
            await store.accept_job("same", "demo", 1800)
        assert not conn.in_transaction
        assert await store.list_jobs() == []
        conn.execute("DROP TRIGGER reject_audit")
        conn.commit()
        job, created = await store.accept_job("same", "demo", 1800)
        assert created
        assert len(await store.events(job.id)) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_cancelled_intake_keeps_connection_locked_until_transaction_finishes(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = store._find_open

    def blocked_find(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(store, "_find_open", blocked_find)
    task = asyncio.create_task(store.accept_job("same", "demo", 1800))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert store._lock.locked()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not store._require_conn().in_transaction
        _, created = await store.accept_job("same", "demo", 1800)
        assert not created
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await store.close()
