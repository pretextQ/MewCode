"""M1 W1: worker 池的并发上限、超时强杀、优雅退出与重启恢复测试。

这些是无人值守服务的地基性质：job 不能卡在中间态、超时必须收敛、
退出时在途任务要显式收尾（仓库已知坑：不依赖 asyncio.run 兜底）。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from mewcode.service.jobs import JobStore
from mewcode.service.worker import WorkerPool


@asynccontextmanager
async def open_store(db_path: Path):
    store = JobStore(db_path)
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


async def make_job(store: JobStore, fingerprint: str = "fp"):
    return await store.create_job(fingerprint=fingerprint, repo="demo")


# =========================================================================
# A. 并发上限
# =========================================================================

class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrency_cap_respected(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            running = 0
            peak = 0

            async def handler(job):
                nonlocal running, peak
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.05)
                running -= 1

            pool = WorkerPool(store, handler, concurrency=2)
            await pool.start()
            try:
                for i in range(6):
                    job = await make_job(store, fingerprint=f"fp-{i}")
                    await pool.submit(job.id)
                await asyncio.wait_for(pool._queue.join(), timeout=5)
            finally:
                await pool.stop(drain_timeout=1)
            assert peak == 2

    @pytest.mark.asyncio
    async def test_submit_before_start_rejected(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            pool = WorkerPool(store, lambda job: None)  # type: ignore[arg-type]
            job = await make_job(store)
            with pytest.raises(RuntimeError):
                await pool.submit(job.id)


# =========================================================================
# B. 失败路径必须落库收敛（不能卡在中间态）
# =========================================================================

class TestFailureConvergence:
    @pytest.mark.asyncio
    async def test_timeout_escalates_job(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            async def handler(job):
                await asyncio.sleep(30)

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=0.2)
            await pool.start()
            try:
                job = await make_job(store)
                await pool.submit(job.id)
                await asyncio.wait_for(pool._queue.join(), timeout=5)
            finally:
                await pool.stop(drain_timeout=1)

            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "timeout" in final.last_error
            events = await store.events(job.id)
            assert any(e.kind == "job_started" for e in events)
            assert any("timeout" in e.detail for e in events if e.kind == "transition")

    @pytest.mark.asyncio
    async def test_handler_exception_escalates_job(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            async def handler(job):
                raise RuntimeError("worktree creation failed")

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=5)
            await pool.start()
            try:
                job = await make_job(store)
                await pool.submit(job.id)
                await asyncio.wait_for(pool._queue.join(), timeout=5)
            finally:
                await pool.stop(drain_timeout=1)

            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "worktree creation failed" in final.last_error

    @pytest.mark.asyncio
    async def test_handler_that_already_escalated_is_not_double_transitioned(self, tmp_path: Path):
        """handler 已把 job 置为终态后再抛异常——worker 不得重复转移（会抛非法转移）。"""
        async with open_store(tmp_path / "jobs.db") as store:
            async def handler(job):
                await store.transition(job.id, "invalid", reason="empty payload")
                raise RuntimeError("boom after terminal")

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=5)
            await pool.start()
            try:
                job = await make_job(store)
                await pool.submit(job.id)
                await asyncio.wait_for(pool._queue.join(), timeout=5)
            finally:
                await pool.stop(drain_timeout=1)

            final = await store.get_or_raise(job.id)
            assert final.status == "invalid"  # 保持 handler 的判定

    @pytest.mark.asyncio
    async def test_terminal_job_skipped(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            calls = []

            async def handler(job):
                calls.append(job.id)

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=5)
            await pool.start()
            try:
                job = await make_job(store)
                await store.transition(job.id, "invalid")
                await pool.submit(job.id)
                await asyncio.wait_for(pool._queue.join(), timeout=5)
            finally:
                await pool.stop(drain_timeout=1)
            assert calls == []


# =========================================================================
# C. 优雅退出
# =========================================================================

class TestGracefulShutdown:
    @pytest.mark.asyncio
    async def test_stop_drains_queue_and_inflight(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            done: list[str] = []

            async def handler(job):
                await asyncio.sleep(0.05)
                done.append(job.id)

            pool = WorkerPool(store, handler, concurrency=2, job_timeout=5)
            await pool.start()
            for i in range(4):
                job = await make_job(store, fingerprint=f"fp-{i}")
                await pool.submit(job.id)

            await pool.stop(drain_timeout=5)  # 队列排空后才返回
            assert len(done) == 4
            assert pool.queue_depth == 0
            assert not pool.running

    @pytest.mark.asyncio
    async def test_stop_cancels_stragglers_without_terminalising_them(self, tmp_path: Path):
        """drain 超时被取消：只落中断事件、不改状态——重启恢复要靠它保持未完结。"""
        async with open_store(tmp_path / "jobs.db") as store:
            started = asyncio.Event()

            async def handler(job):
                started.set()
                await asyncio.sleep(30)

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=60)
            await pool.start()
            job = await make_job(store)
            await pool.submit(job.id)
            await asyncio.wait_for(started.wait(), timeout=5)

            await pool.stop(drain_timeout=0.2)  # drain 超时 -> 取消在途
            final = await store.get_or_raise(job.id)
            assert final.status == "received"          # 状态保留，未变终态
            assert not final.is_terminal
            events = await store.events(job.id)
            assert any(e.kind == "interrupted" for e in events)

    @pytest.mark.asyncio
    async def test_interrupted_job_is_recovered_on_restart(self, tmp_path: Path):
        """被取消的 job 在下一次启动时由 requeue_unfinished 接续（验收标准 3）。"""
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            await store.transition(job.id, "triaging")
            await store.transition(job.id, "reproducing")
            await store.transition(job.id, "fixing")
            await store.add_event(job.id, "interrupted", "cancelled during service shutdown")

            seen: list[str] = []

            async def handler(j):
                seen.append(j.status)

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=5)
            await pool.start()
            try:
                assert await pool.requeue_unfinished() == 1
                await asyncio.wait_for(pool._queue.join(), timeout=5)
            finally:
                await pool.stop(drain_timeout=1)
            assert seen == ["received"]
            kinds = {e.kind for e in await store.events(job.id)}
            assert {"interrupted", "recovery_reset", "recovered"} <= kinds

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            async def handler(job):
                return None

            pool = WorkerPool(store, handler, concurrency=1)
            await pool.start()
            await pool.stop(drain_timeout=1)
            await pool.stop(drain_timeout=1)  # 第二次是 no-op


# =========================================================================
# D. 重启恢复
# =========================================================================

class TestRecovery:
    @pytest.mark.asyncio
    async def test_requeue_unfinished_resets_and_processes(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            # 模拟崩溃现场：一个 job 卡在 fixing（在途中被杀）
            job = await make_job(store)
            await store.transition(job.id, "triaging")
            await store.transition(job.id, "reproducing")
            await store.transition(job.id, "fixing")
            done = await make_job(store, fingerprint="fp-done")
            await store.transition(done.id, "invalid")

            seen: list[tuple[str, str]] = []

            async def handler(j):
                seen.append((j.id, j.status))

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=5)
            await pool.start()
            try:
                n = await pool.requeue_unfinished()
                await asyncio.wait_for(pool._queue.join(), timeout=5)
            finally:
                await pool.stop(drain_timeout=1)

            assert n == 1  # 终态 job 不入队
            assert seen == [(job.id, "received")]
            events = await store.events(job.id)
            assert any(e.kind == "recovery_reset" for e in events)
            assert any(e.kind == "recovered" for e in events)
