"""Temporary real files for the flat source / typed derived recall contract."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mewcode.memory.auto_memory import MemoryManager
from mewcode.memory.recall import find_relevant_memories, render_reminder, scan_memory_files

FACTS = "### 用户偏好\n- 喜欢简洁代码\n### 纠正反馈\n- 更正：使用互斥锁\n### 项目知识\n- 项目使用 SQLite\n### 参考资料\n- https://example.test/docs"


@pytest.fixture(autouse=True)
def offline_environment(isolated_home, monkeypatch):
    monkeypatch.chdir(isolated_home)


def candidates(manager):
    return scan_memory_files(manager.user_mem_dir, "user") + scan_memory_files(manager.project_mem_dir, "project")


@pytest.mark.asyncio
async def test_real_auto_writer_populates_typed_scan_selector_and_body(tmp_path):
    manager = MemoryManager(str(tmp_path / "project"))
    manager._write_memories(FACTS)
    headers = candidates(manager)
    assert {(h.scope, h.type) for h in headers} == {
        ("user", "user"), ("user", "feedback"), ("project", "project"), ("project", "reference"),
    }
    selector = AsyncMock(return_value=json.dumps({"selected_memories": [h.filename for h in headers]}))
    result = await find_relevant_memories("remember", manager.user_mem_dir, manager.project_mem_dir, None, None, selector)
    assert len(result) == 4 and selector.await_count == 1
    body = render_reminder(result)
    assert "喜欢简洁代码" in body and "项目使用 SQLite" in body
    assert "喜欢简洁代码" in manager.load() and "更正" in manager.load()


def test_existing_flat_memory_backfills_idempotently_without_changing_load(tmp_path):
    initial = MemoryManager(str(tmp_path / "project"))
    initial.user_path.parent.mkdir(parents=True, exist_ok=True)
    initial.project_path.parent.mkdir(parents=True, exist_ok=True)
    initial.user_path.write_bytes("### 用户偏好\r\n- 保留用户正文\r\n".encode())
    initial.project_path.write_text("### 项目知识\n- 保留项目正文\n", encoding="utf-8")
    before = (initial.user_path.read_bytes(), initial.project_path.read_bytes(), initial.load())
    manager = MemoryManager(str(tmp_path / "project"))
    headers = candidates(manager)
    assert len(headers) == 2
    snapshot = {h.file_path: Path(h.file_path).stat().st_mtime_ns for h in headers}
    again = MemoryManager(str(tmp_path / "project"))
    assert {h.file_path: Path(h.file_path).stat().st_mtime_ns for h in candidates(again)} == snapshot
    assert (manager.user_path.read_bytes(), manager.project_path.read_bytes(), manager.load()) == before


def test_manual_files_index_and_edited_derived_file_are_preserved(tmp_path):
    manager = MemoryManager(str(tmp_path / "project"))
    manager.project_mem_dir.mkdir(parents=True)
    index = manager.project_mem_dir / "MEMORY.md"
    index.write_text("# Hand written\n- [manual](manual.md)\n", encoding="utf-8")
    manual = manager.project_mem_dir / "manual.md"
    manual.write_text("---\nname: manual\ntype: reference\ndescription: hand-written\n---\nkeep manual", encoding="utf-8")
    manager._write_memories(FACTS)
    generated = next(h for h in candidates(manager) if h.type == "project")
    path = Path(generated.file_path)
    path.write_text("user replaced this derived file", encoding="utf-8")
    manager.refresh_derived()
    assert path.read_text(encoding="utf-8") == "user replaced this derived file"
    assert manual.read_text(encoding="utf-8").endswith("keep manual")
    assert index.read_text(encoding="utf-8").startswith("# Hand written\n- [manual](manual.md)\n")
    assert len([h for h in candidates(manager) if h.type == "project"]) == 1
    before = {str(p): p.read_bytes() for p in manager.project_mem_dir.iterdir() if p.is_file()}
    manager.refresh_derived()
    assert {str(p): p.read_bytes() for p in manager.project_mem_dir.iterdir() if p.is_file()} == before
    manager.clear()
    assert manual.exists() and path.exists() and manager.load() == ""


def test_path_text_and_forged_manifest_paths_cannot_escape_memory_directory(tmp_path):
    from mewcode.memory.typed_view import MANIFEST
    manager = MemoryManager(str(tmp_path / "project"))
    sentinel = tmp_path / "sentinel.md"
    sentinel.write_text("keep outside", encoding="utf-8")
    manager._write_memories("### 项目知识\n- ../../sentinel.md ' : # 中文\n")
    manifest = manager.project_mem_dir / MANIFEST
    data = json.loads(manifest.read_text(encoding="utf-8"))
    record = next(iter(data["entries"].values()))
    data["entries"]["../../sentinel.md"] = record
    manifest.write_text(json.dumps(data), encoding="utf-8")
    manager.refresh_derived()
    assert sentinel.read_text(encoding="utf-8") == "keep outside"
    assert all(Path(h.file_path).parent == manager.project_mem_dir for h in candidates(manager))


def test_view_failure_retains_flat_facts_and_can_retry(tmp_path, monkeypatch):
    from mewcode.memory import typed_view
    manager = MemoryManager(str(tmp_path / "project"))
    original = typed_view.atomic_write_text
    def fail(path, content):
        raise OSError("offline view write failure")
    monkeypatch.setattr(typed_view, "atomic_write_text", fail)
    manager._write_memories(FACTS)
    assert "喜欢简洁代码" in manager.load() and "项目使用 SQLite" in manager.load()
    assert manager.view_errors and "更新失败" in manager.get_display_text()
    monkeypatch.setattr(typed_view, "atomic_write_text", original)
    manager.refresh_derived()
    assert len(candidates(manager)) == 4 and manager.view_errors == []


@pytest.mark.parametrize("conflict_kind", ["file", "index"])
def test_hand_file_created_during_publish_is_preserved_and_retryable(conflict_kind, tmp_path, monkeypatch):
    from mewcode.memory import typed_view
    manager = MemoryManager(str(tmp_path / "project"))
    original = typed_view._write_changed
    conflict = []
    def concurrent_edit(path, content):
        if path.name == typed_view.MANIFEST and not conflict:
            data = json.loads(content)
            name = next(iter(data["entries"]))
            target = path.parent / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if conflict_kind == "file":
                target.write_text("concurrent hand file", encoding="utf-8")
            (path.parent / "MEMORY.md").write_text("concurrent user index\n", encoding="utf-8")
            conflict.append(target if conflict_kind == "file" else path.parent / "MEMORY.md")
        return original(path, content)
    monkeypatch.setattr(typed_view, "_write_changed", concurrent_edit)
    manager._write_memories(FACTS)
    expected = "concurrent hand file" if conflict_kind == "file" else "concurrent user index\n"
    assert conflict[0].read_text(encoding="utf-8") == expected
    assert (conflict[0].parent / "MEMORY.md").read_text(encoding="utf-8").startswith("concurrent user index\n")
    assert manager.view_errors
    monkeypatch.setattr(typed_view, "_write_changed", original)
    manager.refresh_derived()
    assert not manager.view_errors
    assert conflict[0].read_text(encoding="utf-8").startswith(expected)
    assert len([h for h in candidates(manager) if h.type in {"user", "feedback", "project", "reference"}]) == 4


def test_manual_manifest_and_manual_index_marker_are_not_claimed(tmp_path):
    from mewcode.memory.typed_view import MANIFEST
    manager = MemoryManager(str(tmp_path / "project"))
    directory = manager.project_mem_dir
    directory.mkdir(parents=True)
    manifest = directory / MANIFEST
    manifest.write_text("hand written manifest", encoding="utf-8")
    manager._write_memories(FACTS)
    assert manifest.read_text(encoding="utf-8") == "hand written manifest"
    assert manager.view_errors and "项目使用 SQLite" in manager.load()
    manifest.unlink()  # Explicitly remove this fixture's reserved-name collision.
    manual_block = "<!-- mewcode:auto-memory:v1:0:start -->\nmanual index\n<!-- mewcode:auto-memory:v1:0:end -->\n"
    index = directory / "MEMORY.md"
    index.write_text(manual_block, encoding="utf-8")
    manager.refresh_derived()
    assert index.read_text(encoding="utf-8").startswith(manual_block)
    snapshot = index.read_bytes()
    manager.refresh_derived()
    assert index.read_bytes() == snapshot


@pytest.mark.asyncio
async def test_empty_candidates_and_pending_owned_retirement_do_not_select(tmp_path, monkeypatch):
    manager = MemoryManager(str(tmp_path / "project"))
    selector = AsyncMock(return_value='{"selected_memories": []}')
    assert await find_relevant_memories("q", manager.user_mem_dir, manager.project_mem_dir, None, None, selector) == []
    assert selector.await_count == 0
    manager._write_memories(FACTS)
    original = Path.unlink
    def fail_unlink(path, *args, **kwargs):
        if path.name.startswith("auto-"):
            raise PermissionError("offline retirement failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    manager.clear()
    assert manager.view_errors and manager.load() == ""
    assert await find_relevant_memories("q", manager.user_mem_dir, manager.project_mem_dir, None, None, selector) == []
    assert selector.await_count == 0
    monkeypatch.setattr(Path, "unlink", original)
    manager.refresh_derived()
    assert not candidates(manager) and not manager.view_errors


def test_memory_directory_symlink_cannot_redirect_derived_writes(tmp_path):
    manager = MemoryManager(str(tmp_path / "project"))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    manager.project_mem_dir.parent.mkdir(parents=True)
    try:
        manager.project_mem_dir.symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    manager._write_memories(FACTS)
    assert manager.view_errors and "项目使用 SQLite" in manager.load()
    assert list(foreign.iterdir()) == []


@pytest.mark.asyncio
async def test_failed_flat_write_keeps_extraction_cursor_retryable(tmp_path, monkeypatch):
    from mewcode.client import LLMClient
    from mewcode.conversation import ConversationManager
    from mewcode.memory import auto_memory
    from mewcode.tools.base import StreamEnd, TextDelta
    manager = MemoryManager(str(tmp_path / "project"))
    conv = ConversationManager()
    conv.add_user_message("remember this")
    calls = []
    class Extractor(LLMClient):
        async def stream(self, conversation, system="", tools=None):
            calls.append(True)
            yield TextDelta("### 项目知识\n- 新知识\n")
            yield StreamEnd("end_turn")
    original = auto_memory.atomic_write_text
    def fail(path, content):
        raise OSError("offline flat write failure")
    monkeypatch.setattr(auto_memory, "atomic_write_text", fail)
    with pytest.raises(OSError):
        await manager.extract(Extractor(), conv, "anthropic")
    assert manager._last_extraction_msg_count == 0
    monkeypatch.setattr(auto_memory, "atomic_write_text", original)
    await manager.extract(Extractor(), conv, "anthropic")
    assert manager._last_extraction_msg_count == 1 and len(calls) == 2
    assert "新知识" in manager.load() and len(candidates(manager)) == 1


def test_concurrent_thread_and_process_extractions_do_not_lose_flat_facts(tmp_path, isolated_home):
    root = tmp_path / "project"
    def write(i):
        MemoryManager(str(root))._write_memories(f"### 项目知识\n- thread-{i}\n")
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(write, range(12)))
    code = ("import sys; from pathlib import Path; "
            "Path.home = classmethod(lambda cls: Path(sys.argv[1])); "
            "from mewcode.memory.auto_memory import MemoryManager; "
            "MemoryManager(sys.argv[2])._write_memories('### 用户偏好\\n- process-' + sys.argv[3] + '\\n')")
    repo = Path(__file__).resolve().parents[1]
    processes = [subprocess.Popen([sys.executable, "-c", code, str(isolated_home), str(root), str(i)], cwd=tmp_path,
                                  env={**os.environ, "PYTHONPATH": str(repo), "PYTHONUTF8": "1"},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for i in range(4)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr.decode()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    manager = MemoryManager(str(root))
    assert all(f"thread-{i}" in manager.load() for i in range(12))
    assert all(f"process-{i}" in manager.load() for i in range(4))
    assert len(candidates(manager)) == 16


@pytest.mark.asyncio
async def test_already_injected_auto_entries_skip_selector_but_manual_files_still_recall(tmp_path, monkeypatch):
    from mewcode.app import MewCodeApp
    from mewcode.client import LLMClient
    from mewcode.config import ProviderConfig
    from mewcode.conversation import ConversationManager
    from mewcode.tools.base import StreamEnd, TextDelta
    manager = MemoryManager(str(tmp_path / "project"))
    manager._write_memories(FACTS)
    conv = ConversationManager()
    # The first request will inject the flat source; later requests use its actual snapshot.
    context = SimpleNamespace(memory_manager=manager, conversation=conv,
                              _selected_provider=ProviderConfig("offline", "openai-compat", "http://127.0.0.1:1", "offline"))
    created, closed = [], []
    class Selector(LLMClient):
        async def stream(self, conversation, system="", tools=None):
            yield TextDelta('{"selected_memories": ["manual.md"]}')
            yield StreamEnd("end_turn")
        async def close(self):
            closed.append(True)
    def create(provider):
        created.append(True)
        return Selector()
    monkeypatch.setattr("mewcode.app.create_client", create)
    assert await MewCodeApp._prefetch_relevant_memories(context, "query") == ""
    assert created == []
    conv.inject_long_term_memory("", manager.load())
    assert await MewCodeApp._prefetch_relevant_memories(context, "query") == ""
    assert created == []
    (manager.project_mem_dir / "manual.md").write_text(
        "---\nname: manual\ntype: project\ndescription: manual\n---\nmanual positive control", encoding="utf-8")
    assert "manual positive control" in await MewCodeApp._prefetch_relevant_memories(context, "query")
    assert len(created) == len(closed) == 1
    manager._write_memories("### 项目知识\n- 新增且尚未注入的知识\n")
    selector = AsyncMock(return_value='{"selected_memories": []}')
    await find_relevant_memories("query", manager.user_mem_dir, manager.project_mem_dir, None, None, selector,
                                 injected_keys=manager.injected_keys(conv))
    assert selector.await_count == 1 and "新增且尚未注入的知识" in selector.call_args.args[1]
    assert "喜欢简洁代码" not in selector.call_args.args[1]


@pytest.mark.asyncio
async def test_injected_auto_files_do_not_consume_manual_candidate_budget(tmp_path, monkeypatch):
    from mewcode.conversation import ConversationManager
    monkeypatch.setattr("mewcode.memory.recall.MAX_MEMORY_FILES", 2)
    manager = MemoryManager(str(tmp_path / "project"))
    manager.project_mem_dir.mkdir(parents=True)
    manual = manager.project_mem_dir / "manual.md"
    manual.write_text("---\nname: older manual\ntype: project\ndescription: manual positive control\n---\nkeep recall", encoding="utf-8")
    os.utime(manual, (1, 1))
    manager._write_memories(FACTS)
    conv = ConversationManager()
    conv.inject_long_term_memory("", manager.load())
    selector = AsyncMock(return_value='{"selected_memories": ["manual.md"]}')
    selected = await find_relevant_memories("q", manager.user_mem_dir, manager.project_mem_dir, None, None, selector,
                                           injected_keys=manager.injected_keys(conv))
    assert len(selected) == 1 and selected[0].path == str(manual.resolve())
    assert selector.await_count == 1
