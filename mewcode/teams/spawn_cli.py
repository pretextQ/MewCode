"""One argv/parser contract and authenticated, memory-only worker bootstrap."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mewcode.atomic import atomic_write_text


def worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mewcode --teammate")
    for flag in ("team-name", "name", "work-dir", "prompt"):
        parser.add_argument(f"--{flag}", required=True)
    for flag in ("team-config", "agent-id", "agent-type", "model", "bootstrap-token"):
        parser.add_argument(f"--{flag}", default="")
    parser.add_argument("--bootstrap-port", type=int, default=0)
    return parser


def build_worker_argv(
    team_name: str, teammate_name: str, worktree_path: str, prompt: str,
    agent_type: str = "", model: str = "", mailbox_dir: str = "", *,
    team_config: str = "", agent_id: str = "", bootstrap_port: int = 0, bootstrap_token: str = "",
    executable: str | None = None,
) -> list[str]:
    from mewcode.teams.models import resolve_team_dir
    directory = Path(mailbox_dir).parent if mailbox_dir else resolve_team_dir(team_name)
    config = team_config or str(directory / "config.json")
    values = {"team-name": team_name, "name": teammate_name, "work-dir": worktree_path, "prompt": prompt,
              "agent-type": agent_type, "model": model, "team-config": config, "agent-id": agent_id,
              "bootstrap-port": str(bootstrap_port), "bootstrap-token": bootstrap_token}
    return [executable or sys.executable, "-m", "mewcode", "--teammate", *[f"--{k}={v}" for k, v in values.items()]]


@asynccontextmanager
async def bootstrap_server(payload: dict[str, Any]):
    """One authenticated read. Provider credentials never enter argv or files."""
    token = secrets.token_urlsafe(32)
    consumed = False
    handlers: set[asyncio.Task[Any]] = set()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal consumed
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            offered = (await asyncio.wait_for(reader.readline(), 5)).decode().strip()
            if secrets.compare_digest(offered, token) and not consumed:
                consumed = True
                writer.write(json.dumps(payload, ensure_ascii=False).encode())
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], token
    finally:
        server.close()
        await server.wait_closed()
        for handler in list(handlers):
            handler.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


def state_path(config: str, agent_id: str) -> Path:
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", agent_id):
        raise ValueError("Invalid worker agent ID")
    return Path(config).parent / "workers" / f"{agent_id}.json"


def write_state(path: Path, state: str, **extra: Any) -> None:
    atomic_write_text(path, json.dumps({"state": state, "pid": os.getpid(), **extra}))


async def worker_run(args: argparse.Namespace) -> None:
    from mewcode.agent import Agent
    from mewcode.agents.tool_filter import apply_plan_readonly_filter, build_teammate_tools
    from mewcode.client import create_client
    from mewcode.config import ProviderConfig
    from mewcode.conversation import ConversationManager, Message, ThinkingBlock, ToolResultBlock, ToolUseBlock
    from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
    from mewcode.teams.manager import TeamManager
    from mewcode.teams.worker_runtime import run_team_worker
    from mewcode.tools import ToolRegistry, create_default_registry
    from mewcode.tools.impl.tool_search import ToolSearchTool

    state_file: Path | None = None
    client = None
    agent = None
    final_state = "failed"
    try:
        manager = TeamManager()
        team = manager.attach_worker_team(args.team_config)
        member = team.get_member(args.agent_id)
        if (team.name != args.team_name or member is None or member.name != args.name
                or member.agent_type != args.agent_type or member.model != args.model):
            raise ValueError("Unknown teammate identity")
        if Path(member.worktree_path).resolve() != Path(args.work_dir).resolve():
            raise ValueError("Worker worktree does not match team membership")
        state_file = state_path(args.team_config, args.agent_id)
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", args.bootstrap_port), 5)
        try:
            writer.write((args.bootstrap_token + "\n").encode())
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 5)
            if len(raw) > 8_000_000:
                raise ValueError("Worker bootstrap is too large")
            payload = json.loads(raw)
        finally:
            writer.close()
            await writer.wait_closed()
        if (payload["agent_id"] != args.agent_id or payload["team_config"] != args.team_config
                or payload["name"] != args.name or payload["work_dir"] != args.work_dir
                or payload["provider"]["model"] != args.model or payload["agent_type"] != args.agent_type):
            raise ValueError("Worker bootstrap identity mismatch")
        os.chdir(args.work_dir)
        provider = ProviderConfig(**payload["provider"])
        client = create_client(provider)
        base = create_default_registry()
        base.register(ToolSearchTool(base, protocol=provider.protocol))
        registry = build_teammate_tools(base, manager, team.name, args.agent_id, args.name, "in-process")
        permitted = ToolRegistry()
        for tool in registry.list_tools():
            if tool.name in payload["tools"]:
                permitted.register(tool)
        mode = PermissionMode(payload["permission_mode"])
        if mode == PermissionMode.PLAN:
            permitted = apply_plan_readonly_filter(permitted)
        checker = PermissionChecker(
            DangerousCommandDetector(), PathSandbox(args.work_dir),
            RuleEngine(*[Path(p) if p else None for p in payload["rule_paths"]]), mode=mode,
        )
        from mewcode.memory.instructions import agent_instructions
        instructions = payload["instructions"]
        if not payload.get("instructions_loaded", False):
            instructions = agent_instructions(args.work_dir, instructions)
        agent = Agent(client, permitted.bind_work_dir(args.work_dir), provider.protocol, work_dir=args.work_dir,
                      permission_checker=checker, max_iterations=payload["max_iterations"],
                      context_window=provider.get_context_window(), instructions_content=instructions,
                      hook_engine=decode_hooks(payload.get("hooks", [])),
                      enable_supervision=payload.get("enable_supervision", False))
        agent.agent_id, agent.parent_id, agent.trace_id = args.agent_id, payload["parent_id"], payload["trace_id"]
        agent.team_name, agent._team_manager = team.name, manager
        conversation = ConversationManager()
        for entry in payload.get("history", []):
            conversation.history.append(Message(
                entry["role"], entry["content"],
                [ToolUseBlock(**b) for b in entry["tool_uses"]],
                [ToolResultBlock(**b) for b in entry["tool_results"]],
                [ThinkingBlock(**b) for b in entry["thinking_blocks"]],
            ))
        conversation.env_injected = payload.get("env_injected", False)
        conversation.ltm_injected = payload.get("ltm_injected", False)
        identity = dict(agent_id=agent.agent_id, parent_id=agent.parent_id, trace_id=agent.trace_id,
                        model=provider.model, work_dir=agent.work_dir)
        write_state(state_file, "ready", **identity)

        async def run(prompt: str) -> str:
            return await agent.run_to_completion(prompt, conversation)

        def state_changed(state: str) -> None:
            assert state_file is not None
            write_state(state_file, state, **identity)

        await run_team_worker(manager, team.name, args.agent_id, args.name,
                              lambda: run(args.prompt), run, lambda result: None,
                              state_changed)
        final_state = "stopped"
    except BaseException as exc:
        if state_file:
            write_state(state_file, "failed", error=type(exc).__name__)
        raise
    finally:
        if agent:
            await agent.cancel_background_tasks()
            if agent.hook_engine:
                await agent.hook_engine.cancel_background()
        if client:
            await client.close()
        if state_file and final_state == "stopped":
            write_state(state_file, final_state)


def decode_hooks(entries: list[dict[str, Any]]) -> Any:
    from mewcode.hooks import HookEngine
    from mewcode.hooks.conditions import Condition, ConditionGroup
    from mewcode.hooks.models import Action, Hook
    hooks = []
    for entry in entries:
        values = dict(entry)
        values["action"] = Action(**values["action"])
        condition = values.get("condition")
        if condition:
            values["condition"] = ConditionGroup([Condition(**c) for c in condition["conditions"]], condition["logic"])
        hooks.append(Hook(**values))
    return HookEngine(hooks) if hooks else None


def worker_main(argv: list[str]) -> None:
    args = worker_parser().parse_args(argv)
    try:
        asyncio.run(worker_run(args))
    except Exception as exc:
        # SDK/config exceptions can contain credentials; expose only the class.
        print(f"Teammate startup/runtime failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1) from None
