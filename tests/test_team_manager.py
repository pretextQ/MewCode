"""F4.3: TeamManager 的测试——审查报告覆盖盲区。

覆盖团队生命周期：create/get/register/idle/mailbox/completed/delete
（含活跃成员拒绝删除），以及 F3.7③ 的缓存键 slug 一致性。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.teams.manager import TeamError, TeamManager
from mewcode.teams.models import TeammateInfo


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def tm(home: Path) -> TeamManager:
    return TeamManager()


def _member(name: str, agent_id: str = "agent-1", active: bool = True) -> TeammateInfo:
    return TeammateInfo(
        name=name,
        agent_id=agent_id,
        agent_type="worker",
        model="test-model",
        worktree_path="",
        backend_type="inprocess",
        is_active=active,
    )


def test_create_team_registers_slug_and_stores(tm: TeamManager, home: Path) -> None:
    team = tm.create_team("Fix Auth", lead_agent_id="lead-1", is_interactive=False)

    # 名字被 sanitize 为 slug（小写、非法字符转 -）
    assert team.name == "fix-auth"
    assert tm.get_team("Fix Auth") is team
    assert tm.get_team("fix-auth") is team

    team_dir = home / ".mewcode" / "teams" / "fix-auth"
    assert team_dir.exists()
    assert (team_dir / "config.json").exists()
    assert (team_dir / "tasks.json").exists()
    assert (team_dir / "mailbox").exists()


def test_get_team_cache_key_uses_slug(tm: TeamManager) -> None:
    # F3.7③: display name 与 slug 必须命中同一缓存条目
    created = tm.create_team("My Team", lead_agent_id="lead-1", is_interactive=False)
    assert tm.get_team("My Team") is created
    assert tm.get_team("my-team") is created
    assert tm.get_team("MY TEAM") is created


def test_get_team_unknown_returns_none(tm: TeamManager) -> None:
    assert tm.get_team("nope") is None


def test_register_member_and_teammate_lookup(tm: TeamManager) -> None:
    team = tm.create_team("t1", lead_agent_id="lead-1", is_interactive=False)
    member = _member("alice", agent_id="a-1")
    tm.register_member(team.name, member)

    stored = tm.get_team("t1")
    assert stored is not None
    assert [m.name for m in stored.members] == ["alice"]
    assert tm.get_team_for_teammate("a-1") == team.name

    from mewcode.teams.registry import AgentNameRegistry
    assert AgentNameRegistry.instance().resolve("alice") == "a-1"


def test_register_member_unknown_team_raises(tm: TeamManager) -> None:
    with pytest.raises(TeamError, match="not found"):
        tm.register_member("ghost", _member("alice"))


def test_set_member_idle_marks_inactive_and_notifies_lead(tm: TeamManager) -> None:
    team = tm.create_team("t1", lead_agent_id="lead-1", is_interactive=False)
    tm.register_member(team.name, _member("alice", agent_id="a-1"))

    tm.set_member_idle(team.name, "alice")

    stored = tm.get_team("t1")
    assert stored is not None
    assert stored.members[0].is_active is False

    mailbox = tm.get_mailbox(team.name)
    assert mailbox is not None
    msgs = mailbox.consume("lead-1")
    assert len(msgs) == 1
    assert "alice" in msgs[0].content
    assert "idle" in msgs[0].content


def test_drain_lead_mailbox_formats_notifications(tm: TeamManager) -> None:
    team = tm.create_team("t1", lead_agent_id="lead-1", is_interactive=False)
    tm.register_member(team.name, _member("alice", agent_id="a-1"))

    tm.set_member_idle(team.name, "alice")
    notes = tm.drain_lead_mailbox()

    assert len(notes) == 1
    assert '<team-notification team="t1">' in notes[0]
    assert "from=alice" in notes[0]
    # 消费后清空
    assert tm.drain_lead_mailbox() == []


def test_on_teammate_completed_sets_idle(tm: TeamManager) -> None:
    team = tm.create_team("t1", lead_agent_id="lead-1", is_interactive=False)
    tm.register_member(team.name, _member("alice", agent_id="a-1"))

    tm.on_teammate_completed("a-1")

    stored = tm.get_team("t1")
    assert stored is not None
    assert stored.members[0].is_active is False
    # 未知 agent_id 不炸
    tm.on_teammate_completed("nobody")


def test_delete_team_rejects_active_members(tm: TeamManager) -> None:
    team = tm.create_team("t1", lead_agent_id="lead-1", is_interactive=False)
    tm.register_member(team.name, _member("alice", agent_id="a-1", active=True))

    with pytest.raises(TeamError, match="active members"):
        tm.delete_team(team.name)
    assert tm.get_team("t1") is not None


def test_delete_team_with_display_name_removes_caches(
    tm: TeamManager, home: Path
) -> None:
    team = tm.create_team("My Team", lead_agent_id="lead-1", is_interactive=False)
    tm.register_member(team.name, _member("alice", agent_id="a-1"))
    tm.set_member_idle(team.name, "alice")

    tm.delete_team("My Team")

    assert tm.get_team("my-team") is None
    assert tm.get_team("My Team") is None
    assert tm.get_task_store("my-team") is None
    assert tm.get_mailbox("my-team") is None
    assert not (home / ".mewcode" / "teams" / "my-team").exists()
    assert tm.get_team_for_teammate("a-1") is None


def test_delete_unknown_team_raises(tm: TeamManager) -> None:
    with pytest.raises(TeamError, match="not found"):
        tm.delete_team("ghost")


def test_pane_id_roundtrip(tm: TeamManager) -> None:
    assert tm.get_pane_id("a-1") is None
    tm.register_pane_id("a-1", "%3")
    assert tm.get_pane_id("a-1") == "%3"


def test_duplicate_team_name_gets_unique_suffix(tm: TeamManager, home: Path) -> None:
    t1 = tm.create_team("dup", lead_agent_id="lead-1", is_interactive=False)
    t2 = tm.create_team("dup", lead_agent_id="lead-2", is_interactive=False)

    assert t1.name == "dup"
    assert t2.name == "dup-2"
    assert tm.get_team("dup-2") is t2

# =========================================================================
# 补充分支：store/mailbox 访问、pane 清理、未知团队兜底
# =========================================================================

def test_get_task_store_roundtrip(tm: TeamManager) -> None:
    from mewcode.teams.shared_task import SharedTask

    tm.create_team("t1", lead_agent_id="lead-1", is_interactive=False)
    store = tm.get_task_store("t1")
    assert store is not None
    task = store.create(title="do something")
    assert isinstance(task, SharedTask)
    assert tm.get_task_store("t1").get(task.id) is not None


def test_get_mailbox_uses_team_dir(tm: TeamManager, home: Path) -> None:
    tm.create_team("t1", lead_agent_id="lead-1", is_interactive=False)
    mailbox = tm.get_mailbox("t1")
    assert mailbox is not None
    assert mailbox._base_dir == home / ".mewcode" / "teams" / "t1" / "mailbox"


def test_set_member_idle_unknown_team_is_noop(tm: TeamManager) -> None:
    tm.set_member_idle("ghost", "alice")


def test_delete_team_kills_registered_pane(
    tm: TeamManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    team = tm.create_team("t1", lead_agent_id="lead-1", is_interactive=False)
    member = _member("alice", agent_id="a-1", active=False)
    member.backend_type = "tmux"
    tm.register_member(team.name, member)
    tm.register_pane_id("a-1", "%7")

    killed: list[str] = []
    monkeypatch.setattr(
        tm, "_kill_pane", lambda pane_id, backend: killed.append(pane_id)
    )

    tm.delete_team(team.name)

    assert killed == ["%7"]
    assert tm.get_pane_id("a-1") is None
