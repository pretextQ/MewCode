"""Regression coverage for the October 24 review."""
from types import SimpleNamespace

import pytest

from mewcode.agents.parser import AgentDef
from mewcode.agents.tool_filter import resolve_agent_tools
from mewcode.tools import ToolRegistry


def test_duplicate_mcp_configs_are_rejected_before_mutation():
    from mewcode.config import MCPServerConfig
    from mewcode.mcp.manager import MCPManager

    manager = MCPManager()
    with pytest.raises(ValueError, match="duplicate"):
        manager.load_configs([MCPServerConfig(name="same"), MCPServerConfig(name="same")])
    assert manager._configs == {}


def test_legacy_chinese_output_decodes(monkeypatch):
    from mewcode.tools.base import decode_output

    monkeypatch.setattr("mewcode.tools.base.locale.getpreferredencoding", lambda _: "gbk")
    assert decode_output("中文输出".encode("gbk")) == "中文输出"
    assert decode_output("中文输出".encode()) == "中文输出"


def test_rule_text_matching_is_platform_independent():
    from mewcode.permissions.rules import Rule

    rule = Rule("Bash", "Git status", "deny")
    assert rule.matches("Bash", "Git status")
    assert not rule.matches("Bash", "git status")
    assert not Rule("ReadFile", "src/*.py", "allow").matches("ReadFile", r"src\file.py")


def test_permission_rules_cache_reloads_after_change(tmp_path, monkeypatch):
    import mewcode.permissions.rules as module

    path = tmp_path / "rules.yml"
    path.write_text('- {rule: "Bash(npm test)", effect: deny}', encoding="utf-8")
    original = module._load_rules_file
    loads = []

    def load(file):
        loads.append(file)
        return original(file)

    monkeypatch.setattr(module, "_load_rules_file", load)
    engine = module.RuleEngine(local_rules_path=path)
    assert engine.evaluate("Bash", "npm test") == "deny"
    assert engine.evaluate("Bash", "npm test") == "deny"
    assert len(loads) == 1
    path.write_text('- {rule: "Bash(npm test)", effect: allow}', encoding="utf-8")
    assert engine.evaluate("Bash", "npm test") == "allow"


@pytest.mark.asyncio
async def test_large_file_read_rejected_before_loading(tmp_path):
    from mewcode.tools.read_file import MAX_READ_BYTES, Params, ReadFile

    path = tmp_path / "large"
    with path.open("wb") as stream:
        stream.truncate(MAX_READ_BYTES + 1)
    result = await ReadFile().execute(Params(file_path=str(path)))
    assert result.is_error and "limit" in result.output


@pytest.mark.asyncio
async def test_bash_output_limit_kills_runaway_process(monkeypatch):
    import sys

    from mewcode.tools.bash import Bash, Params

    monkeypatch.setattr("mewcode.tools.bash.MAX_CAPTURE_BYTES", 1024)
    command = f'"{sys.executable}" -c "import sys; sys.stdout.write(chr(120)*100000)"'
    result = await Bash().execute(Params(command=command))
    assert result.is_error and "output limit" in result.output


@pytest.mark.asyncio
async def test_sandbox_negative_probe_expires(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from mewcode.config import SandboxConfig
    from mewcode.service.sandbox import DockerSandbox

    monkeypatch.setattr("mewcode.service.sandbox.shutil.which", lambda _: "docker")
    sandbox = DockerSandbox(SandboxConfig(), mewcode_src=str(tmp_path))
    sandbox._run = AsyncMock(side_effect=[(1, "not ready"), (0, "ready")])
    assert not await sandbox.available()
    assert not await sandbox.available()
    sandbox._checked_at -= 61
    assert await sandbox.available()
    assert sandbox._run.await_count == 2


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


def test_file_caches_are_bounded_and_eviction_requires_reread(tmp_path):
    from mewcode.cache import FileCache
    from mewcode.tools.file_state_cache import FileStateCache

    cache = FileCache(max_entries=2, max_chars=5)
    cache.put("one", "abc")
    cache.put("two", "def")
    assert cache.get("one") is None and cache.get("two") == "def"
    cache.put("huge", "x" * 6)
    assert cache.get("huge") is None
    states = FileStateCache(max_entries=1)
    states.record("old", "unused", 1)
    states.record("new", "unused", 2)
    assert not states.check("old")[0]


def test_agent_loader_discovers_new_files_and_caches_parsing(tmp_path, monkeypatch):
    from mewcode.agents.loader import AgentLoader

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "user")
    loader = AgentLoader(str(tmp_path))
    loader.load_all()
    folder = tmp_path / ".mewcode" / "agents"
    folder.mkdir(parents=True)
    path = folder / "new.md"
    path.write_text("---\nname: fresh\ndescription: original\n---\nbody", encoding="utf-8")
    original = loader.get("fresh")
    assert original and original.when_to_use == "original"
    assert loader.get("fresh") is original
    path.write_text("---\nname: fresh\ndescription: updated description\n---\nbody", encoding="utf-8")
    updated = loader.get("fresh")
    assert updated and updated.when_to_use == "updated description"
    assert updated.file_path == path


@pytest.mark.asyncio
async def test_failed_worktree_agent_always_attempts_safe_cleanup(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from mewcode.tools.agent_tool import AgentTool, AgentToolParams

    manager = SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(path=str(tmp_path), head_commit="sha")),
        auto_cleanup=AsyncMock(return_value=SimpleNamespace(kept=True, path=str(tmp_path), branch="saved")),
    )
    parent = SimpleNamespace(max_iterations=2)
    tool = AgentTool(None, None, None, parent, worktree_manager=manager)
    monkeypatch.setattr(tool, "_run_in_worktree", AsyncMock(side_effect=RuntimeError("setup failed")))
    result = await tool._execute_with_worktree(AgentToolParams(prompt="fix", description="test"))
    assert result.is_error and "preserved" in result.output
    manager.auto_cleanup.assert_awaited_once()


def test_plugin_agents_are_loaded_and_discovered(tmp_path, monkeypatch):
    from mewcode.agents.loader import AgentLoader

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "user")
    folder = tmp_path / "plugin"
    folder.mkdir()
    loader = AgentLoader(str(tmp_path))
    loader.register_plugin_source(folder)
    (folder / "agent.md").write_text("---\nname: plugin-agent\ndescription: plugin\n---\nbody", encoding="utf-8")
    agent = loader.get("plugin-agent")
    assert agent and agent.source == "plugin"
