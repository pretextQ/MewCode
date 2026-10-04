"""服务运行时：把 JobStore、WorkerPool 与触发适配器组装成可启停的整体。

职责边界：
- 落库与去重（同仓库 + 指纹 + 未完结 + 窗口内 => 合并到已有 job）；
- 入队（交给 WorkerPool）；
- 生命周期（start/stop：启动时恢复未完结 job，退出时优雅 drain）。

执行链（W3）通过构造参数注入 handler —— 运行时本身不认识 agent 内核，
这也是"服务层是外壳、内核保持解耦"（架构文档决策 1）的落地方式。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mewcode.config import ServiceConfig

from .jobs import Job, JobStore
from .triggers.base import JobDraft
from .worker import JobHandler, WorkerPool

log = logging.getLogger(__name__)


async def unconfigured_handler(job: Job) -> None:
    """W1 占位执行链：服务能收单、落库、可观测，但还没有执行内核。

    抛错而不是静默返回——worker 会把 job 收敛到 escalate 并记录原因，
    job 不会卡在 received 假装"在处理"。W3 用真实执行链替换这一注入点。
    """
    raise RuntimeError("execution chain not configured (agent wiring lands in W3)")


@dataclass
class IntakeResult:
    """一次 intake 的结果：新入队的 job 与合并到已有 job 的告警。"""

    accepted: list[Job] = field(default_factory=list)
    deduped: list[Job] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "accepted": [{"id": j.id, "status": j.status, "repo": j.repo} for j in self.accepted],
            "deduped": [{"id": j.id, "status": j.status, "repo": j.repo} for j in self.deduped],
            "warnings": self.warnings,
        }


class ServiceRuntime:
    def __init__(
        self,
        config: ServiceConfig,
        handler: JobHandler,
        store: JobStore | None = None,
        repo_root: str | Path | None = None,
        worktree_cleanup_cutoff_hours: int | None = 24,
        notifier: Any = None,
    ) -> None:
        self.config = config
        self._handler = handler
        data_dir = Path(repo_root or ".") / config.data_dir
        self.store = store or JobStore(data_dir / "jobs.db")
        self.notifier = notifier
        self.pool = WorkerPool(
            store=self.store,
            handler=handler,
            concurrency=config.concurrency,
            job_timeout=config.job_timeout_seconds,
            drain_timeout=config.drain_timeout_seconds,
        )
        self.worktree_cleanup_cutoff_hours = worktree_cleanup_cutoff_hours
        self._data_dir = data_dir

    # -- 生命周期 ---------------------------------------------------------

    async def start(self, recover: bool = True) -> None:
        await self.store.connect()
        await self.pool.start()
        if self.worktree_cleanup_cutoff_hours is not None:
            await self._cleanup_worktrees()
        if recover:
            await self.pool.requeue_unfinished()
        log.info(
            "service runtime started: concurrency=%d port=%d data_dir=%s",
            self.config.concurrency,
            self.config.port,
            self.config.data_dir,
        )

    async def _cleanup_worktrees(self) -> None:
        """启动时顺带清一次陈旧 worktree（架构文档风险表：24/7 运行磁盘只增不减）。"""
        from mewcode.worktree import WorktreeManager
        from mewcode.worktree.cleanup import cleanup_stale_worktrees

        for name, repo in self.config.repos.items():
            path = Path(repo.path)
            if not path.is_dir():
                continue
            try:
                manager = WorktreeManager(repo_root=str(path), symlink_directories=[])
                removed = await cleanup_stale_worktrees(
                    manager, self.worktree_cleanup_cutoff_hours or 24
                )
                if removed:
                    log.info("cleaned %d stale worktree(s) in %s", removed, name)
            except Exception as e:  # 清理失败不能阻止服务启动
                log.warning("worktree cleanup failed for %s: %s", name, e)

    async def stop(self) -> None:
        await self.pool.stop()
        await self.store.close()

    # -- 接入 -------------------------------------------------------------

    async def intake(self, drafts: list[JobDraft]) -> IntakeResult:
        """去重后落库并入队；返回本次受理结果。

        去重语义（架构文档第四节）：同仓库 + 同指纹 + 未完结 + 窗口内 =>
        不新建 job，把这次触发记录为已有 job 的一条事件（保留"告警又响了一次"
        的证据，但不重复消耗 token）。
        """
        result = IntakeResult()
        for draft in drafts:
            result.warnings.extend(draft.warnings)
            existing = await self.store.find_open_by_fingerprint(
                draft.repo, draft.fingerprint, self.config.dedup_window_seconds
            )
            if existing is not None:
                await self.store.add_event(
                    existing.id, "deduped", f"duplicate alert merged (fingerprint={draft.fingerprint})"
                )
                result.deduped.append(existing)
                log.info("job %s: merged duplicate alert (fingerprint=%s)", existing.id, draft.fingerprint)
                continue

            job = await self.store.create_job(
                fingerprint=draft.fingerprint,
                repo=draft.repo,
                severity=draft.severity,
                title=draft.title,
                payload=draft.payload,
            )
            if self.pool.running:
                await self.pool.submit(job.id)
            result.accepted.append(job)
            if self.notifier is not None:
                try:
                    await self.notifier.notify_job_event(
                        job, "received", f"severity={job.severity} title={job.title or '(no title)'}"
                    )
                except Exception as e:  # 通知失败不影响受理
                    log.warning("notify failed for %s: %s", job.id, e)
        return result
