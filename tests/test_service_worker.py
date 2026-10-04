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
        """两个消费者必须真的并行，且上限不被突破。

        断言不赌 sleep 的时序（Windows CI 上 `peak == 2` 的采样式断言偶发假失败）：
        用握手让两个消费者在 handler 里相遇——只有真并发才能相遇，遇不上就是
        "上限没被突破但没有并行"或"上限被突破了"，都会明确失败。
        """
        async with open_store(tmp_path / "jobs.db") as store:
            running = 0
            peak = 0
            two_in_flight = asyncio.Event()

            async def handler(job):
                nonlocal running, peak
                running += 1
                peak = max(peak, running)
                if running >= 2:
                    two_in_flight.set()
                try:
                    # 等第二个消费者也进 handler（真并发的证据）；超时不是错误，
                    # 由下面的 peak 断言给出明确结论
                    await asyncio.wait_for(two_in_flight.wait(), timeout=1)
                except TimeoutError:
                    pass
                running -= 1

            pool = WorkerPool(store, handler, concurrency=2)
            await pool.start()
            try:
                for i in range(6):
                    job = await make_job(store, fingerprint=f"fp-{i}")
                    await pool.submit(job.id)
                await asyncio.wait_for(pool._queue.join(), timeout=10)
            finally:
                await pool.stop(drain_timeout=1)

            assert peak <= 2, f"并发上限被突破：peak={peak}"
            assert peak == 2, "两个消费者没有并行过（握手超时）"

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


class TestHumanWaitStatesNotResumed:
    """等人工的 job（human_review）不属于"被中断的工作"，重启恢复不得重跑。"""

    @pytest.mark.asyncio
    async def test_human_review_job_not_requeued(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            job = await make_job(store)
            for state in ("triaging", "reproducing", "fixing", "verifying", "pr_opened", "ci_gate"):
                await store.transition(job.id, state)
            job = await store.transition(job.id, "human_review", pr_url="https://x/pr/1")

            seen: list[str] = []

            async def handler(j):
                seen.append(j.status)

            pool = WorkerPool(store, handler, concurrency=1)
            await pool.start()
            try:
                assert await pool.requeue_unfinished() == 0
                await asyncio.sleep(0.1)
            finally:
                await pool.stop(drain_timeout=1)

            assert seen == []
            assert (await store.get_or_raise(job.id)).status == "human_review"
            assert await store.list_resumable() == []
            # 仍在"未完结"口径里（观测用），只是不参与恢复
            assert [j.id for j in await store.list_unfinished()] == [job.id]

    @pytest.mark.asyncio
    async def test_interrupted_and_human_wait_coexist(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            waiting = await make_job(store, fingerprint="fp-wait")
            for state in ("triaging", "reproducing", "fixing", "verifying", "pr_opened", "ci_gate"):
                await store.transition(waiting.id, state)
            await store.transition(waiting.id, "human_review")

            interrupted = await make_job(store, fingerprint="fp-int")
            for state in ("triaging", "reproducing", "fixing"):
                await store.transition(interrupted.id, state)

            assert [j.id for j in await store.list_resumable()] == [interrupted.id]


# =========================================================================
# E. handler 泄漏的取消：消费者不能被带走（真机事故的回归）
# =========================================================================

class TestLeakedHandlerCancellation:
    """MCP 收尾的 anyio 作用域取消会漏进 handler 的 await 链。

    真机事故：直跑模式下它落在收尾之后的第一个 await 上，打死执行链；消费者
    跟着退出后，服务活着却不再消费任何 job（concurrency=1 时等于静默停摆）。
    worker 侧必须：保住消费者、把 job 放回队列（有界）、达到上限 escalate。
    """

    @pytest.mark.asyncio
    async def test_consumer_survives_and_job_is_retried(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            calls: list[str] = []

            async def handler(job):
                calls.append(job.id)
                if len(calls) == 1:  # 第一次：模拟收尾窗口漏出的取消
                    raise asyncio.CancelledError("Cancelled via cancel scope test-scope")

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=5)
            await pool.start()
            try:
                job = await make_job(store)
                await pool.submit(job.id)
                await asyncio.wait_for(pool._queue.join(), timeout=10)
                # 消费者还活着：新 job 依然有人处理
                second = await make_job(store, fingerprint="fp-2")
                await pool.submit(second.id)
                await asyncio.wait_for(pool._queue.join(), timeout=10)
            finally:
                await pool.stop(drain_timeout=1)

            assert calls == [job.id, job.id, second.id]  # 被打断的 job 重跑了一次
            assert pool.queue_depth == 0
            events = await store.events(job.id)
            assert any(e.kind == "interrupted" and "pool was running" in e.detail for e in events)
            assert any(e.kind == "recovered" for e in events)
            final = await store.get_or_raise(job.id)
            assert final.status != "escalate"

    @pytest.mark.asyncio
    async def test_repeated_leaks_escalate_instead_of_looping_forever(self, tmp_path: Path):
        async with open_store(tmp_path / "jobs.db") as store:
            calls: list[str] = []

            async def handler(job):
                calls.append(job.id)
                raise asyncio.CancelledError("Cancelled via cancel scope test-scope")

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=5, spurious_cancel_limit=2)
            await pool.start()
            try:
                job = await make_job(store)
                await pool.submit(job.id)
                await asyncio.wait_for(pool._queue.join(), timeout=10)
                # 消费者仍在（第 2 次尝试后 escalate，不再重跑）
                second = await make_job(store, fingerprint="fp-2")
                await pool.submit(second.id)
                await asyncio.wait_for(pool._queue.join(), timeout=10)
            finally:
                await pool.stop(drain_timeout=1)

            assert calls.count(job.id) == 2  # 首次 + 一次重跑，然后收敛
            final = await store.get_or_raise(job.id)
            assert final.is_terminal and final.status == "escalate"
            assert "handler-level cancellation" in (final.last_error or "")
            assert calls.count(second.id) == 2  # 消费者活着，第二个 job 同样被处理

    @pytest.mark.asyncio
    async def test_stop_wakes_a_consumer_that_swallowed_cancellation(self, tmp_path: Path):
        """消费者把关停取消吞掉时，stop() 用队列哨兵收尾，不能把自己挂死。"""
        async with open_store(tmp_path / "jobs.db") as store:
            started = asyncio.Event()
            finished = asyncio.Event()

            async def handler(job):
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.2)  # 模拟收尾窗口里吞掉取消、继续干完
                finished.set()

            pool = WorkerPool(store, handler, concurrency=1, job_timeout=30)
            await pool.start()
            job = await make_job(store)
            await pool.submit(job.id)
            await asyncio.wait_for(started.wait(), timeout=5)

            await asyncio.wait_for(pool.stop(drain_timeout=0), timeout=10)
            assert finished.is_set()
            assert not pool.running
