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
from .policy import PolicyError, RepoPolicyLoader
from .triggers.base import JobDraft
from .worker import JobHandler, WorkerPool

log = logging.getLogger(__name__)


@dataclass
class IntakeResult:
    """一次 intake 的结果：新入队的 job 与合并到已有 job 的告警。"""

    accepted: list[Job] = field(default_factory=list)
    deduped: list[Job] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 被仓库策略拒收的告警（repo/fingerprint/title/reason）——
    #: "告警为什么没被修"必须在响应里可回答（M3 W2 触发路由）
    rejected: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "accepted": [{"id": j.id, "status": j.status, "repo": j.repo} for j in self.accepted],
            "deduped": [{"id": j.id, "status": j.status, "repo": j.repo} for j in self.deduped],
            "warnings": self.warnings,
            "rejected": self.rejected,
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
        policy_loader: RepoPolicyLoader | None = None,
    ) -> None:
        self.config = config
        self._handler = handler
        data_dir = Path(repo_root or ".") / config.data_dir
        self.store = store or JobStore(data_dir / "jobs.db")
        self.notifier = notifier
        self.policy_loader = policy_loader or RepoPolicyLoader(config.repos)
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
            # 策略在门口判定：不匹配的告警拒收（响应里给原因），不建 job、
            # 也不进去重——路由语义优先于"告警又响了一次"的证据合并
            try:
                policy = self.policy_loader.load(draft.repo)
            except PolicyError as e:
                result.rejected.append(
                    {
                        "repo": draft.repo,
                        "fingerprint": draft.fingerprint,
                        "title": draft.title,
                        "reason": f"repo policy unreadable: {e}",
                    }
                )
                log.warning("rejected alert for %s: %s", draft.repo, e)
                continue
            if not policy.accepts(draft.severity):
                reason = (
                    f"repo policy only accepts severities: "
                    f"{', '.join(policy.severities)} (got '{draft.severity}')"
                )
                result.rejected.append(
                    {
                        "repo": draft.repo,
                        "fingerprint": draft.fingerprint,
                        "title": draft.title,
                        "reason": reason,
                    }
                )
                log.info("rejected alert for %s: %s", draft.repo, reason)
                continue
            job, created = await self.store.accept_job(
                fingerprint=draft.fingerprint,
                repo=draft.repo,
                window_seconds=self.config.dedup_window_seconds,
                severity=draft.severity,
                title=draft.title,
                payload=draft.payload,
            )
            if not created:
                result.deduped.append(job)
                log.info("job %s: merged duplicate alert (fingerprint=%s)", job.id, draft.fingerprint)
                continue
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
