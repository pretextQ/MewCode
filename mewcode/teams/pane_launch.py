"""Parent-side pane startup, readiness and cleanup of this attempt's resources."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from mewcode.teams.models import sanitize_team_name
from mewcode.teams.pane_handle import PaneHandle
from mewcode.teams.spawn_cli import bootstrap_server, state_path
from mewcode.tools.base import ToolResult

STARTUP_TIMEOUT = 10.0


def worktree_name(team: str, name: str) -> str:
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"team-{sanitize_team_name(team)[:16]}/{sanitize_team_name(name)[:16]}-{digest}"


def new_worktree(manager: Any, name: str) -> bool:
    from mewcode.worktree.slug import flatten_slug
    directory = getattr(manager, "worktree_dir", None)
    return bool(directory and not (Path(directory) / flatten_slug(name)).exists())


def bootstrap_payload(tool: Any, team: Any, member: Any, agent: Any, conversation: Any = None) -> dict[str, Any]:
    if tool._provider_config is None:
        raise ValueError("Pane workers require an explicit parent provider")
    provider = replace(tool._provider_config, model=getattr(agent.client, "model", tool._provider_config.model))
    provider_data = asdict(provider)
    provider_data["api_key"] = provider.resolve_api_key()
    checker = agent.permission_checker
    rules = checker.rule_engine if checker else None
    return {
        "agent_id": member.agent_id, "name": member.name, "agent_type": member.agent_type,
        "team_config": team.config_path, "work_dir": member.worktree_path, "provider": provider_data,
        "parent_id": agent.parent_id, "trace_id": agent.trace_id,
        "instructions": agent.instructions_content, "max_iterations": agent.max_iterations,
        "permission_mode": checker.mode.value if checker else "dontAsk",
        "rule_paths": [str(getattr(rules, attr) or "") if rules else ""
                       for attr in ("_user_path", "_project_path", "_local_path")],
        "tools": [t.name for t in agent.registry.list_tools() if agent.registry.is_enabled(t.name)],
        "hooks": [asdict(h) for h in agent.hook_engine.hooks] if agent.hook_engine else [],
        "enable_supervision": agent.supervision.enabled,
        "history": [asdict(m) for m in conversation.history] if conversation else [],
        "env_injected": conversation.env_injected if conversation else False,
        "ltm_injected": conversation.ltm_injected if conversation else False,
    }


async def launch_pane_worker(tool: Any, p: Any, team: Any, member: Any, backend: Any, wt: Any,
                             agent: Any, conversation: Any, owns_worktree: bool,
                             previous_registration: str | None = None) -> ToolResult:
    manager = tool._team_manager
    pane = None
    path = state_path(team.config_path, member.agent_id)
    try:
        payload = bootstrap_payload(tool, team, member, agent, conversation)
        member.model = payload["provider"]["model"]
        team.save()
        async with bootstrap_server(payload) as (port, token):
            options = dict(team_name=team.name, teammate_name=member.name, worktree_path=wt.path,
                           prompt="" if conversation else p.prompt, agent_type=member.agent_type, model=member.model,
                           team_config=team.config_path, agent_id=member.agent_id,
                           bootstrap_port=port, bootstrap_token=token)
            owner_pid = ""
            if backend.value == "tmux":
                from mewcode.teams.spawn_tmux import spawn_tmux_teammate
                info = spawn_tmux_teammate(**options)
                pane_id = info.pane_id
                owner_pid = info.owner_pid
            else:
                from mewcode.teams.spawn_iterm2 import spawn_iterm2_teammate
                iterm_info = spawn_iterm2_teammate(**options)
                pane_id = iterm_info.session_id
            pane = PaneHandle(pane_id, backend.value, path, owner_pid)
            manager.register_pane_handle(member.agent_id, pane)
            deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT
            while pane.status not in ("ready", "running", "listening"):
                if pane.status in ("failed", "stopped") or not pane.exists(live_only=True):
                    raise RuntimeError("Worker exited before readiness")
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("Worker readiness handshake timed out")
                await asyncio.sleep(0.05)
        return ToolResult(output=(f"Teammate '{member.name}' ready in team '{team.name}'.\n"
                                  f"Agent ID: {member.agent_id}\nBackend: {backend.value} (pane)\nWorktree: {wt.path}"))
    except BaseException as exc:
        reaped = True
        if pane:
            mailbox = manager.get_mailbox(team.name)
            reaped = bool(mailbox and await pane.stop(mailbox, member.agent_id, team.lead_agent_id, timeout=0.2))
        if reaped:
            team.remove_member(member.agent_id)
            team.save()
            from mewcode.teams.registry import AgentNameRegistry
            names = AgentNameRegistry.instance()
            if names.resolve(member.name) == member.agent_id:
                names.unregister(member.name)
                if previous_registration:
                    names.register(member.name, previous_registration)
            manager._teammate_team_map.pop(member.agent_id, None)
            manager._pane_ids.pop(member.agent_id, None)
            tool._trace_manager.remove(member.agent_id)
            path.unlink(missing_ok=True)
            path.with_suffix(".pane").unlink(missing_ok=True)
            mailbox = manager.get_mailbox(team.name)
            if mailbox:
                mailbox.cleanup(member.agent_id)
            if owns_worktree:
                await tool._worktree_manager.auto_cleanup(wt.name, wt.head_commit)
        if isinstance(exc, asyncio.CancelledError):
            raise
        detail = str(exc) if isinstance(exc, (RuntimeError, TimeoutError, ValueError)) else type(exc).__name__
        return ToolResult(output=f"Pane worker startup failed: {detail}. Resources reaped: {reaped}.", is_error=True)
    finally:
        if agent.client is not tool._parent_agent.client:
            await agent.client.close()
