"""Regression coverage for the October 24 review."""
from types import SimpleNamespace

import pytest

from mewcode.agents.parser import AgentDef
from mewcode.agents.tool_filter import resolve_agent_tools
from mewcode.tools import ToolRegistry


def test_failed_atomic_replace_keeps_previous_file(tmp_path, monkeypatch):
    from mewcode.atomic import atomic_write_text

    path = tmp_path / "meta"
    path.write_text("previous", encoding="utf-8")

    def fail(*args):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("mewcode.atomic.os.replace", fail)
    with pytest.raises(OSError):
        atomic_write_text(path, "new")
    assert path.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [path]


def test_resume_recovers_corrupt_metadata(tmp_path):
    from mewcode.conversation import Message
    from mewcode.memory.session import SessionManager

    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session.append(Message(role="user", content="preserved transcript"))
    session.close()
    (manager._sessions_dir / f"{session.session_id}.meta").write_text("{", encoding="utf-8")
    resumed = manager.resume(session.session_id)
    assert resumed is not None
    try:
        assert resumed.messages[0].content == "preserved transcript"
    finally:
        resumed.session.close()


def test_memory_extraction_cannot_omit_existing_sections(tmp_path, monkeypatch):
    from mewcode.memory.auto_memory import MemoryManager

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "user")
    manager = MemoryManager(str(tmp_path / "project"))
    manager._write_memories("### 用户偏好\n- old preference\n### 纠正反馈\n- old correction")
    manager._write_memories("### 用户偏好\n- new preference\n- old preference")
    content = manager.user_path.read_text(encoding="utf-8")
    assert "old correction" in content and "new preference" in content
    assert content.count("old preference") == 1


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("name", ["mcp_files_delete", "mcp__files__delete"])
def test_mcp_agent_restrictions(name, background):
    registry = ToolRegistry()
    registry.register(SimpleNamespace(name=name))
    definition = AgentDef("restricted", "test", disallowed_tools=["mcp*files*"])
    assert resolve_agent_tools(registry, definition, background).get(name) is None
    definition.disallowed_tools = []
    definition.tools = ["ReadFile"]
    assert resolve_agent_tools(registry, definition, background).get(name) is None


def test_compose_limits_and_alias_rejection(tmp_path):
    import yaml

    from mewcode.service.compose import safe_compose_snapshot

    source, target = tmp_path / "compose.yml", tmp_path / "frozen.yml"
    source.write_text("services:\n  db:\n    image: redis\n    pids_limit: -1\n", encoding="utf-8")
    safe_compose_snapshot(source, target)
    service = yaml.safe_load(target.read_text())["services"]["db"]
    assert service["pids_limit"] == 256
    assert service["mem_limit"] == "512m"
    assert service["cpus"] == 1.0
    source.write_text("services: &a {db: *a}", encoding="utf-8")
    with pytest.raises(ValueError, match="aliases"):
        safe_compose_snapshot(source, target)
    source.write_text("[" * 33 + "]" * 33, encoding="utf-8")
    with pytest.raises(ValueError, match="structural"):
        safe_compose_snapshot(source, target)
