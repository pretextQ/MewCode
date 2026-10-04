"""共享测试夹具：隔离的工作目录、家目录与缓存实例。

供权限/安全类测试（阶段 1 起）复用，避免用例触碰真实用户目录或产生跨用例状态。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.cache import FileCache
from mewcode.filehistory import FileHistory


@pytest.fixture
def isolated_home(monkeypatch, tmp_path: Path) -> Path:
    """把 Path.home 指向临时目录，防止测试读写真实 ~/.mewcode。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """隔离的工作目录并切换进程 CWD，测试结束自动还原。"""
    original = Path.cwd()
    import os

    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(original)


@pytest.fixture
def file_cache() -> FileCache:
    return FileCache()


@pytest.fixture
def file_history(tmp_path: Path) -> FileHistory:
    return FileHistory(str(tmp_path), session_id="test-session")
