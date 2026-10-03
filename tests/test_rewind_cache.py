"""F3.6 /rewind 后 FileCache 失效。

read → 修改 → read（缓存 v2）→ rewind → read 必须得到回滚后的 v1。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from mewcode.cache import FileCache
from mewcode.filehistory import FileHistory
from mewcode.tools.read_file import Params as ReadParams
from mewcode.tools.read_file import ReadFile


def _rewind(ctx) -> None:
    from mewcode.commands.handlers.rewind import _handle_rewind

    asyncio.run(_handle_rewind(ctx))


def _make_ctx(agent) -> SimpleNamespace:
    messages: list[str] = []
    ui = SimpleNamespace(add_system_message=messages.append)
    conversation = SimpleNamespace(
        history=[],
        replace_history=lambda h: None,
    )
    return SimpleNamespace(
        agent=agent,
        args="1 3",  # 第 1 个快照、option 3（仅恢复代码）
        ui=ui,
        conversation=conversation,
        session=None,
        session_manager=None,
        memory_manager=None,
        config={},
    )


def _read(read_tool: ReadFile, target: Path) -> str:
    r = asyncio.run(read_tool.execute(ReadParams.model_validate(
        {"file_path": str(target)}
    )))
    assert not r.is_error, r.output
    return r.output


def test_rewind_invalidates_file_cache(tmp_path: Path):
    target = tmp_path / "code.py"
    target.write_text("v1")

    cache = FileCache()
    history = FileHistory(str(tmp_path), session_id="t")
    read_tool = ReadFile(file_cache=cache)
    agent = SimpleNamespace(file_history=history, file_cache=cache)

    # 1) 读 v1 并缓存
    assert "v1" in _read(read_tool, target)

    # 2) 写前快照，然后内容变为 v2 并重新读取（缓存里现在是 v2）
    history.track_edit(str(target))
    history.make_snapshot(1, "user said edit")
    target.write_text("v2")
    cache.invalidate(str(target.resolve()))  # 模拟工具写入后的失效
    assert "v2" in _read(read_tool, target)
    assert cache.get(str(target.resolve())) == "v2"

    # 3) rewind 恢复 v1
    _rewind(_make_ctx(agent))
    assert target.read_text() == "v1", "rewind 应恢复文件内容"

    # 4) 再次读必须拿到 v1：缓存未失效时会返回 v2
    out = _read(read_tool, target)
    assert "v1" in out
    assert "v2" not in out
