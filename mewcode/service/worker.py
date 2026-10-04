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
#: 同一个 job 被"handler 泄漏的取消"打断的上限：达到即 escalate。
#: 每次重跑都要重新烧一遍 agent 的 token，不能无限循环（真机事故见
#: `_resume_after_handler_cancellation` 的说明）。
DEFAULT_SPURIOUS_CANCEL_LIMIT = 2
#: stop() 取消消费者后的宽限期：正常情况取消立刻生效，超过说明有消费者把
#: 取消吞掉了（在途 job 收尾窗口里的刻意行为），这时才动用队列哨兵兜底。
CANCEL_GRACE_SECONDS = 1.0


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
        spurious_cancel_limit: int = DEFAULT_SPURIOUS_CANCEL_LIMIT,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.store = store
        self.handler = handler
        self.concurrency = concurrency
        self.job_timeout = job_timeout
        self.drain_timeout = drain_timeout
        self.spurious_cancel_limit = max(1, spurious_cancel_limit)
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

        取消的 job 会被置回未完结状态（附中断事件）——不能留在中间态，
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
        _, pending = await asyncio.wait(self._consumers, timeout=CANCEL_GRACE_SECONDS)
        if pending:
            # 有消费者把取消吞掉了（在途收尾窗口里吞取消是刻意行为，见
            # execution.shutdown_agent_resources）：用队列哨兵让它在下一次
            # 循环退出——否则 gather 会一直等，服务卡在关停上。哨兵只在
            # 真需要时才发，免得污染 stop 之后的队列深度。
            log.warning(
                "worker pool: %d consumer(s) survived cancellation; waking them with sentinels",
                len(pending),
            )
            for _ in pending:
                self._queue.put_nowait(None)
            await asyncio.gather(*pending, return_exceptions=True)
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

        等人工的等待态（human_review）被排除在外——那不是"被中断的工作"，
        而是"球在人手里"，重启重跑只会白白烧 token 并重复 push。

        在途状态（如中途被杀在 fixing）先回退到 received 再入队——重跑
        执行链时 worktree 快速恢复会复现上次的代码现场（F3.4/F3.5）。
        """
        jobs = await self.store.list_resumable()
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
            if not self._running:
                # 优雅退出的兜底：drain 超时后消费者被取消。保留原状态、只落一条中断事件——
                # 服务重启时 requeue_unfinished 会把它从最后状态捡起来继续跑；若这里
                # escalate，job 就变成"需要人工介入"，重启恢复永远轮不到它。
                await asyncio.shield(self._record_interruption(job_id))
                raise
            # 池子还在跑 => 这不是关停信号，而是 handler 链路里泄漏出来的取消
            # （真机踩到：MCP 收尾的 anyio 作用域取消投递在收尾之后的第一个 await
            # 上，顺着 CancelledError 打死执行链；消费者要是跟着退出，服务就活着
            # 却不再消费任何 job——concurrency=1 时等于整服务静默停摆）。
            # 消费者必须活下来，job 放回队列继续跑（次数有界）。
            log.exception(
                "worker %d: handler cancelled while the pool is running (%s); "
                "keeping the consumer alive", index, job_id,
            )
            await self._resume_after_handler_cancellation(job_id, index)
        except Exception as e:  # handler 的任何异常都不能让 job 卡在中间态
            log.exception("worker %d: handler failed for %s", index, job_id)
            await self._escalate_safely(job_id, f"{type(e).__name__}: {e}")
        finally:
            self._in_flight.discard(job_id)

    async def _record_interruption(self, job_id: str) -> None:
        """记录中断现场而不改状态（供重启恢复接续）。"""
        try:
            job = await self.store.get(job_id)
            if job is None or job.is_terminal:
                return
            await self.store.add_event(
                job_id, "interrupted", f"cancelled during service shutdown (status={job.status})"
            )
        except JobStoreError as e:  # pragma: no cover - 落库失败只能记日志
            log.error("failed to record interruption for %s: %s", job_id, e)

    async def _resume_after_handler_cancellation(self, job_id: str, index: int) -> None:
        """handler 泄漏的取消：保住消费者，把 job 放回队列继续跑（次数有界）。

        状态机没有"从中间态原地续跑"的语义，这里复用重启恢复那一套：
        `reset_for_recovery` 回退到 received 再入队（worktree 的快速恢复会接着
        上次的代码现场跑）。同一个 job 连续被打断到上限就 escalate——不无限重跑
        烧 token（真机事故：M2 降级直跑路径上，MCP 收尾的伪取消每轮都来一次，
        重跑永远到不了终点，必须有个收敛点）。
        """
        try:
            job = await self.store.get(job_id)
            if job is None or job.is_terminal:
                return
            prior = sum(
                1
                for event in await self.store.events(job_id)
                if event.kind == "interrupted" and "pool was running" in event.detail
            )
            occurrence = prior + 1
            await self.store.add_event(
                job_id,
                "interrupted",
                f"handler cancelled while the worker pool was running "
                f"(occurrence {occurrence}); the consumer stays alive and the job is re-queued",
            )
            if occurrence >= self.spurious_cancel_limit:
                reason = (
                    f"job interrupted {occurrence} time(s) by a handler-level cancellation "
                    "while the worker pool was running; escalating instead of re-running forever"
                )
                await self.store.transition(job_id, "escalate", reason=reason, last_error=reason)
                return
            if job.status != "received":
                await self.store.reset_for_recovery(
                    job_id, reason="re-queued after a handler-level cancellation"
                )
            await self.store.add_event(
                job_id, "recovered", "re-queued after a handler-level cancellation"
            )
            await self._queue.put(job_id)
        except asyncio.CancelledError:
            # 关停时的真取消：原样交给上层（消费者正常退出）
            raise
        except Exception as e:  # 这段自身失败也不能把消费者带走
            log.error("failed to re-queue %s after a leaked cancellation: %s", job_id, e)

    async def _escalate_safely(self, job_id: str, reason: str) -> None:
        job = await self.store.get(job_id)
        if job is None or job.is_terminal:
            log.warning("escalate skipped for %s: %s", job_id, reason)
            return
        try:
            await self.store.transition(job_id, "escalate", reason=reason, last_error=reason)
        except JobStoreError as e:
            log.error("failed to escalate %s: %s", job_id, e)
