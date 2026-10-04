
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from mewcode.worktree.changes import has_unpushed_commits, has_worktree_changes
from mewcode.worktree.manager import WorktreeManager

log = logging.getLogger(__name__)

EPHEMERAL_PATTERNS = [
    # 生成器产出 "agent-" + 8 位随机 hex（integration.generate_worktree_name）；
    # 旧正则要求首字符恰为 'a'，约 94% 的 worktree 永不回收
    re.compile(r"^agent-[0-9a-f]{8}$"),
    re.compile(r"^wf_[0-9a-f]{8}-[0-9a-f]{3}-\d+$"),
    re.compile(r"^wf-\d+$"),
    re.compile(r"^bridge-[A-Za-z0-9_]+(-[A-Za-z0-9_]+)*$"),
    re.compile(r"^job-[a-zA-Z0-9._-]{1,55}-[0-9a-f]{8}$"),
    # 服务层（M1）用 job id 原样命名 worktree：job-<12 位 hex>。
    # 24/7 服务每来一个告警建一个 worktree，不纳入回收会直接拖垮磁盘。
    re.compile(r"^job-[0-9a-f]{12}$"),
]


def _is_ephemeral(name: str) -> bool:
    return any(p.match(name) for p in EPHEMERAL_PATTERNS)


async def cleanup_stale_worktrees(manager: WorktreeManager, cutoff_hours: int) -> int:
    cutoff = datetime.now() - timedelta(hours=cutoff_hours)
    removed = 0
    worktree_dir = Path(manager.worktree_dir)

    if not worktree_dir.exists():
        return 0

    for entry in worktree_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name

        if not _is_ephemeral(name):
            continue

        if manager.current_session and manager.current_session.worktree_name == name:
            continue

        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime)
            if mtime > cutoff:
                continue
        except OSError:
            continue

        head_sha = WorktreeManager.read_worktree_head_sha(str(entry))
        if head_sha is None:
            continue

        if has_worktree_changes(str(entry), head_sha):
            continue

        if has_unpushed_commits(str(entry)):
            continue

        try:
            flat_name = name
            if flat_name in manager.active:
                await manager._remove_worktree(flat_name, manager.active[flat_name])
            else:
                result = await asyncio.to_thread(
                    manager._run_git,
                    ["worktree", "remove", "--force", str(entry)],
                )
                if result.returncode == 0:
                    await asyncio.sleep(0.1)
                    await asyncio.to_thread(
                        manager._run_git, ["branch", "-D", f"worktree-{flat_name}"]
                    )
            removed += 1
            log.info("Cleaned up stale worktree: %s", name)
        except Exception as e:
            log.warning("Failed to clean up stale worktree %s: %s", name, e)

    return removed


async def start_stale_cleanup_task(
    manager: WorktreeManager,
    interval: int,
    cutoff_hours: int,
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            count = await cleanup_stale_worktrees(manager, cutoff_hours)
            if count:
                log.info("Stale worktree cleanup removed %d worktrees", count)
        except Exception as e:
            log.warning("Stale worktree cleanup error: %s", e)

