from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mewcode.teams.backend_detect import detect_backend
from mewcode.teams.mailbox import Mailbox, create_message
from mewcode.teams.models import (
    AgentTeam,
    BackendType,
    TeammateInfo,
    resolve_team_dir,
    sanitize_team_name,
    unique_team_name,
)
from mewcode.teams.progress import TeammateProgress
from mewcode.teams.registry import AgentNameRegistry
from mewcode.teams.shared_task import SharedTaskStore
from mewcode.teams.spawn_inprocess import InProcessTeammateHandle

if TYPE_CHECKING:
    from mewcode.agents.task_manager import TaskManager

log = logging.getLogger(__name__)


class TeamError(Exception):
    pass


class TeamManager:
    def __init__(self, worktree_manager: Any = None, trace_manager: Any = None) -> None:
        self._teams: dict[str, AgentTeam] = {}
        self._task_stores: dict[str, SharedTaskStore] = {}
        self._mailboxes: dict[str, Mailbox] = {}
        self._inprocess_handles: dict[str, InProcessTeammateHandle] = {}
        self._pane_ids: dict[str, str] = {}  # agent_id -> pane_id (tmux/iterm2)
        self._task_handles: dict[str, tuple[TaskManager, str]] = {}
        self._worker_process = False
        self._detected_backend: BackendType | None = None
        self._worktree_manager = worktree_manager
        self._trace_manager = trace_manager
        self._teammate_team_map: dict[str, str] = {}  # agent_id -> team_name

    def detect_backend(
        self,
        teammate_mode: str = "",
        is_interactive: bool = True,
    ) -> BackendType:
        if self._detected_backend is None:
            self._detected_backend = detect_backend(teammate_mode, is_interactive)
        return self._detected_backend


    def create_team(
        self,
        name: str,
        lead_agent_id: str,
        description: str = "",
        teammate_mode: str = "",
        is_interactive: bool = True,
    ) -> AgentTeam:
        backend = self.detect_backend(teammate_mode, is_interactive)
        slug = unique_team_name(name)
        team_dir = resolve_team_dir(slug)
        team_dir.mkdir(parents=True, exist_ok=True)

        config_path = str(team_dir / "config.json")
        team = AgentTeam(
            name=slug,
            lead_agent_id=lead_agent_id,
            config_path=config_path,
            description=description,
        )
        team.save()

        task_store = SharedTaskStore(team_dir / "tasks.json")
        task_store.init_empty()

        mailbox_dir = team_dir / "mailbox"
        mailbox_dir.mkdir(parents=True, exist_ok=True)
        mailbox = Mailbox(mailbox_dir)

        self._teams[slug] = team
        self._task_stores[slug] = task_store
        self._mailboxes[slug] = mailbox

        log.info("Created team '%s' at %s (backend=%s)", slug, team_dir, backend.value)
        return team


    def get_team(self, name: str) -> AgentTeam | None:
        # 缓存键统一为 sanitize 后的 slug（create_team 写入时用的键）；
        # 旧实现直接用原始名查询，display name 永远 miss
        slug = sanitize_team_name(name)
        if slug in self._teams:
            team = self._teams[slug]
            if self._worker_process:
                team = AgentTeam.load(team.config_path)
                self._teams[slug] = team
            self._refresh_pane_activity(team)
            return team
        if name in self._teams:
            return self._teams[name]
        team_dir = resolve_team_dir(name)
        config_path = team_dir / "config.json"
        if config_path.exists():
            team = AgentTeam.load(str(config_path))
            self._teams[slug] = team
            return team
        return None

    def attach_worker_team(self, config_path: str) -> AgentTeam:
        team = AgentTeam.load(config_path)
        self._worker_process = True
        self._teams[team.name] = team
        self._mailboxes[team.name] = Mailbox(Path(config_path).parent / "mailbox")
        self._task_stores[team.name] = SharedTaskStore(Path(config_path).parent / "tasks.json")
        return team

    def update_worker_activity(self, team: AgentTeam, agent_id: str, active: bool) -> None:
        team.set_member_active(agent_id, active)
        if not self._worker_process:
            team.save()

    def _refresh_pane_activity(self, team: AgentTeam) -> None:
        for member in team.members:
            handle = self.get_pane_handle(member.agent_id, team)
            if handle:
                member.is_active = handle.status in ("starting", "ready", "running") and handle.exists(live_only=True)

    def get_task_store(self, team_name: str) -> SharedTaskStore | None:
        slug = sanitize_team_name(team_name)
        if slug in self._task_stores:
            return self._task_stores[slug]
        if team_name in self._task_stores:
            return self._task_stores[team_name]
        team_dir = resolve_team_dir(team_name)
        tasks_path = team_dir / "tasks.json"
        if tasks_path.exists():
            store = SharedTaskStore(tasks_path)
            self._task_stores[slug] = store
            return store
        return None

    def get_mailbox(self, team_name: str) -> Mailbox | None:
        if team_name in self._mailboxes:
            return self._mailboxes[team_name]
        team_dir = resolve_team_dir(team_name)
        mailbox_dir = team_dir / "mailbox"
        if mailbox_dir.exists():
            mailbox = Mailbox(mailbox_dir)
            self._mailboxes[team_name] = mailbox
            return mailbox
        return None

    def register_member(
        self,
        team_name: str,
        member: TeammateInfo,
    ) -> None:
        team = self.get_team(team_name)
        if team is None:
            raise TeamError(f"Team '{team_name}' not found")
        team.add_member(member)
        team.save()

        AgentNameRegistry.instance().register(member.name, member.agent_id)
        self._teammate_team_map[member.agent_id] = team_name
        log.info("Registered member '%s' (agent=%s) in team '%s'", member.name, member.agent_id, team_name)

    def set_member_idle(self, team_name: str, member_name: str) -> None:
        team = self.get_team(team_name)
        if team is None:
            return
        team.set_member_active(member_name, False)
        team.save()

        mailbox = self.get_mailbox(team_name)
        if mailbox:
            msg = create_message(
                from_agent=member_name,
                to_agent=team.lead_agent_id,
                content=f"Teammate '{member_name}' is now idle (run_to_completion finished).",
                summary=f"{member_name} idle",
                message_type="text",
            )
            mailbox.write(team.lead_agent_id, msg)

    def register_inprocess_handle(self, agent_id: str, handle: InProcessTeammateHandle) -> None:
        self._inprocess_handles[agent_id] = handle

    def register_pane_id(self, agent_id: str, pane_id: str) -> None:
        self._pane_ids[agent_id] = pane_id

    def register_pane_handle(self, agent_id: str, handle: Any) -> None:
        import json

        from mewcode.atomic import atomic_write_text
        self.register_pane_id(agent_id, handle.pane_id)
        atomic_write_text(handle.state_file.with_suffix(".pane"),
                          json.dumps({"pane_id": handle.pane_id, "backend": handle.backend,
                                      "owner_pid": handle.owner_pid}))

    def get_pane_handle(self, agent_id: str, team: AgentTeam | None = None) -> Any:
        import json

        from mewcode.teams.pane_handle import PaneHandle
        from mewcode.teams.spawn_cli import state_path
        if team is None:
            team = next((t for t in self._teams.values() if t.get_member(agent_id)), None)
        if team is None:
            return None
        path = state_path(team.config_path, agent_id)
        try:
            data = json.loads(path.with_suffix(".pane").read_text(encoding="utf-8"))
            return PaneHandle(data["pane_id"], data["backend"], path, data.get("owner_pid", ""))
        except (OSError, ValueError, KeyError):
            return None

    def register_task_handle(self, agent_id: str, manager: TaskManager, task_id: str) -> None:
        self._task_handles[agent_id] = (manager, task_id)

    def member_is_online(self, agent_id: str) -> bool:
        if agent_id in self._task_handles:
            manager, task_id = self._task_handles[agent_id]
            return manager.is_live(task_id)
        handle = self._inprocess_handles.get(agent_id)
        if handle is not None:
            return not handle.done
        pane = self.get_pane_handle(agent_id)
        return pane is not None and pane.online

    async def stop_member(self, team_name: str, agent_id: str, *, timeout: float = 5,
                          terminal_status: str = "stopped") -> bool:
        team = self.get_team(team_name)
        member = team.get_member(agent_id) if team else None
        if team is None or member is None:
            raise TeamError("Unknown team member")
        if agent_id in self._task_handles:
            manager, task_id = self._task_handles[agent_id]
            if not await manager.stop(task_id, timeout, terminal_status=terminal_status):
                return False
        handle = self._inprocess_handles.get(agent_id)
        if handle and not handle.done:
            import asyncio
            if not handle.task.cancelling():
                handle.cancel()
            _, pending = await asyncio.wait([handle.task], timeout=timeout)
            if pending:
                return False
        pane = self.get_pane_handle(agent_id, team)
        mailbox = self.get_mailbox(team_name)
        if pane:
            if not mailbox or not await pane.stop(mailbox, agent_id, team.lead_agent_id, timeout=min(timeout, 3)):
                return False
            import json

            from mewcode.atomic import atomic_write_text
            try:
                data = json.loads(pane.state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data.update(state="stopped", stopped_by=team.lead_agent_id)
            atomic_write_text(pane.state_file, json.dumps(data))
        member.is_active = False
        team.save()
        return True

    async def shutdown_team(self, team_name: str) -> None:
        team = self.get_team(team_name)
        if team is None:
            raise TeamError(f"Team '{team_name}' not found")
        for member in team.members:
            if not await self.stop_member(team_name, member.agent_id, terminal_status="cancelled"):
                raise TeamError(f"Worker '{member.name}' did not stop")

    async def shutdown_all(self) -> None:
        for name in list(self._teams):
            await self.shutdown_team(name)


    def get_pane_id(self, agent_id: str) -> str | None:
        return self._pane_ids.get(agent_id)

    def delete_team(self, team_name: str) -> None:
        team = self.get_team(team_name)
        if team is None:
            raise TeamError(f"Team '{team_name}' not found")

        active = [m for m in team.members if m.is_active is not False]
        if active:
            names = ", ".join(m.name for m in active)
            raise TeamError(f"Cannot delete team: active members: {names}")

        if any(self.member_is_online(m.agent_id) for m in team.members):
            raise TeamError("Await shutdown_team before deleting listening workers")

        for member in list(team.members):
            AgentNameRegistry.instance().unregister(member.name)
            self._teammate_team_map.pop(member.agent_id, None)
            self._task_handles.pop(member.agent_id, None)

            handle = self._inprocess_handles.pop(member.agent_id, None)
            if handle and not handle.done:
                handle.cancel()

            pane_id = self._pane_ids.pop(member.agent_id, None)
            if pane_id:
                self._kill_pane(pane_id, member.backend_type)

            if member.worktree_path:
                self._cleanup_worktree(member.worktree_path)

            if self._trace_manager:
                self._trace_manager.remove(member.agent_id)

        mailbox = self.get_mailbox(team_name)
        if mailbox:
            mailbox.cleanup_all()

        team_dir = resolve_team_dir(team_name)
        self._remove_dir(team_dir)

        slug = sanitize_team_name(team_name)
        self._teams.pop(team_name, None)
        self._teams.pop(slug, None)
        self._task_stores.pop(team_name, None)
        self._task_stores.pop(slug, None)
        self._mailboxes.pop(team_name, None)
        self._mailboxes.pop(slug, None)

        log.info("Deleted team '%s'", team_name)

    def get_team_for_teammate(self, agent_id: str) -> str | None:
        if agent_id in self._teammate_team_map:
            return self._teammate_team_map[agent_id]
        for name, team in self._teams.items():
            for m in team.members:
                if m.agent_id == agent_id:
                    return name
        return None


    def drain_lead_mailbox(self) -> list[str]:
        notes: list[str] = []
        for team_name in list(self._teams.keys()):
            team = self.get_team(team_name)
            if team is None:
                continue
            mailbox = self.get_mailbox(team_name)
            if mailbox is None:
                continue
            msgs = mailbox.consume(team.lead_agent_id)
            if not msgs:
                continue
            parts = [f'<team-notification team="{team_name}">']
            for m in msgs:
                parts.append(f"from={m.from_agent}: {m.content}")
            parts.append("</team-notification>")
            notes.append("\n".join(parts))
        return notes

    def get_all_teammate_progress(self) -> list[TeammateProgress]:
        """Collect progress objects attached to every registered teammate."""
        results: list[TeammateProgress] = []
        for team in self._teams.values():
            for member in team.members:
                if hasattr(member, "progress") and member.progress is not None:
                    results.append(member.progress)
        return results

    def on_teammate_completed(self, agent_id: str) -> None:
        team_name = self.get_team_for_teammate(agent_id)
        if team_name is None:
            return
        team = self.get_team(team_name)
        if team is None:
            return
        member = next((m for m in team.members if m.agent_id == agent_id), None)
        if member:
            self.set_member_idle(team_name, member.name)


    def _kill_pane(self, pane_id: str, backend_type: str) -> None:
        try:
            if backend_type == BackendType.TMUX.value:
                from mewcode.teams.spawn_tmux import kill_pane
                kill_pane(pane_id)
            elif backend_type == BackendType.ITERM2.value:
                from mewcode.teams.spawn_iterm2 import kill_pane as close_session
                close_session(pane_id)
        except Exception as e:
            log.warning("Failed to kill pane %s: %s", pane_id, e)

    def _cleanup_worktree(self, worktree_path: str) -> None:
        import subprocess

        from mewcode.worktree.changes import has_worktree_changes
        try:
            # Team deletion is not permission to discard a teammate's edits or commits.
            base = next((wt.head_commit for wt in self._worktree_manager.active.values()
                         if wt.path == worktree_path), "") if self._worktree_manager else ""
            if not base or has_worktree_changes(worktree_path, base):
                log.info("Preserving worktree with changes or unknown base: %s", worktree_path)
                return
            result = subprocess.run(["git", "worktree", "remove", worktree_path],
                                    capture_output=True, timeout=10)
            if result.returncode:
                log.warning("Preserving worktree after git removal failed: %s", worktree_path)
        except Exception as e:
            log.warning("Preserving worktree after cleanup error for %s: %s", worktree_path, type(e).__name__)

    def _remove_dir(self, path: Path) -> None:
        import shutil
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            log.warning("Failed to remove directory %s: %s", path, e)
