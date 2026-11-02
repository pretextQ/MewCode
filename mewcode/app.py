from __future__ import annotations

import asyncio
import logging
import os
import re
import time as _time
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Markdown, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import Worker

from mewcode.agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    PermissionRequest,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from mewcode.agents.loader import AgentLoader
from mewcode.agents.notification import inject_task_notifications
from mewcode.agents.task_manager import TaskManager
from mewcode.agents.trace import TraceManager
from mewcode.cache import FileCache
from mewcode.client import (
    AuthenticationError,
    LLMClient,
    create_client,
    resolve_context_window,
)
from mewcode.commands import (
    CommandContext,
    CommandRegistry,
    complete,
    parse_command,
)
from mewcode.commands.completion import CompletionPopup
from mewcode.commands.handlers import register_all_commands
from mewcode.commands.handlers.skill_register import register_skill_commands
from mewcode.commands.handlers.tasks import create_tasks_command
from mewcode.commands.handlers.worktree import create_worktree_command
from mewcode.config import MCPServerConfig, ProviderConfig
from mewcode.conversation import ConversationManager, Message
from mewcode.hooks import HookContext, HookEngine
from mewcode.mcp import MCPManager
from mewcode.memory import (
    MemoryManager,
    Session,
    SessionManager,
    find_relevant_memories,
    generate_session_summary,
    load_instructions,
    make_compact_boundary,
    render_reminder,
)
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.skills.executor import SkillExecutor
from mewcode.skills.loader import SkillLoader
from mewcode.teammate_tree import TeammateTree
from mewcode.tools import ToolRegistry, create_default_registry
from mewcode.tools.agent_tool import AgentTool
from mewcode.tools.ask_user import AskUserEvent, AskUserTool
from mewcode.tools.impl.tool_search import ToolSearchTool
from mewcode.tools.load_skill import LoadSkill
from mewcode.tui.chrome import MEWCODE_THEME, brand_title, workspace_title
from mewcode.tui.details import DetailsPanel, ExecutionHistory
from mewcode.tui.git import branch_name
from mewcode.tui.input import ChatInput as ChatInput
from mewcode.tui.messages import ChatScroll, assistant_row, user_row
from mewcode.tui.tools import (
    SubAgentBlock as SubAgentBlock,
)
from mewcode.tui.tools import (
    ToolCallBlock as ToolCallBlock,
)
from mewcode.tui.tools import (
    ToolGroupSummary as ToolGroupSummary,
)
from mewcode.tui.tools import (
    _is_subagent_tool,
)
from mewcode.worktree.cleanup import start_stale_cleanup_task
from mewcode.worktree.manager import WorktreeManager

if TYPE_CHECKING:
    from mewcode.askuser_dialog import InlineAskUserWidget
    from mewcode.permission_dialog import InlinePermissionWidget
    from mewcode.plan_dialog import InlinePlanWidget

logger = logging.getLogger(__name__)

MAX_AT_REF_BYTES = 10240

_AT_REF_RE = re.compile(r"@([\w./_\-]+(?:\.[\w]+)*)")

_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".mewcode", "build", ".gradle"}


def scan_files_for_at(prefix: str, work_dir: str, limit: int = 10) -> list[str]:
    matches: list[str] = []
    base = os.path.join(work_dir, os.path.dirname(prefix)) if "/" in prefix else work_dir
    name_prefix = os.path.basename(prefix).lower()
    if not os.path.isdir(base):
        return matches
    try:
        for entry in sorted(os.listdir(base)):
            if entry in _SKIP_DIRS or entry.startswith("."):
                continue
            if entry.lower().startswith(name_prefix):
                rel = os.path.join(os.path.dirname(prefix), entry) if "/" in prefix else entry
                if os.path.isdir(os.path.join(base, entry)):
                    rel += "/"
                matches.append(rel)
                if len(matches) >= limit:
                    break
    except OSError:
        pass
    return matches


def expand_at_refs(text: str, work_dir: str) -> str:
    def _replace(m: re.Match) -> str:
        rel_path = m.group(1)
        full_path = os.path.join(work_dir, rel_path)
        if not os.path.isfile(full_path):
            return m.group(0)
        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                content = f.read(MAX_AT_REF_BYTES)
            return f"[File: {rel_path}]\n```\n{content}\n```"
        except Exception:
            return m.group(0)
    return _AT_REF_RE.sub(_replace, text)




COLLAPSIBLE_TOOLS = {"ReadFile", "Glob", "Grep", "ToolSearch"}

# 流式进行中禁止的状态变更命令（会替换 conversation/session 或压缩历史）
_STATEFUL_COMMANDS = {"clear", "session", "compact"}




_MODE_CYCLE = [
    PermissionMode.DEFAULT,
    PermissionMode.ACCEPT_EDITS,
    PermissionMode.PLAN,
    PermissionMode.BYPASS,
]

_MODE_COLORS = {
    PermissionMode.DEFAULT: "dim",
    PermissionMode.ACCEPT_EDITS: "green",
    PermissionMode.PLAN: "yellow",
    PermissionMode.BYPASS: "red",
}

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"




class MewCodeApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "MewCode"
    INLINE_PADDING = 0
    BINDINGS = [
        Binding("ctrl+c", "handle_ctrl_c", "Quit", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("shift+tab", "cycle_mode", "Cycle mode", priority=True),
        Binding("f2", "details", "开发详情", priority=True),
        Binding("ctrl+end", "latest", "返回最新", priority=True),
        Binding("ctrl+o", "toggle_tool_blocks", "Toggle tools", priority=True),
    ]


    def __init__(
        self,
        providers: list[ProviderConfig],
        permission_mode: PermissionMode = PermissionMode.DEFAULT,
        mcp_servers: list[MCPServerConfig] | None = None,
        hook_engine: HookEngine | None = None,
        enable_fork: bool = False,
        enable_verification_agent: bool = False,
        worktree_config: Any = None,
        teammate_mode: str = "",
        enable_coordinator_mode: bool = False,
        driver_class: type | None = None,
        enable_supervision: bool = False,
    ) -> None:
        super().__init__(driver_class=driver_class)
        self.register_theme(MEWCODE_THEME)
        self.theme = "mewcode"
        self.providers = providers
        self._initial_permission_mode = permission_mode
        self._mcp_server_configs = mcp_servers or []
        self.hook_engine = hook_engine
        self._enable_supervision = enable_supervision
        self._enable_fork = enable_fork
        self._enable_verification_agent = enable_verification_agent
        self._worktree_config = worktree_config
        self._teammate_mode = teammate_mode
        self._enable_coordinator_mode = enable_coordinator_mode
        self.file_cache = FileCache()
        self.client: LLMClient | None = None
        self.conversation = ConversationManager()
        self.registry: ToolRegistry = create_default_registry(file_cache=self.file_cache)
        self.agent: Agent | None = None
        self.mcp_manager: MCPManager | None = None
        self._mcp_init_task: asyncio.Task[None] | None = None
        self._selected_provider: ProviderConfig | None = None
        self._streaming = False
        self._cancel_requested = False
        self._thinking_start: float = 0.0
        self._spinner_idx: int = 0
        self._spinner_timer: Timer | None = None
        self._spinner_label: Static | None = None
        self._pending_askuser_event: AskUserEvent | None = None
        self._pending_perm_request: PermissionRequest | None = None
        self._mcp_server_info: str = ""
        self._agent_task: asyncio.Task[None] | None = None
        self._subagent_start_time: float | None = None
        self.session_manager: SessionManager | None = None
        self.session: Session | None = None
        self.memory_manager: MemoryManager | None = None
        self._instructions_content: str = ""
        self.command_registry = CommandRegistry()
        register_all_commands(self.command_registry)
        self.skill_loader: SkillLoader | None = None
        self.skill_executor: SkillExecutor | None = None
        self._load_skill_tool: LoadSkill | None = None
        self.agent_loader: AgentLoader | None = None
        self.task_manager: TaskManager = TaskManager()
        self.trace_manager: TraceManager = TraceManager()
        self._notification_check_task: asyncio.Task[None] | None = None
        self.worktree_manager: WorktreeManager | None = None
        self._stale_cleanup_task: asyncio.Task[None] | None = None
        # fire-and-forget 后台任务的强引用（事件循环只持弱引用）。
        self._bg_tasks: set[asyncio.Task] = set()
        self._mcp_instructions: str = ""
        self._mcp_instructions_ok: bool = False
        self._mcp_connecting: bool = False
        self._teammate_tree: TeammateTree | None = None
        self._teammate_timer: Timer | None = None
        self._execution_history = ExecutionHistory()
        self._details_open = False
        self._branch_worker: Worker[None] | None = None
        self._branch = "未知"
        self._header_work_dir = ""

    @staticmethod
    def _make_banner(model: str = "", work_dir: str = "") -> RichText:
        return brand_title()

    def compose(self) -> ComposeResult:
        with Vertical(id="app-header"):
            yield Static(self._make_banner(), id="title-bar")
            with Horizontal(id="workspace-bar"):
                yield Static("选择模型后显示工作区", id="workspace-label", markup=False)
                yield Static("—", id="model-label", markup=False)
        if len(self.providers) > 1:
            with Vertical(id="provider-select"):
                yield Static("选择模型入口", id="select-label")
                yield OptionList(
                    *[Option(RichText(f"{p.name}  ·  {p.model}"), id=p.name) for p in self.providers],
                    id="provider-list",
                )
        with Horizontal(id="conversation-layout"):
            yield ChatScroll(id="chat-area")
            yield DetailsPanel()
        with Vertical(id="input-area"):
            yield Static("[@click=app.latest]↓ 返回最新 · Ctrl+End[/]", id="latest-link")
            yield Static("待输入", id="run-status", markup=False)
            with Vertical(id="composer"):
                yield Static("下一条消息", id="composer-label", markup=False)
                yield ChatInput(id="chat-input", placeholder="› 描述下一步要完成的工作…")
                yield Static(
                    "Enter 发送 · Shift+Enter 换行    Esc 停止 · F2 详情    / 命令 · @ 文件",
                    id="key-hints", markup=False,
                )
                yield CompletionPopup()
            with Horizontal(id="status-bar"):
                yield Static("● 默认权限", id="mode-label", markup=False)
                yield Static("", id="teammates-label", markup=False)
                yield Static("未配置 MCP", id="mcp-label", markup=False)
                yield Static("上下文 —", id="context-label", markup=False)

    def _handle_exception(self, error: Exception) -> None:
        from mewcode.diagnostics import record_exception
        record_exception("textual", error)
        super()._handle_exception(error)

    def on_mount(self) -> None:
        self.set_interval(1, self._refresh_details)
        if len(self.providers) == 1:
            self._select_provider(self.providers[0])
        else:
            self.query_one("#conversation-layout").display = False
            self.query_one("#input-area").display = False

    def _select_provider(self, provider: ProviderConfig) -> None:
        self._selected_provider = provider
        try:
            self.client = create_client(provider)
        except AuthenticationError as e:
            self._show_error(str(e))
            return

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
            mode=self._initial_permission_mode,
        )

        self._instructions_content = load_instructions(work_dir)
        self.memory_manager = MemoryManager(work_dir)
        self.session_manager = SessionManager(work_dir)
        self.session_manager.cleanup()
        self.session = self.session_manager.create()

        from mewcode.filehistory import FileHistory
        self.file_history = FileHistory(work_dir, self.session.session_id)
        for tool in self.registry.list_tools():
            if hasattr(tool, "file_history"):
                tool.file_history = self.file_history

        load_skill_tool = LoadSkill()
        self.registry.register(load_skill_tool)
        self._load_skill_tool = load_skill_tool

        self.registry.register(
            ToolSearchTool(self.registry, protocol=provider.protocol)
        )
        self.registry.register(AskUserTool())

        from mewcode.tools.exit_plan_mode import ExitPlanModeTool
        self._exit_plan_tool = ExitPlanModeTool()
        self.registry.register(self._exit_plan_tool)

        self.agent = Agent(
            client=self.client,
            registry=self.registry,
            protocol=provider.protocol,
            work_dir=work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=self._instructions_content,
            memory_manager=self.memory_manager,
            hook_engine=self.hook_engine,
            enable_supervision=self._enable_supervision,
        )
        self.agent.file_history = self.file_history
        self.agent.file_cache = self.file_cache
        self.agent.session_id = self.session.session_id

        self._exit_plan_tool._is_plan_mode = lambda: self.agent.plan_mode
        self._exit_plan_tool._plan_exists = lambda: self.agent._get_plan_path().exists()

        # Layer 2: 在后台异步拉取模型的 context window，不阻塞启动流程。
        # agent 已经有一个同步解析的窗口值（来自配置 / 映射表 / 默认值）；
        # 如果异步拉取成功，就原地升级为更准确的值。
        self.run_worker(
            self._resolve_context_window(provider), exclusive=False
        )

        self.skill_loader = SkillLoader(work_dir)
        self.skill_loader.load_all()

        load_skill_tool.set_loader(self.skill_loader)
        load_skill_tool.set_agent(self.agent)

        self.skill_executor = SkillExecutor(
            agent=self.agent,
            client=self.client,
            protocol=provider.protocol,
        )

        catalog = self.skill_loader.get_catalog()
        if catalog:
            lines = [
                "You can use the following Skills:",
                "",
            ]
            for name, desc in catalog:
                lines.append(f"- {name}: {desc}")
            lines.append("")
            lines.append(
                "If the user's request matches a Skill, call LoadSkill to activate it."
            )
            self.agent.set_skill_catalog("\n".join(lines))

        register_skill_commands(
            self.command_registry, self.skill_loader, self.skill_executor
        )

        # --- Worktree 系统初始化 ---
        from mewcode.config import WorktreeConfig
        wt_cfg = self._worktree_config or WorktreeConfig()
        self.worktree_manager = WorktreeManager(
            repo_root=work_dir,
            symlink_directories=wt_cfg.symlink_directories,
            file_cache=self.file_cache,
        )
        restored = self.worktree_manager.restore_session()
        if restored:
            self.agent.work_dir = restored.worktree_path
            self._instructions_content = load_instructions(restored.worktree_path)
            self.agent.instructions_content = self._instructions_content

        wt_command = create_worktree_command(self.worktree_manager)
        self.command_registry.register_sync(wt_command)

        from mewcode.tools.enter_worktree import EnterWorktreeTool
        from mewcode.tools.exit_worktree import ExitWorktreeTool
        self.registry.register(EnterWorktreeTool(worktree_manager=self.worktree_manager))
        self.registry.register(ExitWorktreeTool(worktree_manager=self.worktree_manager))

        self._stale_cleanup_task = asyncio.create_task(
            start_stale_cleanup_task(
                self.worktree_manager,
                wt_cfg.stale_cleanup_interval,
                wt_cfg.stale_cutoff_hours,
            )
        )

        # --- 子 agent 系统初始化 ---
        self.agent_loader = AgentLoader(
            work_dir, enable_verification=self._enable_verification_agent
        )
        self.agent_loader.load_all()

        # --- Agent 团队系统初始化 ---
        from mewcode.teams.manager import TeamManager
        from mewcode.tools.team_create import TeamCreateTool
        from mewcode.tools.team_delete import TeamDeleteTool

        self.team_manager = TeamManager(worktree_manager=self.worktree_manager, trace_manager=self.trace_manager)

        agent_tool = AgentTool(
            agent_loader=self.agent_loader,
            task_manager=self.task_manager,
            trace_manager=self.trace_manager,
            parent_agent=self.agent,
            enable_fork=self._enable_fork,
            provider_config=provider,
            worktree_manager=self.worktree_manager,
            team_manager=self.team_manager,
        )
        self.registry.register(agent_tool)

        team_create_tool = TeamCreateTool(
            team_manager=self.team_manager,
            parent_agent=self.agent,
            teammate_mode=self._teammate_mode,
            is_interactive=True,
            enable_coordinator_mode=self._enable_coordinator_mode,
        )
        self.registry.register(team_create_tool)

        team_delete_tool = TeamDeleteTool(
            team_manager=self.team_manager,
            parent_agent=self.agent,
        )
        self.registry.register(team_delete_tool)

        agent_catalog = self.agent_loader.list_agents()
        if agent_catalog:
            lines = [
                "## Available Sub-Agent Types",
                "",
                "Use the Agent tool with subagent_type parameter to delegate tasks:",
                "",
            ]
            for agent_type, when_to_use in agent_catalog:
                lines.append(f"- **{agent_type}**: {when_to_use}")
            if self._enable_fork:
                lines.append("")
                lines.append(
                    "Leave subagent_type empty to fork the current conversation "
                    "(inherits full dialog history)."
                )
            lines.append("")
            lines.append(
                "IMPORTANT: Sub-agents run in the background. "
                "After calling the Agent tool, you will get a task ID immediately. "
                "Do NOT wait, sleep, or poll for the result. "
                "Simply report the task ID to the user and end your turn. "
                "The system will automatically notify when the task completes."
            )
            self.agent.set_agent_catalog("\n".join(lines), catalog_list=agent_catalog)

        tasks_cmd = create_tasks_command(self.task_manager)
        self.command_registry.register_sync(tasks_cmd)

        from mewcode.commands.handlers.trace import create_trace_command
        trace_cmd = create_trace_command(self.trace_manager, self.agent.agent_id)
        self.command_registry.register_sync(trace_cmd)

        # --- 协调者模式初始化（工具已注册，激活推迟到 TeamCreate 时） ---
        from mewcode.tools.synthetic_output import SyntheticOutputTool

        self.registry.register(SyntheticOutputTool())
        self.agent._team_manager = self.team_manager

        if self.hook_engine:
            self._spawn_background(
                self.hook_engine.run_hooks(
                    "startup",
                    HookContext(event_name="startup"),
                    owner=self.agent.agent_id,
                )
            )

        if self._mcp_server_configs:
            self._mcp_init_task = asyncio.create_task(self._init_mcp())

        self.query_one("#model-label", Static).update(provider.model)
        work_dir = os.getcwd()
        self.query_one("#title-bar", Static).update(
            self._make_banner(provider.model, work_dir)
        )
        self._update_mode_label()
        self._branch_worker = self.run_worker(self._refresh_branch, group="header-git", exclusive=True)

        select = self.query("#provider-select")
        if select:
            select.first().display = False
        self.query_one("#conversation-layout").display = True
        self.query_one("#chat-area").display = True
        self.query_one("#input-area").display = True
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.placeholder = "› 描述下一步要完成的工作…"
        chat_input.load_history(work_dir)
        chat_input.focus()

        self._notification_check_task = asyncio.create_task(
            self._start_notification_polling()
        )

    async def _resolve_context_window(self, provider: ProviderConfig) -> None:
        """Layer 2 后台 worker：异步拉取模型的 context window，
        拉到就原地升级 agent 的窗口值。

        尽力而为 — resolve_context_window 不会抛异常；如果拉不到，
        agent 继续使用同步解析得到的窗口值。
        """
        await resolve_context_window(provider)
        if self.agent is not None:
            self.agent.context_window = provider.get_context_window()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "provider-list":
            provider = self.providers[event.option_index]
            self._select_provider(provider)

    # -----------------------------------------------------------------
    # UIController 协议实现
    # -----------------------------------------------------------------

    def add_system_message(self, text: str) -> None:
        self._show_system_message(text)

    def send_user_message(self, text: str) -> None:
        if self._streaming or self.agent is None:
            return
        self._agent_task = asyncio.create_task(self._send_message(text))

    def set_plan_mode(self, enabled: bool) -> None:
        if self.agent is None:
            return
        if enabled:
            self._pre_plan_mode = self.agent.permission_mode
            self.agent.set_permission_mode(PermissionMode.PLAN)
        else:
            restore = getattr(self, "_pre_plan_mode", PermissionMode.DEFAULT)
            self.agent.set_permission_mode(restore)
        self._update_mode_label()

    def get_token_count(self) -> tuple[int, int]:
        if self.agent:
            return self.agent.total_input_tokens, self.agent.total_output_tokens
        return 0, 0

    def refresh_status(self) -> None:
        self._update_mode_label()

    # -----------------------------------------------------------------
    # 命令分发
    # -----------------------------------------------------------------


    def _build_command_context(self, args: str) -> CommandContext:
        if self.agent is None or self.session_manager is None or self.memory_manager is None:
            raise RuntimeError("select a provider before running commands")
        return CommandContext(
            args=args,
            agent=self.agent,
            conversation=self.conversation,
            session=self.session,
            session_manager=self.session_manager,
            memory_manager=self.memory_manager,
            ui=self,
            config={
                "registry": self.command_registry,
                "set_session": self._set_session,
                "set_conversation": self._set_conversation,
                "clear_chat": self._clear_chat,
                "render_restored": self._render_restored_messages,
                "skill_loader": self.skill_loader,
                "skill_executor": self.skill_executor,
            },
        )

    def _set_session(self, session: Session) -> None:
        self.session = session
        if self.agent:
            self.agent.session_id = session.session_id

    def _persist_compact_boundary(self, notification: CompactNotification) -> None:
        """Layer-2 compact 后写入 compact_boundary 记录。

        将摘要 + 原样保留的尾部内联到一条记录中，resume 时只需这一条
        就能重建压缩后的状态。之前已写入磁盘的原始前缀不会被重放。
        没有活跃 session 或 compact 未产出 boundary 时直接跳过。
        """
        if not self.session or notification.boundary is None:
            return
        record = make_compact_boundary(
            notification.boundary.summary,
            notification.boundary.keep,
        )
        self.session.append_record(record)

    def _set_conversation(self, conv: ConversationManager) -> None:
        self.conversation = conv

    def _clear_chat(self) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        chat.remove_children()

    async def _dispatch_command(self, text: str) -> None:
        name, args, is_command = parse_command(text)

        if not is_command:
            if self._streaming or self.agent is None:
                return
            self._agent_task = asyncio.create_task(self._send_message(text))
            return

        if name == "":
            commands = self.command_registry.list_commands()
            lines = ["可用命令："]
            for listed_command in commands:
                aliases_str = ", ".join(f"/{a}" for a in listed_command.aliases)
                name_part = f"/{listed_command.name}"
                if aliases_str:
                    name_part += f", {aliases_str}"
                lines.append(f"  {name_part:<24} {listed_command.description}")
            self._show_system_message("\n".join(lines))
            return

        cmd = self.command_registry.find(name)
        if cmd is None:
            self._show_system_message(f"未知命令：/{name}，输入 /help 查看可用命令")
            return

        # 流式进行中禁止改状态命令：/clear 或 /session 会在运行中的
        # _send_message 持有旧 conversation/session 引用时直接替换它们，
        # 导致状态永久分裂
        if self._streaming and name in _STATEFUL_COMMANDS:
            self._show_system_message(
                f"/{name} 在回复进行中不可用，先按 Esc 打断"
            )
            return

        if not args and cmd.arg_prompt:
            self._show_system_message(cmd.arg_prompt)
            return

        ctx = self._build_command_context(args)
        try:
            await cmd.handler(ctx)
        except Exception as e:
            self._show_error(f"命令执行失败: {e}")

    # -----------------------------------------------------------------
    # 输入处理
    # -----------------------------------------------------------------

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        text = event.text.strip()
        if self._streaming and not text.startswith("/"):
            if self._agent_task and not self._agent_task.done():
                self._agent_task.cancel()
                try:
                    await self._agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._finish_streaming()
            self._show_system_message("(response interrupted)")
        await self._dispatch_command(text)

    def on_chat_input_tab_complete(self, event: ChatInput.TabComplete) -> None:
        matches = complete(self.command_registry, event.text)
        if not matches:
            return
        popup = self.query_one(CompletionPopup)
        if len(matches) == 1:
            input_widget = self.query_one("#chat-input", ChatInput)
            input_widget.clear()
            input_widget.insert(matches[0][1] + " ")
        else:
            popup.show_pairs(matches)

    def on_chat_input_slash_menu_update(self, event: ChatInput.SlashMenuUpdate) -> None:
        popup = self.query_one(CompletionPopup)
        if event.prefix is None:
            popup.hide()
            return
        matches = complete(self.command_registry, event.prefix)
        if not matches:
            popup.hide()
            return
        popup.show_pairs(matches)

    def on_chat_input_at_file_request(self, event: ChatInput.AtFileRequest) -> None:
        work_dir = self.agent.work_dir if self.agent else os.getcwd()
        prefix = event.prefix
        # 目录扫描挪入线程池 + 200ms 防抖：连续输入只取最后一次
        self._at_scan_seq = getattr(self, "_at_scan_seq", 0) + 1
        seq = self._at_scan_seq

        async def _scan() -> None:
            try:
                await asyncio.sleep(0.2)
                if seq != getattr(self, "_at_scan_seq", 0):
                    return
                matches = await asyncio.to_thread(scan_files_for_at, prefix, work_dir)
                if not matches or seq != getattr(self, "_at_scan_seq", 0):
                    return
                popup = self.query_one(CompletionPopup)
                popup.show([f"@{m}" for m in matches])
            except Exception:
                return

        asyncio.create_task(_scan())

    def on_completion_popup_selected(self, event: CompletionPopup.Selected) -> None:
        input_widget = self.query_one("#chat-input", ChatInput)
        selected = event.value
        text = input_widget.text
        if selected.startswith("@"):
            at_idx = text.rfind("@")
            if at_idx >= 0:
                input_widget.clear()
                input_widget.insert(text[:at_idx] + selected + " ")
                input_widget.focus()
                return
        input_widget.clear()
        input_widget.insert(selected + " ")
        input_widget.focus()

    def action_cycle_mode(self) -> None:
        from mewcode.plan_dialog import InlinePlanWidget

        dialog = self._focused_dialog(self.focused)
        if isinstance(dialog, InlinePlanWidget):
            dialog.action_approve_with_feedback()
            return
        if self.agent is None:
            return
        current = self.agent.permission_mode
        try:
            idx = _MODE_CYCLE.index(current)
        except ValueError:
            idx = 0
        next_mode = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
        self.agent.set_permission_mode(next_mode)
        self._update_mode_label()

    def action_toggle_tool_blocks(self) -> None:
        for block in self.query(ToolCallBlock):
            if block._loading:
                continue
            block._collapsed = not block._collapsed
            if block._collapsed:
                block._render_collapsed()
            else:
                block._render_expanded()

        for summary in self.query(ToolGroupSummary):
            summary.toggle()
            parent = summary.parent
            if parent:
                for child in parent.children:
                    if isinstance(child, ToolCallBlock) and child.tool_name in COLLAPSIBLE_TOOLS:
                        child.display = summary._expanded

        for subagent_block in self.query(SubAgentBlock):
            if subagent_block._done:
                subagent_block._collapsed = not subagent_block._collapsed
                subagent_block._render_done()

    @staticmethod
    def _focused_dialog(focused) -> InlineAskUserWidget | InlinePlanWidget | InlinePermissionWidget | None:
        """App 级 priority 绑定先于聚焦组件命中；弹窗聚焦时把 Esc /
        shift+tab 转发给弹窗自身，而不是吞掉（否则 Esc 会取消整个
        agent 运行、plan 弹窗的 shift+tab 成为死绑定）。"""
        if focused is None:
            return None
        from mewcode.askuser_dialog import InlineAskUserWidget
        from mewcode.permission_dialog import InlinePermissionWidget
        from mewcode.plan_dialog import InlinePlanWidget

        while focused is not None:
            if isinstance(focused, (InlineAskUserWidget, InlinePlanWidget, InlinePermissionWidget)):
                return focused
            focused = focused.parent
        return None

    def action_cancel(self) -> None:
        from mewcode.permission_dialog import InlinePermissionWidget

        dialog = self._focused_dialog(self.focused)
        if dialog is not None:
            if isinstance(dialog, InlinePermissionWidget):
                dialog.action_deny()
            else:
                dialog.action_cancel()
            return
        popup = self.query_one(CompletionPopup)
        if popup.is_visible:
            popup.hide()
            self.query_one("#chat-input", ChatInput).focus()
            return
        if self._details_open:
            self._details_open = False
            self._layout_details()
            self.query_one(ChatInput).focus()
            return
        if self._agent_task and not self._agent_task.done():
            self._request_cancel()

    def _request_cancel(self) -> None:
        if self._cancel_requested:
            return
        self._cancel_requested = True
        self._set_run_status("取消中")
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()

    async def _prefetch_relevant_memories(self, query: str) -> str:
        """Run the recall selector as a side-query with an 8s timeout.

        Creates a fresh LLM client so the selector's system prompt is
        independent of the main conversation's system prompt. Returns the
        rendered system-reminder body, or "" on any failure / timeout.
        """
        if self.memory_manager is None or self._selected_provider is None:
            return ""

        provider = self._selected_provider
        user_dir = self.memory_manager.user_mem_dir
        project_dir = self.memory_manager.project_mem_dir

        async def selector(system_prompt: str, user_message: str) -> str:
            from mewcode.tools.base import StreamEnd, TextDelta

            side_client = create_client(provider)
            mini_conv = ConversationManager()
            mini_conv.history = [Message(role="user", content=user_message)]
            collected = ""
            try:
                async for event in side_client.stream(mini_conv, system=system_prompt):
                    if isinstance(event, TextDelta):
                        collected += event.text
                    elif isinstance(event, StreamEnd):
                        pass
            finally:
                await side_client.close()
            return collected

        try:
            results = await asyncio.wait_for(
                find_relevant_memories(
                    query=query,
                    user_mem_dir=user_dir,
                    project_mem_dir=project_dir,
                    recent_tools=None,
                    already_surfaced=None,
                    selector=selector,
                    injected_keys=self.memory_manager.injected_keys(self.conversation),
                ),
                timeout=8.0,
            )
            return await asyncio.to_thread(render_reminder, results)
        except (TimeoutError, Exception):
            return ""

    async def _send_message(self, text: str, is_notification: bool = False) -> None:
        if self._streaming:
            self._show_system_message("(已有回复进行中，忽略重复请求)")
            return
        self._streaming = True
        self._cancel_requested = False
        self._execution_history.begin(text)
        self._agent_task = asyncio.current_task()
        self._thinking_start = _time.monotonic()
        self._set_run_status("连接工具" if self._mcp_connecting else "等待模型响应")
        self._start_spinner()
        try:
            await self._run_message(text, is_notification)
        except asyncio.CancelledError:
            self._set_run_status("已停止")
        except Exception as exc:
            from mewcode.diagnostics import record_exception
            record_exception("tui-run", exc)
            self._show_error(str(exc))
            self._set_run_status("出错")
        finally:
            self._execution_history.interrupted(
                "已停止 / 结果未知" if self._run_phase == "已停止" else "出错 / 结果未知"
                if self._run_phase == "出错" else "结果未知 / 未记录完成"
            )
            self._finish_streaming()
            await self._refresh_details()
            self._branch_worker = self.run_worker(self._refresh_branch, group="header-git", exclusive=True)
            for selector in ("#perm-inline", "#askuser-inline"):
                for widget in self.query(selector):
                    widget.remove()
            self._pending_perm_request = None
            self._pending_askuser_event = None
            input_widget = self.query_one("#chat-input", ChatInput)
            input_widget.disabled = False
            if not self.query("#plan-inline"):
                input_widget.focus()
            await self._process_task_notifications()

    async def _run_message(self, text: str, is_notification: bool) -> None:
        assert self.agent is not None
        intent = text if not is_notification else ""
        if self._mcp_init_task and not self._mcp_init_task.done():
            self._show_system_message("Waiting for MCP servers to connect...")
            try:
                await self._mcp_init_task
            except Exception as e:
                self._show_error(f"MCP 初始化失败: {e}")

        chat = self.query_one("#chat-area", VerticalScroll)
        if text and "@" in text:
            text = expand_at_refs(text, self.agent.work_dir)

        if text:
            await chat.mount(user_row(text))
            self._follow_chat()

            supervision = getattr(self.agent, "supervision", None)
            if supervision is not None and supervision.enabled:
                supervision.steer(intent)
            self.conversation.add_user_message(text)
            if self.session:
                self.session.append(Message(role="user", content=text))

        if self._mcp_instructions and not self._mcp_instructions_ok:
            self.conversation.add_system_reminder(self._mcp_instructions)
            self._mcp_instructions_ok = True

        # Await recall in this task so cancellation cannot leave an orphan side-query.
        if text:
            try:
                reminder = await asyncio.wait_for(self._prefetch_relevant_memories(text), timeout=3.0)
                if reminder:
                    self.conversation.add_system_reminder(reminder)
            except (TimeoutError, Exception):
                pass

        history_cursor = len(self.conversation.history)

        ai_row = Vertical(classes="ai-row")
        await chat.mount(ai_row)
        streaming_label: Markdown | None = None
        accumulated_text = ""
        last_stream_refresh = 0.0
        tool_blocks: dict[str, ToolCallBlock | SubAgentBlock] = {}
        run_failed = False
        self._run_phase = "等待模型响应"
        self._spinner_idx = 0
        self._spinner_label = self.query_one("#run-status", Static)

        # Mount teammate tree (initially hidden) below the spinner
        self._teammate_tree = TeammateTree(id="teammate-tree")
        self._teammate_tree.display = False
        await chat.mount(self._teammate_tree)
        self._start_teammate_polling()

        self._follow_chat()
        self._start_spinner()

        await asyncio.sleep(0)

        try:
            stream = (self.agent.run(self.conversation, intent=intent)
                      if getattr(self.agent, "supervision", None) and self.agent.supervision.enabled
                      else self.agent.run(self.conversation))
            async for event in stream:
                if isinstance(event, ThinkingText):
                    self._follow_chat()

                elif isinstance(event, StreamText):
                    self._run_phase = "接收回复"
                    if streaming_label is None:
                        segment = assistant_row("")
                        await ai_row.mount(segment)
                        streaming_label = segment.query_one(Markdown)
                    accumulated_text += event.text
                    now = _time.monotonic()
                    if now - last_stream_refresh >= 0.05:
                        await streaming_label.update(accumulated_text)
                        last_stream_refresh = now

                elif isinstance(event, RetryEvent):
                    self._show_system_message(f"↻ Retrying: {event.reason}")

                elif isinstance(event, ToolUseEvent):
                    self._execution_history.started(event.tool_id, event.tool_name, event.arguments)
                    await self._refresh_details()
                    self._run_phase = f"执行工具 · {event.tool_name}"
                    if streaming_label is not None:
                        await streaming_label.update(accumulated_text)
                    streaming_label = None
                    accumulated_text = ""

                    if _is_subagent_tool(event.tool_name):
                        agent_type = event.arguments.get("subagent_type", "")
                        desc = event.arguments.get("description", "")
                        block: ToolCallBlock | SubAgentBlock = SubAgentBlock(
                            agent_type or "agent",
                            desc,
                            classes="tool-block subagent-block",
                        )
                    else:
                        block = ToolCallBlock(
                            event.tool_name, event.arguments, classes="tool-block"
                        )
                    await ai_row.mount(block)
                    tool_blocks[event.tool_id] = block
                    self._follow_chat()

                    # AskUserQuestion 在 await 用户输入期间不产出事件，
                    # 必须在 tool_use 阶段就预创建事件并挂弹窗，
                    # 否则 UI 永远轮询不到（工具 300s 超时）
                    if event.tool_name == "AskUserQuestion":
                        ask_tool = self.registry.get("AskUserQuestion")
                        if isinstance(ask_tool, AskUserTool):
                            ask_event = ask_tool.prepare(
                                event.arguments.get("questions", [])
                            )
                            await self._handle_askuser(ask_event)

                elif isinstance(event, PermissionRequest):
                    await self._handle_permission_request(event)

                elif isinstance(event, ToolResultEvent):
                    self._execution_history.finished(event.tool_id, event.output, event.is_error, event.elapsed)
                    await self._refresh_details()
                    self._run_phase = "等待模型响应"
                    completed_block = tool_blocks.get(event.tool_id)
                    if completed_block:
                        completed_block.set_result(event.output, event.is_error, event.elapsed)
                    self._follow_chat()

                    ask_tool = self.registry.get("AskUserQuestion")
                    if (
                        isinstance(ask_tool, AskUserTool)
                        and ask_tool.current_question() is not None
                    ):
                        # 兜底：工具已完成但事件仍挂起（异步完成场景）
                        pending_question = ask_tool.current_question()
                        if pending_question is not None:
                            await self._handle_askuser(pending_question)

                elif isinstance(event, TurnComplete):
                    if self.session:
                        for msg in self.conversation.history[history_cursor:]:
                            self.session.append(msg)
                        history_cursor = len(self.conversation.history)

                    collapsible = [
                        (tid, blk) for tid, blk in tool_blocks.items()
                        if isinstance(blk, ToolCallBlock)
                        and blk.tool_name in COLLAPSIBLE_TOOLS
                        and not blk._loading
                    ]
                    if len(collapsible) >= 2:
                        total_elapsed = sum(b._elapsed for _, b in collapsible)
                        summary = ToolGroupSummary(
                            len(collapsible), total_elapsed,
                            classes="tool-block tool-group-summary",
                            failure_count=sum(b._is_error for _, b in collapsible),
                        )
                        for _, blk in collapsible:
                            blk.display = False
                        await ai_row.mount(summary)

                    if streaming_label is not None:
                        await streaming_label.update(accumulated_text)
                    streaming_label = None
                    accumulated_text = ""
                    tool_blocks.clear()
                    ai_row = Vertical(classes="ai-row")
                    await chat.mount(ai_row)
                    self._follow_chat()

                elif isinstance(event, UsageEvent):
                    pass  # token 展示已移除

                elif isinstance(event, HookEvent):
                    status = "✓" if event.success else "✗"
                    self._show_system_message(
                        f"Hook [{event.hook_id}] {status} {event.output}"
                    )

                elif isinstance(event, CompactNotification):
                    self._show_system_message(event.message)
                    # auto_compact 已重写 conversation.history（摘要 +
                    # boundary + 保留尾部）。先持久化 boundary 记录，然后
                    # 将游标推进到重建后的历史末尾，这样 TurnComplete/LoopComplete
                    # 刷盘时只追加 boundary 之后的新消息，不会把已压缩的
                    # 前缀作为普通记录重复写入。
                    self._persist_compact_boundary(event)
                    history_cursor = len(self.conversation.history)

                elif isinstance(event, ErrorEvent):
                    run_failed = True
                    self._show_error(event.message)

                elif isinstance(event, LoopComplete):
                    if self.session:
                        for msg in self.conversation.history[history_cursor:]:
                            self.session.append(msg)
                        history_cursor = len(self.conversation.history)
                        self.session.meta.total_tokens = (
                            self.agent.total_input_tokens
                            + self.agent.total_output_tokens
                        )
                        self._spawn_background(self._update_session_summary())
                    if self.agent.plan_mode:
                        self._spawn_background(self._show_plan_approval())

            if streaming_label is not None:
                await streaming_label.update(accumulated_text)
            self._set_run_status("出错" if run_failed else "本次运行结束")

        except asyncio.CancelledError:
            if streaming_label is not None:
                await streaming_label.update(accumulated_text)
            for block in tool_blocks.values():
                block.set_cancelled()
            self._set_run_status("已停止")
            self._show_system_message("运行已停止；已有修改保留。")
        except Exception as e:
            if streaming_label is not None:
                await streaming_label.update(accumulated_text)
            from mewcode.diagnostics import record_exception
            record_exception("tui-send", e)
            logger.exception("Unexpected error in _send_message")
            for block in tool_blocks.values():
                block.set_cancelled("出错")
            self._show_error(str(e))
            self._set_run_status("出错")
    async def _process_task_notifications(self) -> None:
        completed = self.task_manager.poll_completed()
        if not completed or self.agent is None:
            return

        inject_task_notifications(self.conversation, completed)

        for task in completed:
            status_icon = "✓" if task.status == "completed" else "✗"
            self._show_system_message(
                f"{status_icon} 后台任务完成: [{task.id}] {task.name} — {task.status}"
            )

        if self._streaming or (self._agent_task and not self._agent_task.done()):
            return
        self._agent_task = asyncio.create_task(
            self._send_message("", is_notification=True)
        )

    async def _start_notification_polling(self) -> None:
        while True:
            await asyncio.sleep(2)
            if not self._streaming and self.agent is not None:
                await self._process_task_notifications()
                await self._process_mailbox_notifications()

    async def _process_mailbox_notifications(self) -> None:
        if not hasattr(self, "team_manager") or self.team_manager is None:
            return
        if self._streaming or self.agent is None:
            return
        notes = self.team_manager.drain_lead_mailbox()
        if not notes:
            return
        for note in notes:
            self.conversation.add_system_reminder(note)
        if self._streaming or (self._agent_task and not self._agent_task.done()):
            return
        self._agent_task = asyncio.create_task(
            self._send_message("", is_notification=True)
        )

    async def _show_plan_approval(self) -> None:
        from mewcode.plan_dialog import InlinePlanWidget

        chat = self.query_one("#chat-area", VerticalScroll)
        self._set_run_status("等待计划审批")
        widget = InlinePlanWidget()
        self._details_open = False
        self._layout_details()
        await chat.mount(widget)
        widget.scroll_visible(animate=False, top=True)
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_plan_widget_responded(
        self, event: InlinePlanWidget.Responded
    ) -> None:
        from mewcode.plan_dialog import InlinePlanWidget, PlanChoice

        try:
            self.query_one("#plan-inline", InlinePlanWidget).remove()
        except Exception:
            pass
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

        if self.agent is None:
            return

        choice = event.choice
        feedback = event.feedback
        plan_path = self.agent._get_plan_path()
        plan_content = ""
        if plan_path.exists():
            try:
                plan_content = plan_path.read_text(encoding="utf-8")
            except Exception:
                pass

        pre = getattr(self, "_pre_plan_mode", PermissionMode.DEFAULT)
        if choice == PlanChoice.YOLO:
            self.agent.set_permission_mode(PermissionMode.BYPASS)
            self._update_mode_label()
            if plan_content:
                self.send_user_message(f"Execute this plan:\n\n{plan_content}")
        elif choice == PlanChoice.MANUAL:
            self.agent.set_permission_mode(pre)
            self._update_mode_label()
            if plan_content:
                self.send_user_message(f"Execute this plan:\n\n{plan_content}")
        elif choice == PlanChoice.FEEDBACK:
            if feedback:
                self.send_user_message(feedback)
            else:
                self._show_system_message("Type your feedback and send.")

    async def _handle_askuser(self, event: AskUserEvent) -> None:
        from mewcode.askuser_dialog import InlineAskUserWidget

        chat = self.query_one("#chat-area", VerticalScroll)
        self._set_run_status("等待用户回答")
        widget = InlineAskUserWidget(event.questions)
        self._pending_askuser_event = event
        self._details_open = False
        self._layout_details()
        await chat.mount(widget)
        widget.scroll_visible(animate=False, top=True)
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_ask_user_widget_responded(
        self, event: InlineAskUserWidget.Responded
    ) -> None:
        from mewcode.askuser_dialog import InlineAskUserWidget

        req = getattr(self, "_pending_askuser_event", None)
        if req is not None and not req.future.done():
            req.future.set_result(event.answers if event.answers else {})
            self._pending_askuser_event = None
        self._set_run_status("等待模型响应" if self._streaming else "待输入")
        try:
            self.query_one("#askuser-inline", InlineAskUserWidget).remove()
        except Exception:
            pass
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

    def _start_spinner(self) -> None:
        """启动 braille spinner 动画（每帧 80ms）。"""
        if self._spinner_timer is not None:
            return
        self._spinner_timer = self.set_interval(0.08, self._tick_spinner)

    def _stop_spinner(self) -> None:
        """停止 spinner 动画。"""
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def _finish_streaming(self) -> None:
        """清理所有 streaming 状态（取消或完成时调用）。"""
        self._streaming = False
        self._stop_spinner()
        self._stop_teammate_polling()
        self._agent_task = None
        if self._teammate_tree is not None:
            self._teammate_tree.remove()
            self._teammate_tree = None
        if self._spinner_label is not None:
            self._spinner_label = None

    def _set_run_status(self, phase: str) -> None:
        self._run_phase = phase
        self.query_one("#run-status", Static).update(phase)

    def _tick_spinner(self) -> None:
        self._spinner_idx += 1
        elapsed = _time.monotonic() - self._thinking_start
        self.query_one("#run-status", Static).update(
            f"{self._run_phase} · {elapsed:.0f}s · Esc 停止"
        )

    def _follow_chat(self) -> None:
        chat = self.query_one("#chat-area", ChatScroll)
        self.query_one("#latest-link").display = not chat.is_vertical_scroll_end

    def action_latest(self) -> None:
        self.query_one("#chat-area", ChatScroll).anchor()

    def on_resize(self) -> None:
        self.screen.set_class(self.size.height < 28, "compact")
        if self.query("#details-panel"):
            self._layout_details()
            self._refresh_header()

    def _layout_details(self) -> None:
        wide = self.size.width >= 120 and self.size.height >= 24
        panel = self.query_one(DetailsPanel)
        panel.display = self._details_open
        self.screen.set_class(self._details_open and not wide, "details-full")
        self.query_one("#chat-area").display = not self._details_open or wide

    async def action_details(self) -> None:
        if self.query("#perm-inline, #askuser-inline, #plan-inline"):
            return
        self._details_open = not self._details_open
        self._layout_details()
        if self._details_open:
            await self._refresh_details()
            self._branch_worker = self.run_worker(self._refresh_branch, group="header-git", exclusive=True)
        else:
            self.query_one(ChatInput).focus()

    async def _refresh_details(self) -> None:
        if not self.query(DetailsPanel):
            return
        if self.agent and self.agent.work_dir != self._header_work_dir:
            self._header_work_dir = self.agent.work_dir
            self._branch = "未知"
            self._refresh_header()
            self._branch_worker = self.run_worker(self._refresh_branch, group="header-git", exclusive=True)
        mcp = ("MCP 连接中" if self._mcp_connecting else self._mcp_server_info
               or ("MCP 未连接 / 状态未知" if self._mcp_server_configs else "未配置 MCP"))
        if self._selected_provider:
            mcp += f"\n配置模型：{self._selected_provider.model}\nGit 分支：{self._branch}"
        await self.query_one(DetailsPanel).refresh_facts(
            self._execution_history, self.task_manager, self.agent, mcp,
        )

    async def _refresh_branch(self) -> None:
        if self.agent:
            work_dir = self.agent.work_dir
            branch = await branch_name(work_dir)
            if self.agent.work_dir == work_dir:
                self._branch = branch
                self._header_work_dir = work_dir
                self._refresh_header()

    def _refresh_header(self) -> None:
        if not self._selected_provider or not self.agent or not self.query("#title-bar"):
            return
        work_dir = self.agent.work_dir
        self.query_one("#title-bar", Static).update(brand_title())
        workspace = self.query_one("#workspace-label", Static)
        width = workspace.size.width or max(1, self.size.width - len(self._selected_provider.model) - 8)
        text = workspace_title(work_dir, self._branch, width)
        workspace.update(text)
        workspace.tooltip = RichText(f"{work_dir}\n分支：{self._branch}")
        model = self.query_one("#model-label", Static)
        model.update(RichText(self._selected_provider.model))
        model.tooltip = RichText(self._selected_provider.model)

    def _start_teammate_polling(self) -> None:
        """Start polling teammate progress every 0.5s."""
        if self._teammate_timer is not None:
            return
        self._teammate_timer = self.set_interval(0.5, self._tick_teammate_tree)

    def _stop_teammate_polling(self) -> None:
        """Stop the teammate progress polling timer."""
        if self._teammate_timer is not None:
            self._teammate_timer.stop()
            self._teammate_timer = None

    def _tick_teammate_tree(self) -> None:
        """Poll team_manager for teammate progress and update the tree widget."""
        if not hasattr(self, "team_manager") or self.team_manager is None:
            return
        if self._teammate_tree is None:
            return

        progress_list = self.team_manager.get_all_teammate_progress()

        if not progress_list:
            self._teammate_tree.display = False
            self._update_teammates_label(0)
            return

        # Update the reactive properties via mutate_reactive for list
        self._teammate_tree.teammates = list(progress_list)

        # Update leader tokens from main agent
        if self.agent:
            self._teammate_tree.leader_tokens = (
                self.agent.total_input_tokens + self.agent.total_output_tokens
            )

        self._teammate_tree.display = True
        active_count = sum(1 for p in progress_list if p.status == "running")
        self._update_teammates_label(active_count)

    def _update_teammates_label(self, count: int) -> None:
        """Update the teammates count in the status bar."""
        try:
            label = self.query_one("#teammates-label", Static)
            if count > 0:
                label.update(f"[cyan]● {count} teammate{'s' if count != 1 else ''}[/cyan]  ")
            else:
                label.update("")
        except Exception:
            pass

    async def _handle_permission_request(self, request: PermissionRequest) -> None:
        from mewcode.permission_dialog import InlinePermissionWidget

        chat = self.query_one("#chat-area", VerticalScroll)
        self._set_run_status("等待权限")
        widget = InlinePermissionWidget(request.tool_name, request.description, request_token=request.future)
        self._pending_perm_request = request
        self._details_open = False
        self._layout_details()
        await chat.mount(widget)
        widget.scroll_visible(animate=False, top=True)
        # 权限提示弹窗期间禁用输入框
        try:
            self.query_one("#chat-input").disabled = True
        except Exception:
            pass

    def on_inline_permission_widget_responded(
        self, event: InlinePermissionWidget.Responded
    ) -> None:
        from mewcode.permission_dialog import InlinePermissionWidget

        req = self._pending_perm_request
        if req is None or event.request_token is not req.future:
            return
        if not req.future.done():
            req.future.set_result(event.reply)
        self._pending_perm_request = None
        self._set_run_status("等待模型响应" if self._streaming else "待输入")
        # 从聊天区移除权限弹窗组件
        try:
            widget = self.query_one("#perm-inline", InlinePermissionWidget)
            widget.remove()
        except Exception:
            pass
        # 重新启用输入框
        try:
            self.query_one("#chat-input").disabled = False
            self.query_one("#chat-input").focus()
        except Exception:
            pass

    # -----------------------------------------------------------------
    # 恢复 session 的消息渲染
    # -----------------------------------------------------------------

    async def _render_restored_messages(self, messages: list[Message]) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        await chat.remove_children()

        tools: dict[str, ToolCallBlock] = {}
        for msg in messages:
            if msg.content:
                if msg.role == "user" and not msg.tool_results:
                    await chat.mount(user_row(msg.content))
                elif msg.role == "assistant":
                    await chat.mount(assistant_row(msg.content))
            for call in msg.tool_uses:
                block = ToolCallBlock(call.tool_name, call.arguments, classes="tool-block")
                block.set_history()
                tools[call.tool_use_id] = block
                await chat.mount(block)
            for result in msg.tool_results:
                if restored_block := tools.get(result.tool_use_id):
                    restored_block.set_history(result.content, result.is_error)
        chat.anchor()

    # -----------------------------------------------------------------
    # Session 摘要（异步后台生成）
    # -----------------------------------------------------------------

    async def _update_session_summary(self) -> None:
        if not self.session or not self.client or not self.agent:
            return
        try:
            summary = await generate_session_summary(
                self.client, self.conversation, self.agent.protocol
            )
            if summary:
                self.session.meta.summary = summary
                self.session.meta.save(
                    self.session._sessions_dir / f"{self.session.session_id}.meta"
                )
        except Exception:
            pass

    # -----------------------------------------------------------------
    # MCP
    # -----------------------------------------------------------------

    async def _init_mcp(self) -> None:
        self._mcp_connecting = True
        self._update_mode_label()
        manager = MCPManager()
        manager.load_configs(self._mcp_server_configs)
        tools_before = len(self.registry.list_tools())
        self.mcp_manager = manager
        try:
            errors = await manager.register_all_tools(self.registry)
        except asyncio.CancelledError:
            self._mcp_server_info = "MCP 连接已停止 / 状态未知"
            raise
        except Exception:
            self._mcp_server_info = "MCP 连接失败 / 状态未知"
            raise
        finally:
            self._mcp_connecting = False
            self._update_mode_label()
        for err in errors:
            self._show_system_message(f"MCP warning: {err}")
        tools_after = len(self.registry.list_tools())
        mcp_tools = tools_after - tools_before
        server_count = len(manager._clients)
        self._mcp_server_info = (
            f"MCP {'部分失败' if errors and server_count else '连接失败' if errors else '已连接'} · "
            f"{server_count} 个服务 / {mcp_tools} 个工具"
        )
        self._update_mode_label()
        if server_count > 0 and mcp_tools > 0:
            parts = []
            for cfg in self._mcp_server_configs:
                srv_name = cfg.name if hasattr(cfg, 'name') else str(cfg)
                tool_names = [
                    t.name for t in self.registry.list_tools()
                    if t.name.startswith(f"mcp__{srv_name}__")
                ]
                section = f"## {srv_name}\n"
                if tool_names:
                    section += "Available tools: " + ", ".join(tool_names)
                parts.append(section)
            self._mcp_instructions = (
                "# MCP Server Instructions\n\n"
                "The following MCP servers are connected. "
                "Use their tools when the user asks.\n\n"
                + "\n\n".join(parts)
            )

    async def _shutdown_mcp(self) -> None:
        if self._mcp_init_task is not None:
            self._mcp_init_task.cancel()
            try:
                await self._mcp_init_task
            except (asyncio.CancelledError, Exception):
                pass
            self._mcp_init_task = None
        if self.mcp_manager is not None:
            await self.mcp_manager.shutdown()
            self.mcp_manager = None

    def _spawn_background(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        """ensure_future 并强引用保存，防止任务被 GC 中途回收。"""
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    # -----------------------------------------------------------------
    # 退出
    # -----------------------------------------------------------------

    async def action_handle_ctrl_c(self) -> None:
        if self._streaming:
            self._request_cancel()
            return

        if getattr(self, "_exit_requested", False):
            self.exit()
            return
        self._exit_requested = True

        async def _cleanup() -> None:
            tasks: list[asyncio.Task] = []

            # 统一取消仍在跑的 fire-and-forget 后台任务，避免与下面的
            # 收尾提取/关闭流程并发写同一状态。
            for t in list(self._bg_tasks):
                t.cancel()
            if self.agent is not None:
                await self.agent.cancel_background_tasks()
            if self.hook_engine is not None:
                await self.hook_engine.cancel_background()

            if self.agent and self.agent.memory_manager:
                tasks.append(asyncio.create_task(
                    self.agent._extract_memories(self.conversation)
                ))
            if self.hook_engine and self.agent is not None:
                tasks.append(asyncio.create_task(
                    self.hook_engine.run_hooks(
                        "shutdown",
                        HookContext(event_name="shutdown"),
                        owner=self.agent.agent_id,
                    )
                ))
            tasks.append(asyncio.create_task(self._shutdown_mcp()))

            if tasks:
                await asyncio.wait(tasks, timeout=3.0)
                for t in tasks:
                    if not t.done():
                        t.cancel()

            if self._stale_cleanup_task and not self._stale_cleanup_task.done():
                self._stale_cleanup_task.cancel()

            if hasattr(self, 'team_manager'):
                await self.task_manager.shutdown_all()
                for name in list(self.team_manager._teams):
                    try:
                        team = self.team_manager._teams[name]
                        for m in team.members:
                            team.set_member_active(m.name, False)
                        await self.team_manager.shutdown_team(name)
                        self.team_manager.delete_team(name)
                    except Exception:
                        pass

            if self.session:
                self.session.close()

        try:
            await _cleanup()
        except Exception:
            pass
        self.exit()

    async def on_unmount(self) -> None:
        await self.task_manager.shutdown_all()
        if hasattr(self, "team_manager"):
            await self.team_manager.shutdown_all()
        # Textual cancels workers on shutdown; wait for bounded Git pipe cleanup too.
        workers = list(self.workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)

    def _show_error(self, text: str) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        error_widget = Static(f"✖ {text}", markup=False, classes="message error-message")
        chat.mount(error_widget)
        self._follow_chat()

    def _show_system_message(self, text: str) -> None:
        chat = self.query_one("#chat-area", VerticalScroll)
        msg = Static(f"  {text}", markup=False, classes="message system-message")
        chat.mount(msg)
        self._follow_chat()

    _MODE_DISPLAY = {
        PermissionMode.DEFAULT: "默认权限",
        PermissionMode.ACCEPT_EDITS: "accept-edits",
        PermissionMode.PLAN: "plan",
        PermissionMode.BYPASS: "YOLO",
    }

    def _update_mode_label(self) -> None:
        if self.agent:
            perm = self.agent.permission_mode
            display = self._MODE_DISPLAY.get(perm, perm.value)
            color = _MODE_COLORS.get(perm, "dim")
            label = self.query_one("#mode-label", Static)
            if perm == PermissionMode.DEFAULT:
                label.update(RichText(f"● {display}", style=MEWCODE_THEME.success or "#197653"))
            else:
                label.update(f"[{color}]{display}[/{color}]  (shift+tab to cycle)")
        if self._selected_provider:
            self.query_one("#model-label", Static).update(RichText(self._selected_provider.model))
        mcp = ("MCP 连接中…" if self._mcp_connecting else self._mcp_server_info
               or ("MCP 未连接 / 状态未知" if self._mcp_server_configs else "未配置 MCP"))
        self.query_one("#mcp-label", Static).update(RichText(mcp))
