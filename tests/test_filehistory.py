"""F4.3: FileHistory（文件快照 / 回滚）的测试——审查报告覆盖盲区。

覆盖主流程：track_edit 备份、make_snapshot、rewind 恢复与新建文件删除、
快照裁剪、OSError 容错。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.filehistory.history import MAX_SNAPSHOTS, FileHistory


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def fh(work_dir: Path) -> FileHistory:
    return FileHistory(str(work_dir), "sess-1")


def test_track_edit_creates_backup_and_bumps_version(
    fh: FileHistory, work_dir: Path
) -> None:
    f = work_dir / "a.txt"
    f.write_text("v1", encoding="utf-8")

    fh.track_edit(str(f))
    fh.track_edit(str(f))

    ver = fh._tracked[str(f.resolve())]
    assert ver == 2
    backup = fh._session_dir / fh._backup_name(str(f.resolve()), 2)
    assert backup.exists()
    assert backup.read_bytes() == b"v1"


def test_track_edit_missing_file_does_not_crash(
    fh: FileHistory, work_dir: Path
) -> None:
    missing = work_dir / "never-existed.txt"
    fh.track_edit(str(missing))
    assert fh._tracked[str(missing.resolve())] == 1
    # 没有备份文件产生
    assert not (fh._session_dir / fh._backup_name(str(missing.resolve()), 1)).exists()


def test_make_snapshot_records_tracked_files(
    fh: FileHistory, work_dir: Path
) -> None:
    f1 = work_dir / "a.txt"
    f1.write_text("A", encoding="utf-8")
    f2 = work_dir / "b.txt"
    f2.write_text("B", encoding="utf-8")

    fh.track_edit(str(f1))
    fh.track_edit(str(f2))
    fh.make_snapshot(msg_index=3, user_text="checkpoint")

    snaps = fh.get_snapshots()
    assert len(snaps) == 1
    assert snaps[0].message_index == 3
    assert snaps[0].user_text == "checkpoint"
    assert set(snaps[0].backups) == {str(f1.resolve()), str(f2.resolve())}
    assert fh.has_snapshots() is True


def test_make_snapshot_prunes_beyond_max(fh: FileHistory) -> None:
    for i in range(MAX_SNAPSHOTS + 5):
        fh.make_snapshot(msg_index=i, user_text=f"s{i}")

    snaps = fh.get_snapshots()
    assert len(snaps) == MAX_SNAPSHOTS
    assert snaps[0].message_index == 5
    assert snaps[-1].message_index == MAX_SNAPSHOTS + 4


def test_rewind_restores_modified_file(
    fh: FileHistory, work_dir: Path
) -> None:
    f = work_dir / "a.txt"
    f.write_text("original", encoding="utf-8")
    fh.track_edit(str(f))
    fh.make_snapshot(msg_index=0, user_text="before")

    f.write_text("modified", encoding="utf-8")
    fh.track_edit(str(f))

    changed = fh.rewind(0)

    assert changed == [str(f.resolve())]
    assert f.read_text(encoding="utf-8") == "original"


def test_rewind_deletes_file_that_did_not_exist_at_snapshot(
    fh: FileHistory, work_dir: Path
) -> None:
    # 「备份缺失 → rewind 时删除」语义：track 时文件尚不存在（无备份可写，
    # 快照仍记录该路径），rewind 到该快照时现存的文件被删除，还原为
    # 「文件不存在」的当时状态。
    future_file = work_dir / "not-yet.txt"
    fh.track_edit(str(future_file))
    fh.make_snapshot(msg_index=0, user_text="s0")

    future_file.write_text("created", encoding="utf-8")
    fh.track_edit(str(future_file))
    fh.make_snapshot(msg_index=1, user_text="s1")

    changed = fh.rewind(0)

    assert str(future_file.resolve()) in changed
    assert not future_file.exists()


def test_rewind_truncates_snapshots_and_resets_versions(
    fh: FileHistory, work_dir: Path
) -> None:
    f = work_dir / "a.txt"
    f.write_text("v0", encoding="utf-8")
    fh.track_edit(str(f))
    fh.make_snapshot(msg_index=0, user_text="s0")
    f.write_text("v1", encoding="utf-8")
    fh.track_edit(str(f))
    fh.make_snapshot(msg_index=1, user_text="s1")

    fh.rewind(0)

    assert len(fh.get_snapshots()) == 1
    assert fh._tracked[str(f.resolve())] == 1


def test_rewind_invalid_index_returns_empty(fh: FileHistory) -> None:
    fh.make_snapshot(msg_index=0, user_text="s0")
    assert fh.rewind(-1) == []
    assert fh.rewind(5) == []


def test_rewind_without_snapshots_returns_empty(fh: FileHistory) -> None:
    assert fh.rewind(0) == []
    assert fh.has_snapshots() is False


def test_rewind_noop_when_content_matches_backup(
    fh: FileHistory, work_dir: Path
) -> None:
    f = work_dir / "a.txt"
    f.write_text("same", encoding="utf-8")
    fh.track_edit(str(f))
    fh.make_snapshot(msg_index=0, user_text="s0")

    changed = fh.rewind(0)

    assert changed == []
    assert f.read_text(encoding="utf-8") == "same"


def test_snapshot_with_deleted_tracked_file_does_not_crash(
    fh: FileHistory, work_dir: Path
) -> None:
    f = work_dir / "gone.txt"
    f.write_text("bye", encoding="utf-8")
    fh.track_edit(str(f))
    f.unlink()

    # 已跟踪文件被外部删除后再次快照：不应抛异常
    fh.make_snapshot(msg_index=0, user_text="s0")
    assert len(fh.get_snapshots()) == 1


def test_session_dir_isolated_per_session(tmp_path: Path) -> None:
    a = FileHistory(str(tmp_path), "sess-a")
    b = FileHistory(str(tmp_path), "sess-b")
    assert a._session_dir != b._session_dir
    assert a._session_dir.exists() and b._session_dir.exists()
