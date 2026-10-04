"""Bounded asyncio worker pool consuming jobs from the store.

Concurrency is bounded by the number of consumer tasks (``concurrency``,
default 3 — equivalent to the architecture doc's ``Semaphore(N=3)``).
Each job runs under a hard timeout; a job that times out or raises is
escalated with the failure recorded as evidence, never silently dropped.

Graceful shutdown drains the queue and waits for in-flight jobs, then cancels
stragglers after a deadline so the process can actually exit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .jobs import Job, JobStore, JobStoreError

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 3
DEFAULT_JOB_TIMEOUT_SECONDS = 1800
DEFAULT_DRAIN_TIMEOUT_SECONDS = 60


class JobHandler(Protocol):
    """执行链（W3）在服务层的接入点：驱动一个 job 从 received 走到终点。"""

    async def __call__(self, job: Job) -> None: ...


class WorkerPool:
    def __init__(
        self,
        store: JobStore,
        handler: JobHandler,
        concurrency: int = DEFAULT_CONCURRENCY,
        job_timeout: float = DEFAULT_JOB_TIMEOUT_SECONDS,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.store = store
        self.handler = handler
        self.concurrency = concurrency
        self.job_timeout = job_timeout
        self.drain_timeout = drain_timeout
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._consumers: list[asyncio.Task[None]] = []
        self._in_flight: set[str] = set()
        self._running = False

    # -- 查询 -------------------------------------------------------------

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def in_flight(self) -> frozenset[str]:
        return frozenset(self._in_flight)

    @property
    def running(self) -> bool:
        return self._running

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for i in range(self.concurrency):
            self._consumers.append(asyncio.create_task(self._consume(i), name=f"mewcode-worker-{i}"))
        log.info("worker pool started: concurrency=%d timeout=%ss", self.concurrency, self.job_timeout)

    async def stop(self, drain_timeout: float | None = None) -> None:
        """优雅退出：先让队列排空、在途 job 收尾，超时才取消。

        取消的 job 会被置为 escalate（附取消原因）——不能留在中间态，
        否则重启恢复时无法区分"被杀"与"正在跑"。
        """
        if not self._running:
            return
        self._running = False
        deadline = self.drain_timeout if drain_timeout is None else drain_timeout

        try:
            await asyncio.wait_for(self._queue.join(), timeout=deadline)
            log.info("worker pool drained cleanly")
        except TimeoutError:
            log.warning("worker pool drain timed out after %ss; cancelling consumers", deadline)

        for task in self._consumers:
            task.cancel()
        await asyncio.gather(*self._consumers, return_exceptions=True)
        self._consumers.clear()
        log.info("worker pool stopped")

    # -- 提交 -------------------------------------------------------------

    async def submit(self, job_id: str) -> None:
        if not self._running:
            raise RuntimeError("WorkerPool.start() must be awaited before submit()")
        await self.store.add_event(job_id, "queued", "submitted to worker pool")
        await self._queue.put(job_id)

    async def requeue_unfinished(self) -> int:
        """服务重启恢复：把非终态 job 重新入队。

        在途状态（如中途被杀在 fixing）先回退到 received 再入队——重跑
        执行链时 worktree 快速恢复会复现上次的代码现场（F3.4/F3.5）。
        """
        jobs = await self.store.list_unfinished()
        requeued = 0
        for job in jobs:
            if job.status != "received":
                try:
                    await self.store.reset_for_recovery(
                        job.id, reason="service restart: re-queued from last state"
                    )
                except JobStoreError as e:  # 理论不可达：非终态回退不会失败
                    log.warning("cannot requeue %s (%s): %s", job.id, job.status, e)
                    await self.store.add_event(job.id, "recovery_skipped", str(e))
                    continue
            await self.store.add_event(job.id, "recovered", f"re-queued from status={job.status}")
            await self._queue.put(job.id)
            requeued += 1
        if requeued:
            log.info("requeued %d unfinished job(s) after restart", requeued)
        return requeued

    # -- 消费 -------------------------------------------------------------

    async def _consume(self, index: int) -> None:
        while True:
            job_id = await self._queue.get()
            if job_id is None:
                self._queue.task_done()
                return
            try:
                await self._process(job_id, index)
            finally:
                self._queue.task_done()

    async def _process(self, job_id: str, index: int) -> None:
        job = await self.store.get(job_id)
        if job is None:
            log.error("worker %d: job %s disappeared from store", index, job_id)
            return
        if job.is_terminal:
            log.info("worker %d: skip terminal job %s (%s)", index, job_id, job.status)
            return

        self._in_flight.add(job_id)
        await self.store.add_event(job_id, "job_started", f"worker={index} status={job.status}")
        try:
            await asyncio.wait_for(self.handler(job), timeout=self.job_timeout)
            await self.store.add_event(job_id, "job_finished", "handler completed")
        except TimeoutError:
            await self._escalate_safely(job_id, f"job timeout after {self.job_timeout:.0f}s")
        except asyncio.CancelledError:
            # 优雅退出的兜底：等待 drain 超时后消费者被取消。错误现场先落库再重新抛出。
            await asyncio.shield(self._escalate_safely(job_id, "cancelled during service shutdown"))
            raise
        except Exception as e:  # handler 的任何异常都不能让 job 卡在中间态
            log.exception("worker %d: handler failed for %s", index, job_id)
            await self._escalate_safely(job_id, f"{type(e).__name__}: {e}")
        finally:
            self._in_flight.discard(job_id)

    async def _escalate_safely(self, job_id: str, reason: str) -> None:
        job = await self.store.get(job_id)
        if job is None or job.is_terminal:
            log.warning("escalate skipped for %s: %s", job_id, reason)
            return
        try:
            await self.store.transition(job_id, "escalate", reason=reason, last_error=reason)
        except JobStoreError as e:
            log.error("failed to escalate %s: %s", job_id, e)
