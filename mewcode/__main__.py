
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mewcode.config import ConfigError, load_config
from mewcode.hooks import HookConfigError, HookEngine, load_hooks
from mewcode.permissions import PermissionMode


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def _serve_main(argv: list[str]) -> None:
    """``mewcode serve``：无头服务入口（M1）。"""
    parser = argparse.ArgumentParser(prog="mewcode serve", description="Run the alert-driven service")
    parser.add_argument("--host", default=None, help="bind address (overrides service.host)")
    parser.add_argument("--port", type=int, default=None, help="bind port (overrides service.port)")
    parser.add_argument("--data-dir", default=None, help="state dir (overrides service.data_dir)")
    parser.add_argument("--config", default=None, help="path to config.yaml (overrides discovery)")
    parser.add_argument(
        "--no-recover",
        action="store_true",
        help="do not re-queue unfinished jobs found in the store on startup",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(Path(args.config)) if args.config else load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    service = config.service
    host = args.host or service.host
    port = args.port or service.port
    if args.data_dir:
        service.data_dir = args.data_dir

    # 无 token 的写端点只允许回环监听：告警 webhook 会触发自动改代码，
    # 裸奔在 0.0.0.0 上等于把"无人值守的 shell"挂到公网。
    if not _is_loopback(host) and not service.webhook_token:
        print(
            f"Error: refusing to bind {host} without service.webhook_token configured; "
            "set a token or bind to 127.0.0.1",
            file=sys.stderr,
        )
        sys.exit(2)

    asyncio.run(_serve(service, host, port, recover=not args.no_recover))


async def _serve(service, host: str, port: int, recover: bool = True) -> None:
    from aiohttp import web

    from mewcode.service.api import create_app
    from mewcode.service.execution import ExecutionChain, HeadlessAgentRunner, SandboxTestRunner
    from mewcode.service.jobs import JobStore
    from mewcode.service.notify import build_notifier
    from mewcode.service.publisher import GitHubCIGate, PullRequestPublisher
    from mewcode.service.runtime import ServiceRuntime
    from mewcode.service.sandbox import DockerSandbox
    from mewcode.service.triggers import build_adapters
    from mewcode.service.vcs import GitHubVCS

    try:
        config = load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)

    provider = config.providers[0]
    store = JobStore(Path(service.data_dir) / "jobs.db")
    await store.connect()

    notifier = build_notifier(service.notify, store)
    # 沙箱（M2 W1）：容器不可用时 HeadlessAgentRunner / SandboxTestRunner 自动回退直跑
    sandbox = None
    if service.sandbox.enabled:
        mewcode_src = str(Path(__file__).resolve().parent.parent)
        sandbox = DockerSandbox(
            service.sandbox, mewcode_src=mewcode_src, work_root=str(Path(service.data_dir) / "sandbox")
        )
    runner = HeadlessAgentRunner(
        service, provider, hook_engine=HookEngine(hooks) if hooks else None, sandbox=sandbox
    )
    vcs = GitHubVCS(service.vcs) if service.vcs.provider == "github" else None
    default_repo = next(iter(service.repos), "job")
    chain = ExecutionChain(
        service,
        store,
        runner,
        test_runner=SandboxTestRunner(sandbox, repo_name=default_repo) if sandbox else None,
        publisher=PullRequestPublisher(vcs, store, service) if vcs else None,
        ci_gate=GitHubCIGate(vcs) if vcs else None,
        notifier=notifier,
    )
    runtime = ServiceRuntime(
        service,
        handler=chain,
        store=store,
        worktree_cleanup_cutoff_hours=config.worktree.stale_cutoff_hours,
        notifier=notifier,
    )
    await runtime.start(recover=recover)

    app = create_app(runtime, build_adapters(service))

    http_runner = web.AppRunner(app)
    await http_runner.setup()
    site = web.TCPSite(http_runner, host, port)
    await site.start()
    print(f"mewcode service listening on http://{host}:{port}", flush=True)

    stop = asyncio.Event()
    _install_signal_handlers(stop)
    try:
        await stop.wait()
    finally:
        print("shutting down: draining in-flight jobs...", flush=True)
        await runtime.stop()
        await http_runner.cleanup()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGINT/SIGTERM → 置位停止事件。

    不经 KeyboardInterrupt（那样在 Windows 上会让清理逻辑走取消路径），
    由主线程信号处理器直接唤醒事件循环。
    """
    import signal

    loop = asyncio.get_running_loop()

    def _on_signal(signum, frame):  # pragma: no cover - 由信号触发
        loop.call_soon_threadsafe(stop.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # 非主线程等场景：退化为默认行为
            pass


def main() -> None:
    # 调试日志默认写在仓库内的 .mewcode/debug.log；沙箱容器里 agent 的 cwd 是
    # job 的 worktree，写进去就成了"修复产物"的一部分（真机踩到：PR body 的
    # 改动统计里多出一个 .mewcode/debug.log）。容器用 MEWCODE_LOG_FILE 重定向。
    log_file = Path(os.environ.get("MEWCODE_LOG_FILE") or ".mewcode/debug.log")
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        log_file = Path(os.devnull)  # 只读文件系统等：宁可没有日志也不能崩
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        filename=str(log_file),
        filemode="w",
    )

    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        _serve_main(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(prog="mewcode", description="MewCode AI coding assistant")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in PermissionMode],
        default=None,
        help="Permission mode (overrides config.yaml)",
    )
    parser.add_argument(
        "-p",
        metavar="PROMPT",
        default=None,
        help="Run non-interactively: execute the prompt and print the result to stdout",
    )
    parser.add_argument(
        "--output-format",
        choices=["text", "json"],
        default="text",
        help="Non-interactive output format: 'text' (default) prints the result, "
        "'json' prints a machine-readable summary (result, usage, tool calls)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to config.yaml (overrides discovery); used by the sandbox, which "
        "runs the agent against a generated minimal config instead of ~/.mewcode",
    )
    args = parser.parse_args()

    try:
        config = load_config(Path(args.config)) if args.config else load_config()
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    mode_str = args.mode if args.mode else config.permission_mode
    permission_mode = PermissionMode(mode_str)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        print(f"Hook config error: {e}", file=sys.stderr)
        sys.exit(1)

    hook_engine = HookEngine(hooks) if hooks else None

    if args.p is not None:
        asyncio.run(_run_prompt(config, permission_mode, hook_engine, args.p, args.output_format))
        return

    from mewcode.app import MewCodeApp
    from mewcode.driver import NoAltScreenDriver

    app = MewCodeApp(
        providers=config.providers,
        permission_mode=permission_mode,
        mcp_servers=config.mcp_servers,
        hook_engine=hook_engine,
        enable_fork=config.enable_fork,
        enable_verification_agent=config.enable_verification_agent,
        worktree_config=config.worktree,
        teammate_mode=config.teammate_mode,
        enable_coordinator_mode=config.enable_coordinator_mode,
        driver_class=NoAltScreenDriver,
    )
    app.run()


def _summary_payload(agent, result_text: str, counters: dict, mcp_tools_used: set[str]) -> dict:
    """``-p --output-format json`` 的机器可读摘要（服务层与容器回读依赖它）。

    单独成函数：这是与沙箱执行层之间的**契约**，值得能被直接测。
    """
    return {
        "result": result_text,
        "usage": {
            "inputTokens": agent.total_input_tokens,
            "outputTokens": agent.total_output_tokens,
        },
        "toolCalls": counters["tool_calls"],
        # M2 W3：内部工具链的使用证据（服务层据此写 job 审计与 PR body）
        "mcpCalls": counters["mcp_calls"],
        "mcpTools": sorted(mcp_tools_used),
        "sessionId": agent.session_id,
    }


async def _run_prompt(config, permission_mode, hook_engine, prompt: str, output_format: str = "text") -> None:
    from mewcode.agent import Agent
    from mewcode.agents.loader import AgentLoader
    from mewcode.agents.task_manager import TaskManager
    from mewcode.agents.trace import TraceManager
    from mewcode.client import create_client, resolve_context_window
    from mewcode.config import WorktreeConfig
    from mewcode.conversation import ConversationManager
    from mewcode.mcp.bootstrap import close_mcp, register_mcp_tools
    from mewcode.memory.instructions import load_instructions
    from mewcode.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        RuleEngine,
    )
    from mewcode.teams.manager import TeamManager
    from mewcode.tools import create_default_registry
    from mewcode.tools.agent_tool import AgentTool
    from mewcode.tools.impl.tool_search import ToolSearchTool
    from mewcode.tools.team_create import TeamCreateTool
    from mewcode.tools.team_delete import TeamDeleteTool
    from mewcode.worktree import WorktreeManager

    provider = config.providers[0]
    client = create_client(provider)
    # 第 2 层：尽力从 provider 自动拉取模型的 context window（缓存在 provider 上）。
    # 不会抛异常或阻塞启动；失败则退化到映射表。
    await resolve_context_window(provider)
    work_dir = os.getcwd()
    home = Path.home()

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(work_dir),
        rule_engine=RuleEngine(
            user_rules_path=home / ".mewcode" / "permissions.yaml",
            project_rules_path=Path(work_dir) / ".mewcode" / "permissions.yaml",
            local_rules_path=Path(work_dir) / ".mewcode" / "permissions.local.yaml",
        ),
        mode=permission_mode,
    )

    instructions = load_instructions(work_dir)
    registry = create_default_registry()
    # M2 W3：内部工具链（只读 MCP）。无头模式的语义与 TUI 不同：工具直接可见
    # （defer=False，无人值守不存在"先 ToolSearch"的余地）、只挂只读工具、
    # 连不上只记日志。沙箱容器内跑的就是这条路径（--config 带 mcp_servers）。
    mcp_result = await register_mcp_tools(registry, config.mcp_servers)
    if mcp_result.server_names:
        _mcp_log = logging.getLogger("mewcode.mcp")
        _mcp_log.info("MCP bootstrap: %s", mcp_result.summary())
        for _error in mcp_result.errors:
            _mcp_log.warning("MCP unavailable: %s", _error)
    registry.register(ToolSearchTool(registry, protocol=provider.protocol))

    agent = Agent(
        client=client,
        registry=registry,
        protocol=provider.protocol,
        work_dir=work_dir,
        permission_checker=checker,
        context_window=provider.get_context_window(),
        instructions_content=instructions,
        hook_engine=hook_engine,
    )

    wt_cfg = config.worktree or WorktreeConfig()
    wt_manager = WorktreeManager(
        repo_root=work_dir,
        symlink_directories=wt_cfg.symlink_directories,
    )
    trace_manager = TraceManager()
    task_manager = TaskManager()
    agent_loader = AgentLoader(work_dir, enable_verification=config.enable_verification_agent)
    agent_loader.load_all()
    team_manager = TeamManager(worktree_manager=wt_manager, trace_manager=trace_manager)

    agent_tool = AgentTool(
        agent_loader=agent_loader,
        task_manager=task_manager,
        trace_manager=trace_manager,
        parent_agent=agent,
        enable_fork=config.enable_fork,
        provider_config=provider,
        worktree_manager=wt_manager,
        team_manager=team_manager,
    )
    registry.register(agent_tool)
    registry.register(TeamCreateTool(
        team_manager=team_manager,
        parent_agent=agent,
        teammate_mode="in-process",
        is_interactive=False,
        enable_coordinator_mode=config.enable_coordinator_mode,
    ))
    registry.register(TeamDeleteTool(team_manager=team_manager, parent_agent=agent))

    def drain_notifications() -> list[str]:
        notes: list[str] = []
        for t in task_manager.poll_completed():
            notes.append(
                f"<task-notification>\n<task_id>{t.id}</task_id>\n"
                f"<status>{t.status}</status>\n<result>{t.result}</result>\n"
                f"</task-notification>"
            )
        notes.extend(team_manager.drain_lead_mailbox())
        return notes

    def drain_mailbox_only() -> list[str]:
        return team_manager.drain_lead_mailbox()

    agent.notification_fn = drain_mailbox_only

    conv = ConversationManager()
    counters = {"tool_calls": 0, "mcp_calls": 0}
    mcp_tools_used: set[str] = set()
    json_mode = output_format == "json"

    def _on_event(event: dict) -> None:
        if event.get("type") == "tool_use":
            counters["tool_calls"] += 1
            name = str(event.get("toolName") or "")
            # MCP 工具的注册名是 mcp_<server>_<tool>（见 mcp/tool_wrapper.py）
            if name.startswith("mcp_"):
                counters["mcp_calls"] += 1
                mcp_tools_used.add(name)

    try:
        if json_mode:
            # JSON 模式只输出最后一份摘要，中间过程不落 stdout
            last_result = await agent.run_to_completion(prompt, conv, event_callback=_on_event)
        else:
            last_result = await agent.run_to_completion(prompt, conv)
            print(last_result, flush=True)

        # 门控改为 TaskManager 的公开接口：仅用 AgentTool 后台任务（无 team）
        # 的运行此前会直接 return，asyncio.run 退出时在途任务被整体取消
        for _ in range(90):
            notes = drain_notifications()
            if notes:
                for note in notes:
                    conv.add_system_reminder(note)
                last_result = await agent.run_to_completion(
                    "Teammate notifications received. Process them and continue.", conv,
                    event_callback=_on_event if json_mode else None,
                )
                if not json_mode:
                    print(last_result, flush=True)
                continue
            if not task_manager.has_pending_work():
                break
            await asyncio.sleep(2)

        if json_mode:
            # 机器可读摘要：服务层（含容器内沙箱执行）依赖它回读 agent 结果与用量。
            # **先出结果，再收尾**：收尾（MCP stdio 子进程）万一出问题，也不能把
            # 已经做完的活丢掉——真机踩到过：容器里 agent 干完活了，收尾异常让
            # 进程 exit 1 且没有任何输出，服务只能 escalate。
            print(json.dumps(_summary_payload(agent, last_result, counters, mcp_tools_used), ensure_ascii=False), flush=True)
    finally:
        # 显式收尾 MCP stdio 子进程：不能依赖进程退出兜底（仓库已知坑）
        await close_mcp(mcp_result)


if __name__ == "__main__":
    main()

