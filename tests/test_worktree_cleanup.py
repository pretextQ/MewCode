"""F4.3: worktree cleanup 的测试——审查报告覆盖盲区。

覆盖 F3.5① 的 ephemeral 正则（旧正则要求首字符恰为 'a'，约 94% 的
worktree 永不回收）与 stale 判定/双保险（有变更、有未推送提交则不清理）。
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from mewcode.cache import FileCache
from mewcode.worktree.cleanup import _is_ephemeral, cleanup_stale_worktrees
from mewcode.worktree.manager import WorktreeManager
from mewcode.worktree.models import WorktreeSession

# =========================================================================
# A. ephemeral 正则
# =========================================================================

class TestEphemeralPatterns:
    def test_agent_prefix_any_hex_start(self):
        # F3.5 修复点：旧正则 ^agent-a[0-9a-f]{7}$ 漏掉非 'a' 开头的名字
        assert _is_ephemeral("agent-0123abcd")
        assert _is_ephemeral("agent-a1b2c3d4")
        assert _is_ephemeral("agent-ffffffff")
        assert _is_ephemeral("agent-00000000")

    def test_agent_prefix_wrong_length_rejected(self):
        assert not _is_ephemeral("agent-123")
        assert not _is_ephemeral("agent-0123abcd0")
        assert not _is_ephemeral("agent-")

    def test_wf_generated_names(self):
        assert _is_ephemeral("wf_0123abcd-001-1")
        assert _is_ephemeral("wf-42")
        assert not _is_ephemeral("wf_0123abcd-001")
        assert not _is_ephemeral("wf-")

    def test_bridge_names(self):
        assert _is_ephemeral("bridge-team1")
        assert _is_ephemeral("bridge-team1-alice")
        assert not _is_ephemeral("bridge-")

    def test_job_names(self):
        assert _is_ephemeral("job-some.task_name-0123abcd")
        assert not _is_ephemeral("job-0123abcd")

    def test_human_named_worktrees_never_match(self):
        assert not _is_ephemeral("test-feature")
        assert not _is_ephemeral("team-alice")
        assert not _is_ephemeral("v1.0")
        assert not _is_ephemeral("agent-0123abcd-extra")


# =========================================================================
# B. stale 清理流程（真实 git 仓库）
# =========================================================================

def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(path), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True)
    (path / "README.md").write_text("# Test", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), capture_output=True, check=True)
    # 无远端的仓库里任何提交都算"未推送"（has_unpushed_commits 恒 True），
    # cleanup 双保险会把所有 worktree 都保下来——挂一个 bare remote 并推送，
    # 让"未推送"判定有真实语义。
    remote = path.parent / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "push", "-u", "origin", "HEAD"], cwd=str(path), capture_output=True, check=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    return repo


@pytest.fixture
def manager(git_repo: Path) -> WorktreeManager:
    return WorktreeManager(
        repo_root=str(git_repo),
        file_cache=FileCache(),
        symlink_directories=[],
    )


def _age_entry(entry: Path, hours: int) -> None:
    import os
    import time

    stale = time.time() - hours * 3600
    os.utime(entry, (stale, stale))


def test_cleanup_removes_stale_ephemeral_worktree(
    manager: WorktreeManager, git_repo: Path
) -> None:
    asyncio.run(manager.create("agent-0123abcd"))
    entry = Path(manager.worktree_dir) / "agent-0123abcd"
    assert entry.is_dir()
    _age_entry(entry, hours=48)

    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))

    assert removed == 1
    assert not entry.exists()


def test_cleanup_skips_recent_worktree(
    manager: WorktreeManager, git_repo: Path
) -> None:
    asyncio.run(manager.create("agent-0123abcd"))
    entry = Path(manager.worktree_dir) / "agent-0123abcd"

    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))

    assert removed == 0
    assert entry.is_dir()


def test_cleanup_skips_non_ephemeral_names(
    manager: WorktreeManager, git_repo: Path
) -> None:
    asyncio.run(manager.create("human-feature"))
    entry = Path(manager.worktree_dir) / "human-feature"
    _age_entry(entry, hours=48)

    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))

    assert removed == 0
    assert entry.is_dir()


def test_cleanup_skips_current_session_worktree(
    manager: WorktreeManager, git_repo: Path
) -> None:
    wt = asyncio.run(manager.create("agent-0123abcd"))
    manager.current_session = WorktreeSession(
        original_cwd=str(git_repo),
        worktree_path=wt.path,
        worktree_name=wt.name,
        original_branch="main",
        original_head_commit=wt.head_commit,
    )
    entry = Path(manager.worktree_dir) / "agent-0123abcd"
    _age_entry(entry, hours=48)

    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))

    assert removed == 0
    assert entry.is_dir()
    manager.current_session = None


def test_cleanup_keeps_worktree_with_uncommitted_changes(
    manager: WorktreeManager, git_repo: Path
) -> None:
    asyncio.run(manager.create("agent-0123abcd"))
    entry = Path(manager.worktree_dir) / "agent-0123abcd"
    _age_entry(entry, hours=48)

    # 双保险 1：有未提交变更 → 不清理
    (entry / "README.md").write_text("# modified", encoding="utf-8")

    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))

    assert removed == 0
    assert entry.is_dir()


def test_cleanup_keeps_worktree_with_unpushed_commits(
    manager: WorktreeManager, git_repo: Path
) -> None:
    asyncio.run(manager.create("agent-0123abcd"))
    entry = Path(manager.worktree_dir) / "agent-0123abcd"
    _age_entry(entry, hours=48)

    # 双保险 2：干净但含未推送提交 → 不清理（单仓库内没有远端，
    # 任何新提交相对 HEAD 之前的分支都算未推送）
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "wip"],
        cwd=str(entry), capture_output=True, check=True,
    )

    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))

    assert removed == 0
    assert entry.is_dir()


def test_cleanup_missing_dir_returns_zero(manager: WorktreeManager) -> None:
    manager.worktree_dir = str(Path(manager.worktree_dir) / "does-not-exist")
    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))
    assert removed == 0


def test_cleanup_skips_files_and_unreadable_entries(
    manager: WorktreeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt_dir = Path(manager.worktree_dir)
    wt_dir.mkdir(parents=True, exist_ok=True)
    (wt_dir / "not-a-dir.txt").write_text("x", encoding="utf-8")
    _age_entry(wt_dir / "not-a-dir.txt", hours=48)

    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))
    assert removed == 0
    assert (wt_dir / "not-a-dir.txt").exists()


def test_cleanup_skips_when_head_sha_unreadable(
    manager: WorktreeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(manager.create("agent-0123abcd"))
    entry = Path(manager.worktree_dir) / "agent-0123abcd"
    _age_entry(entry, hours=48)
    monkeypatch.setattr(
        WorktreeManager, "read_worktree_head_sha", classmethod(lambda cls, p: None)
    )

    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))
    assert removed == 0
    assert entry.is_dir()


def test_cleanup_counts_removal_failure_as_not_removed(
    manager: WorktreeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_remove(name: str, session: object) -> None:
        raise RuntimeError("disk on fire")

    asyncio.run(manager.create("agent-0123abcd"))
    entry = Path(manager.worktree_dir) / "agent-0123abcd"
    _age_entry(entry, hours=48)
    monkeypatch.setattr(manager, "_remove_worktree", failing_remove)

    removed = asyncio.run(cleanup_stale_worktrees(manager, cutoff_hours=24))
    assert removed == 0


@pytest.mark.asyncio
async def test_start_stale_cleanup_task_runs_periodically(
    manager: WorktreeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mewcode.worktree import cleanup as cleanup_mod

    calls: list[int] = []

    async def fake_cleanup(mgr: WorktreeManager, cutoff: int) -> int:
        calls.append(cutoff)
        return 0

    monkeypatch.setattr(cleanup_mod, "cleanup_stale_worktrees", fake_cleanup)

    task = asyncio.create_task(
        cleanup_mod.start_stale_cleanup_task(manager, interval=0.01, cutoff_hours=24)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) >= 2



class TestServiceWorktreePatterns:
    """M1 服务层 worktree（job-<12 hex>）必须纳入回收——24/7 服务磁盘只增不减会拖垮服务。"""

    def test_service_job_worktree_is_ephemeral(self):
        assert _is_ephemeral("job-0123456789ab")
        assert _is_ephemeral("job-ffffffffffff")
        assert _is_ephemeral("job-0a1b2c3d4e5f")

    def test_service_job_pattern_rejects_other_shapes(self):
        assert not _is_ephemeral("job-0123456789")      # 10 位
        assert not _is_ephemeral("job-0123456789abcd")  # 14 位
        assert not _is_ephemeral("job-ZZZZZZZZZZZZ")    # 非 hex
        assert not _is_ephemeral("job-")
