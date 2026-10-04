"""执行链：把 job 从告警变成"已验证的代码改动"（服务层 ↔ 现有内核的接线）。

对应 docs/evolution/03-m1-alert-driven.md W3：
1. 建隔离工作区（worktree）——一切本地动作都在这里，可安全丢弃；
2. 组装告警上下文 + 排查 SOP 提示词；
3. 调用 ``Agent.run_to_completion``（headless 权限语义，无人可问）；
4. 把关键事件（工作区、基线测试、工具调用统计、token、验证结果）写进 job
   审计记录——失败 escalate 时，这些就是"已尝试的分析"。

修复-验证循环有界：验证失败会带着测试输出重跑 agent，次数由 JobStore 的
``max_fix_attempts`` 强制执行（超限 escalate，不死循环烧 token）。

发布（push/PR）不在本模块：由注入的 publisher 负责（W4）。未配置 publisher
时执行链**不会**假装成功，而是 escalate 并保留证据。

所有外部依赖（agent 运行、测试命令、发布）都通过构造参数注入，
因此执行链本身可以完全脱网单测。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mewcode.config import RepoConfig, ServiceConfig

from . import sop
from .jobs import InvalidTransition, Job, JobStore

log = logging.getLogger(__name__)

MAX_DIFF_CHARS = 60_000
MAX_EVIDENCE_CHARS = 4_000
#: 传给 agent 的失败反馈上限（验证失败重试时）
MAX_FEEDBACK_CHARS = 3_000

#: 不算"修复产物"的路径：服务自身状态与 Python 缓存。
#: diff 统计（本模块）与提交（publisher）必须用**同一份清单**，否则 PR body
#: 里的改动规模会与真实提交对不上——真机踩到：容器内 agent 在 worktree 里写了
#: .mewcode/debug.log，body 报"2 files, +32"而实际提交只有 app.py 的 +3。
IGNORED_DIRS = (".mewcode", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")


def is_ignored_path(path: str) -> bool:
    """路径里出现任一被忽略目录名即视为非产物（与提交排除的语义一致）。"""
    return any(part in IGNORED_DIRS for part in path.replace("\\", "/").split("/"))


def commit_excludes() -> list[str]:
    """git 的 exclude pathspec：每个目录给出根目录与任意深度两种 glob。

    git 的 exclude pathspec 不做递归匹配，漏掉任一种都会把服务状态提交进 PR。
    """
    return [
        spec
        for _dir in IGNORED_DIRS
        for spec in (f":(exclude,glob){_dir}/**", f":(exclude,glob)**/{_dir}/**")
    ]


#: 提交时使用的固定排除清单（publisher 直接引用；语义与 :func:`commit_excludes` 一致）
COMMIT_EXCLUDES: tuple[str, ...] = tuple(commit_excludes())


class TokenBudgetExceeded(Exception):
    """单 job token 预算熔断（架构文档：成本控制双重要求之一）。"""


@dataclass
class TestOutcome:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        if self.timed_out:
            return f"`{self.command}` → TIMEOUT"
        return f"`{self.command}` → exit {self.exit_code}"


@dataclass
class IntegrationOutcome:
    """集成验证（自起测试环境，M2 W4）的结果：PR 证据与失败反馈的原料。

    ``skipped`` 非空表示"仓库带了 compose 文件、这一步本该做但没能做"
    （未配置命令 / 容器运行时不可用）——证据链里如实记录，不假装验证过。
    """

    compose_file: str
    project: str = ""
    network: str = ""
    up_ok: bool = False
    up_output: str = ""
    services: str = ""
    test: TestOutcome | None = None
    down_output: str = ""
    skipped: str = ""

    @property
    def ran(self) -> bool:
        return not self.skipped

    @property
    def passed(self) -> bool:
        return bool(self.ran and self.up_ok and self.test is not None and self.test.passed)

    def summary(self) -> str:
        if self.skipped:
            return f"skipped ({self.skipped})"
        if not self.up_ok:
            return f"`{self.compose_file}`: docker compose up failed (project {self.project})"
        if self.test is None:
            return "integration test did not run"
        return self.test.summary()

    def failure_feedback(self) -> str:
        """失败重试时交给 agent 的原始输出（与单测失败同一条反馈通路）。"""
        if not self.up_ok:
            return (
                f"the repository has {self.compose_file} and the service could not start "
                f"the dependencies with docker compose (project {self.project}):\n{self.up_output}"
            )
        return self.test.output if self.test is not None else ""


@dataclass
class AgentRunOutcome:
    final_text: str = ""
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: 内部工具链使用情况（M2 W3）：PR body 里"这次修复用到了哪些内部系统"的证据
    mcp_tool_calls: int = 0
    mcp_tools_used: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class PublishResult:
    pr_url: str = ""
    branch: str = ""
    extra_events: list[tuple[str, str]] = field(default_factory=list)


class AgentRunner(Protocol):
    """跑一次 agent（真实实现调 run_to_completion，测试注入假的）。"""

    async def run(
        self, job: Job, work_dir: str, prompt: str, on_event: Callable[[dict[str, Any]], None]
    ) -> AgentRunOutcome: ...


class Publisher(Protocol):
    """把 worktree 里的改动发布成 PR（W4 实现）。"""

    async def publish(self, job: Job, context: ExecutionContext) -> PublishResult: ...


class CIGate(Protocol):
    """CI 门禁：等远端 checks 出结论（W4 实现）。"""

    async def wait(
        self, job: Job, context: ExecutionContext, published: PublishResult
    ) -> Any: ...


class IntegrationVerifier(Protocol):
    """自起测试环境验证（W4 实现）：仓库含 compose 文件时起依赖、跑集成测试、清理。

    返回 ``None`` 表示"这个仓库没有 compose 文件、不需要集成验证"；返回
    ``IntegrationOutcome`` 表示这一步适用（``skipped`` 非空 = 本该做但没做）。
    """

    async def verify(
        self, job: Job, repo: RepoConfig, work_dir: str
    ) -> IntegrationOutcome | None: ...


@dataclass
class ExecutionContext:
    """执行链产物 = 交给 publisher 的证据链（PR body 的原料）。"""

    repo: RepoConfig
    work_dir: str
    prompt: str
    agent: AgentRunOutcome
    changed_files: list[str]
    diff: str
    baseline_test: TestOutcome | None
    verify_test: TestOutcome | None
    attempts: int = 1
    #: 集成验证结果（M2 W4）；None = 仓库没有 compose 文件、这一步不适用
    integration: IntegrationOutcome | None = None

    def diff_stat(self) -> str:
        added = sum(
            1 for line in self.diff.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1 for line in self.diff.splitlines() if line.startswith("-") and not line.startswith("---")
        )
        return f"{len(self.changed_files)} file(s), +{added}/-{removed}"


# ---------------------------------------------------------------------------
# 测试命令执行
# ---------------------------------------------------------------------------


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """显式收尾子进程树——不能依赖事件循环退出兜底（仓库已知坑）。

    Windows 上 cmd.exe 的子进程不在进程组语义内，用 taskkill /T 收；
    POSIX 上按进程组 kill（start_new_session=True 已隔离）。
    """
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except TimeoutError:  # pragma: no cover - taskkill 未果时的兜底
                proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError) as e:  # pragma: no cover
        log.warning("failed to terminate test process tree: %s", e)
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _absorb_cancellation(exc: BaseException) -> str:
    """明确"吃掉"一次取消（PEP 678 的配套语义：吞掉就要 uncancel）。

    返回取消的摘要文本，供审计记录使用。不 uncancel 的话任务会一直带着
    "cancelling" 计数，后面的 wait_for / timeout 语义会被这枚陈旧计数干扰。
    """
    task = asyncio.current_task()
    if task is not None and hasattr(task, "uncancel"):
        task.uncancel()
    return f"{type(exc).__name__}: {exc}"


async def shutdown_agent_resources(agent: Any, mcp_result: Any) -> list[str]:
    """显式收尾 agent 后台任务与 MCP 连接；返回收尾窗口里吞掉的取消摘要。

    MCP 的 stdio 传输基于 anyio cancel scope：收尾时它会取消宿主任务，且**投递
    点不确定**——真机实测（两个 server = 两个作用域）其中一次投递落在收尾之后的
    第一个 await 上，顺着 CancelledError 把执行链和 worker 消费者一起打死
    （服务活着却不再消费任何 job）。

    收尾必须完成，所以收尾窗口里的取消一律记下来，不让它逃逸；调用方把摘要写进
    审计。真取消（服务关停）由调用方的 finally 之后继续传播——worker 侧还有
    队列哨兵兜底（worker.stop），吞掉一次不会把关停卡住。
    """
    absorbed: list[str] = []
    try:
        await agent.cancel_background_tasks()
    except asyncio.CancelledError as e:
        absorbed.append(_absorb_cancellation(e))
    try:
        from mewcode.mcp.bootstrap import close_mcp

        await close_mcp(mcp_result)  # 契约"永不抛出"；这层保险不打折
    except asyncio.CancelledError as e:  # pragma: no cover - close_mcp 已吞过一层
        absorbed.append(_absorb_cancellation(e))
    # 收尾之后的第一个 await：迟到的投递在这里显形（真机实测点）
    try:
        await asyncio.sleep(0)
    except asyncio.CancelledError as e:
        absorbed.append(_absorb_cancellation(e))
    return absorbed


class TestRunner:
    """在 worktree 内执行仓库测试命令，返回结构化结果。

    子进程环境固定 ``PYTHONDONTWRITEBYTECODE=1``：.pyc 头里存的是**秒级**
    mtime，同一秒内等长的改动（如 ``a - b`` → ``a + b``）会被判定为"缓存有效"，
    于是验证阶段跑的是基线阶段的旧字节码——修复被测试误判为无效。
    这是执行链的真问题（agent 快速改一行就会踩到），不是测试环境的怪癖。
    """

    async def run(self, work_dir: str, command: str, timeout: float) -> TestOutcome:
        if not command:
            raise ValueError("empty test command")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=(sys.platform != "win32"),
            env=env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = (stdout or b"").decode("utf-8", errors="replace")
            return TestOutcome(command=command, exit_code=proc.returncode or 0, output=output)
        except TimeoutError:
            await _terminate_process_tree(proc)
            return TestOutcome(
                command=command,
                exit_code=-1,
                output=f"(timed out after {timeout:.0f}s)",
                timed_out=True,
            )


class SandboxTestRunner:
    """在容器里跑仓库测试命令（M2：验证阶段也隔离——它执行的是仓库代码）。

    容器不可用时回退宿主直跑（M1 行为），并在事件里留下降级记录。
    """

    def __init__(self, sandbox: Any, *, repo_name: str = "job", fallback: TestRunner | None = None) -> None:
        self.sandbox = sandbox
        self.repo_name = repo_name
        self.fallback = fallback or TestRunner()

    async def run(self, work_dir: str, command: str, timeout: float) -> TestOutcome:
        if not command:
            raise ValueError("empty test command")
        try:
            available = await self.sandbox.available()
        except Exception as e:  # pragma: no cover - 探测异常按不可用处理
            log.warning("sandbox probe failed (%s); running tests on the host", e)
            available = False
        if not available:
            log.warning("sandbox unavailable; running `%s` on the host", command)
            return await self.fallback.run(work_dir, command, timeout)

        result = await self.sandbox.run_command(
            self.repo_name, work_dir, command, repo_name=self.repo_name, timeout=timeout
        )
        return TestOutcome(
            command=command,
            exit_code=result.exit_code,
            output=result.stdout,
            timed_out=result.timed_out,
        )


# ---------------------------------------------------------------------------
# 真实 agent 运行器
# ---------------------------------------------------------------------------


class HeadlessAgentRunner:
    """构造并运行 agent 内核（服务模式的权限语义在这里落地）。

    两种执行模式：
    - **沙箱模式（M2）**：agent 在容器内跑（``mewcode -p --output-format json``），
      OS 级隔离覆盖文件/进程/网络；容器不可用时**自动回退**直跑并记 warning
      （M2 验收标准 4）。
    - **直跑模式（M1）**：在宿主进程内跑 agent 内核。

    权限语义（架构文档决策 4，两种模式一致）：
    - ``PermissionMode.DONT_ASK``：无人值守下没有"询问"的语义，ask 一律视为允许；
    - 沙箱根 = worktree 路径：越界读写被拒；
    - 危险命令检测器照常生效（deny 优先于白名单）；
    - 仓库自己的 ``.mewcode/permissions.yaml`` 作为 per-repo 策略文件参与判定。
    """

    def __init__(
        self,
        config: ServiceConfig,
        provider: Any,
        hook_engine: Any = None,
        sandbox: Any = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.hook_engine = hook_engine
        self.sandbox = sandbox
        self._sandbox_decision: bool | None = None

    async def _use_sandbox(self) -> bool:
        """是否走沙箱（探测一次并缓存；不可用只警告一次，不阻塞作业）。"""
        if self.sandbox is None or not self.config.sandbox.enabled:
            return False
        if self._sandbox_decision is None:
            available = await self.sandbox.available()
            self._sandbox_decision = available
            if not available:
                log.warning(
                    "sandbox enabled but the container runtime is unavailable; "
                    "falling back to direct execution (M1 mode)"
                )
        return bool(self._sandbox_decision)

    def _build_agent(self, work_dir: str):
        from mewcode.agent import Agent
        from mewcode.client import create_client
        from mewcode.memory.instructions import load_instructions
        from mewcode.permissions import (
            DangerousCommandDetector,
            PathSandbox,
            PermissionChecker,
            RuleEngine,
        )
        from mewcode.permissions.modes import PermissionMode
        from mewcode.tools import create_default_registry

        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(work_dir),
            rule_engine=RuleEngine(
                user_rules_path=Path(work_dir) / ".mewcode" / "permissions.yaml",
                project_rules_path=Path(work_dir) / ".mewcode" / "permissions.yaml",
                local_rules_path=Path(work_dir) / ".mewcode" / "permissions.local.yaml",
            ),
            mode=PermissionMode.DONT_ASK,
        )
        return Agent(
            client=create_client(self.provider),
            # bind_work_dir 是隔离的关键：不绑定的话 Bash 的 cwd 是本进程的
            # 工作目录（服务启动目录），命令会跑在**主仓库**而不是 job 的
            # worktree 里，相对路径工具同理。CLI 场景 cwd 恰好等于 work_dir，
            # 掩盖了这一点；服务场景必须显式绑定（真机验收时实测踩到）。
            registry=create_default_registry().bind_work_dir(work_dir),
            protocol=self.provider.protocol,
            work_dir=work_dir,
            permission_checker=checker,
            context_window=self.provider.get_context_window(),
            instructions_content=load_instructions(work_dir),
            hook_engine=self.hook_engine,
        )

    async def _build_agent_with_tools(self, work_dir: str, on_event):
        """建 agent 并接上内部工具链（只读 MCP，M2 W3）。

        直跑模式下 MCP server 是本进程拉起的 stdio 子进程；注册失败只记
        事件、不阻塞作业——内部工具是增强项，不是修复的前提。
        未配置任何 server 时完全不碰注册表（零开销、零行为变化）。
        """
        from mewcode.mcp.bootstrap import MCPBootstrapResult, register_mcp_tools

        agent = self._build_agent(work_dir)
        if not self.config.mcp_servers:
            return agent, MCPBootstrapResult()
        result = await register_mcp_tools(agent.registry, self.config.mcp_servers)
        kind = "mcp_ready" if result.ready else "mcp_error"
        on_event({"type": kind, "detail": f"direct: {result.summary()}"})
        return agent, result

    async def run(
        self, job: Job, work_dir: str, prompt: str, on_event: Callable[[dict[str, Any]], None]
    ) -> AgentRunOutcome:
        if await self._use_sandbox():
            return await self._run_in_sandbox(job, work_dir, prompt, on_event)
        return await self._run_direct(job, work_dir, prompt, on_event)

    async def _run_in_sandbox(
        self, job: Job, work_dir: str, prompt: str, on_event: Callable[[dict[str, Any]], None]
    ) -> AgentRunOutcome:
        """容器内跑 agent；容器输出即结构化摘要，直接回读。"""
        on_event({"type": "sandbox", "detail": f"running agent in sandbox for {job.id}"})
        mcp_servers = list(self.config.mcp_servers or [])
        if mcp_servers:
            # MCP server 是**容器内**拉起的 stdio 子进程：宿主看不到它们的连接，
            # 这里记下"配置了哪些"，使用证据由容器回报的 mcpCalls/mcpTools 补上。
            on_event({
                "type": "mcp_ready",
                "detail": "in-container: servers=" + ",".join(c.name for c in mcp_servers),
            })
        # 容器自己设一个比 worker 超时略早的止损点：这样超时是"容器被停"而不是
        # 整个 job 被 wait_for 取消——前者能留下容器日志与明确的超时原因。
        timeout = max(60.0, float(self.config.job_timeout_seconds) - 30.0)
        try:
            result = await self.sandbox.run_agent(
                job.id, work_dir, prompt, self.provider,
                repo_name=job.repo, timeout=timeout, mcp_servers=mcp_servers,
            )
        except Exception as e:  # 沙箱自身故障：记事件并冒泡（执行链会 escalate）
            on_event({"type": "sandbox_error", "detail": f"{type(e).__name__}: {e}"})
            raise

        on_event({
            "type": "usage",
            "usage": {"inputTokens": result.input_tokens, "outputTokens": result.output_tokens},
        })
        if result.timed_out:
            raise TimeoutError(f"sandbox agent run timed out after {timeout:.0f}s")
        if result.exit_code != 0 and not result.result_text:
            tail = (result.stderr or result.stdout or "").strip()[-1500:]
            raise RuntimeError(f"sandbox agent exited with {result.exit_code}: {tail}")
        mcp_calls = int(result.extra.get("mcpCalls", 0) or 0)
        mcp_tools = [str(name) for name in (result.extra.get("mcpTools") or [])]
        if mcp_calls:
            on_event({"type": "mcp_used", "detail": f"{mcp_calls} call(s): {', '.join(mcp_tools)}"})
        return AgentRunOutcome(
            final_text=result.result_text,
            tool_calls=result.tool_calls,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            mcp_tool_calls=mcp_calls,
            mcp_tools_used=mcp_tools,
        )

    async def _run_direct(
        self, job: Job, work_dir: str, prompt: str, on_event: Callable[[dict[str, Any]], None]
    ) -> AgentRunOutcome:
        from mewcode.conversation import ConversationManager

        agent, mcp_result = await self._build_agent_with_tools(work_dir, on_event)
        outcome = AgentRunOutcome()
        budget = self.config.token_budget

        def _callback(event: dict[str, Any]) -> None:
            on_event(event)
            etype = event.get("type")
            if etype == "usage":
                usage = event.get("usage") or {}
                outcome.input_tokens = int(usage.get("inputTokens", outcome.input_tokens))
                outcome.output_tokens = int(usage.get("outputTokens", outcome.output_tokens))
                if budget and outcome.total_tokens > budget:
                    raise TokenBudgetExceeded(
                        f"token budget {budget} exceeded "
                        f"({outcome.input_tokens} in + {outcome.output_tokens} out)"
                    )
            elif etype == "tool_use":
                outcome.tool_calls += 1
                name = str(event.get("toolName") or "")
                if name.startswith("mcp_"):
                    outcome.mcp_tool_calls += 1
                    if name not in outcome.mcp_tools_used:
                        outcome.mcp_tools_used.append(name)

        try:
            outcome.final_text = await agent.run_to_completion(
                prompt, ConversationManager(), event_callback=_callback
            )
        finally:
            # 显式收尾：agent 可能留下后台任务（子代理 / fire-and-forget），
            # MCP 侧还挂着 stdio 子进程——都不能依赖进程退出兜底（仓库已知坑）。
            # 收尾窗口里任何一次"取消"都记成事件（MCP 的作用域取消会漏到这里，
            # 见 shutdown_agent_resources 的说明），不让它改写作业的结果语义。
            absorbed = await shutdown_agent_resources(agent, mcp_result)
            if absorbed:
                on_event({
                    "type": "teardown_cancellation",
                    "detail": "; ".join(absorbed)[:500],
                })
        if outcome.mcp_tool_calls:
            on_event({
                "type": "mcp_used",
                "detail": f"{outcome.mcp_tool_calls} call(s): {', '.join(outcome.mcp_tools_used)}",
            })
        return outcome


# ---------------------------------------------------------------------------
# 执行链
# ---------------------------------------------------------------------------


class ExecutionChain:
    def __init__(
        self,
        config: ServiceConfig,
        store: JobStore,
        runner: AgentRunner,
        *,
        test_runner: TestRunner | None = None,
        publisher: Publisher | None = None,
        ci_gate: CIGate | None = None,
        integration_verifier: IntegrationVerifier | None = None,
        notifier: Any = None,
        worktree_manager_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.runner = runner
        self.test_runner = test_runner or TestRunner()
        self.publisher = publisher
        self.ci_gate = ci_gate
        self.integration_verifier = integration_verifier
        self.notifier = notifier
        self._worktree_manager_factory = worktree_manager_factory
        self._managers: dict[str, Any] = {}
        self._event_tasks: dict[str, list[asyncio.Task[Any]]] = {}

    # -- 辅助 -------------------------------------------------------------

    def _worktree_manager(self, repo_root: str):
        if repo_root not in self._managers:
            if self._worktree_manager_factory is not None:
                self._managers[repo_root] = self._worktree_manager_factory(repo_root)
            else:
                from mewcode.worktree import WorktreeManager

                self._managers[repo_root] = WorktreeManager(
                    repo_root=repo_root,
                    symlink_directories=["node_modules", ".venv", "vendor"],
                )
        return self._managers[repo_root]

    def _skill_bodies(self, work_dir: str) -> dict[str, str]:
        """服务启用的规范正文（仓库自带同名 skill 会自动覆盖内置版）。

        ``service.skills`` 三态：未配置（None）= 内置默认包；``[]`` = 明确不注入
        （"关闭规范"的对比 demo）；非空列表 = 只注入清单里的。
        """
        names = list(sop.DEFAULT_SKILLS) if self.config.skills is None else self.config.skills
        return sop.load_skill_bodies(work_dir, names)

    async def _event(self, job_id: str, kind: str, detail: str) -> None:
        await self.store.add_event(job_id, kind, detail[:MAX_EVIDENCE_CHARS])

    async def _transition(
        self, job_id: str, to_state: str, reason: str = "", **fields: Any
    ) -> Job | None:
        """转移被拒（非法/超限）不抛给 worker——返回 None，由调用方决定收尾。"""
        try:
            return await self.store.transition(job_id, to_state, reason, **fields)
        except InvalidTransition as e:
            log.warning("execution chain: transition %s -> %s rejected: %s", job_id, to_state, e)
            await self._event(job_id, "transition_rejected", str(e))
            return None

    async def _escalate(self, job_id: str, reason: str) -> None:
        await self._event(job_id, "escalated", reason)
        await self._transition(job_id, "escalate", reason=reason, last_error=reason)
        await self._notify(job_id, "escalated", reason)

    async def _notify(self, job_id: str, phase: str, detail: str) -> None:
        if self.notifier is None:
            return
        try:
            job = await self.store.get(job_id)
            if job is not None:
                await self.notifier.notify_job_event(job, phase, detail)
        except Exception as e:  # 通知失败绝不能影响修复主流程
            log.warning("notify failed for %s: %s", job_id, e)

    # -- 主流程 -----------------------------------------------------------

    async def __call__(self, job: Job) -> None:
        try:
            await self._run_chain(job)
        finally:
            await self._drain_event_tasks(job.id)

    async def _run_chain(self, job: Job) -> None:
        repo = self.config.repos.get(job.repo)
        if repo is None:
            await self._escalate(
                job.id, f"repository '{job.repo}' is not in the service.repos routing table"
            )
            return

        # --- triaging：信息不足就不下手（不产生垃圾 PR） ---
        if await self._transition(job.id, "triaging", reason="starting alert triage") is None:
            return
        ok, why = sop.has_actionable_context(job)
        if not ok:
            await self._escalate(job.id, f"triaging failed: {why}")
            return

        repo_root = repo.path
        if not Path(repo_root).is_dir():
            await self._escalate(job.id, f"repository path does not exist: {repo_root}")
            return

        # --- reproducing：建隔离工作区 + 基线测试 ---
        try:
            manager = self._worktree_manager(repo_root)
            worktree = await manager.create(job.id, base_branch=repo.base_branch or "HEAD")
        except Exception as e:
            await self._escalate(job.id, f"worktree creation failed: {type(e).__name__}: {e}")
            return
        work_dir = worktree.path
        await self._event(job.id, "worktree_ready", f"path={work_dir} branch={worktree.branch}")

        if await self._transition(job.id, "reproducing", reason=f"isolated worktree at {work_dir}") is None:
            return
        baseline = await self._run_tests(job, repo, work_dir, phase="baseline")

        # --- 修复 → 验证 → 发布 → CI 门禁 ---
        # 外层循环处理"CI 红了"：带着 CI 失败信息回到 fixing 重来，
        # 重试预算仍由状态机的 attempts 上限强制（超限由内层 escalate）。
        feedback = ""
        while True:
            context = await self._fix_and_verify(job, repo, worktree, baseline, feedback)
            if context is None:
                return

            published = await self._publish(job, context)
            if published is None:
                return

            status = await self._ci_gate(job, context, published)
            if status is None:
                return
            feedback = f"CI failed after the push: {status.details}"
            await self._event(job.id, "ci_retry", feedback)

    # -- 阶段实现 ---------------------------------------------------------

    async def _fix_and_verify(
        self,
        job: Job,
        repo: RepoConfig,
        worktree: Any,
        baseline: TestOutcome | None,
        feedback: str,
    ) -> ExecutionContext | None:
        """有界修复-验证循环。返回证据上下文；任何失败路径都返回 None（已收尾）。"""
        work_dir = worktree.path
        while True:
            kind = "retry" if feedback else "new"
            fixing = await self._transition(job.id, "fixing", reason=f"agent run ({kind})")
            if fixing is None:
                await self._escalate(
                    job.id,
                    "fix retry ceiling reached; escalating instead of looping",
                )
                return None
            attempt = fixing.attempts

            prompt = sop.build_alert_prompt(
                job,
                work_dir,
                test_command=repo.test_command,
                baseline=baseline.output if baseline else "",
                feedback=feedback,
                skills=self._skill_bodies(work_dir),
                mcp_servers=[
                    (cfg.name, cfg.description) for cfg in (self.config.mcp_servers or [])
                ],
                integration_command=repo.integration_test_command,
            )
            if attempt == 1:
                await self._event(job.id, "agent_prompt", prompt)
            await self._notify(job.id, "fixing", f"agent run attempt {attempt}")

            try:
                outcome = await self.runner.run(job, work_dir, prompt, self._make_on_event(job))
            except TokenBudgetExceeded as e:
                await self._escalate(job.id, f"cost guard: {e}")
                return None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._event(job.id, "agent_error", f"{type(e).__name__}: {e}")
                await self._escalate(job.id, f"agent run failed: {type(e).__name__}: {e}")
                return None

            await self.store.update(job.id, result=outcome.final_text[:MAX_EVIDENCE_CHARS])
            await self._event(
                job.id,
                "agent_finished",
                f"attempt={attempt} tool_calls={outcome.tool_calls} "
                f"tokens_in={outcome.input_tokens} tokens_out={outcome.output_tokens}",
            )

            # 变更检测：没有任何改动就没有可发布的修复
            changed_files, diff = await self._collect_diff(job.id, worktree)
            if not changed_files:
                await self._transition(
                    job.id, "cant_repro", reason="agent produced no code changes; nothing to publish"
                )
                return None

            if await self._transition(job.id, "verifying", reason="re-running tests after fix") is None:
                return None
            verify = await self._run_tests(job, repo, work_dir, phase="verify")
            if verify is not None and baseline is not None:
                await self._event(
                    job.id, "test_delta", f"before: {baseline.summary()} / after: {verify.summary()}"
                )

            # 集成验证（M2 W4）：单测过了（或未配置）才值得自起测试环境——
            # 单测都不过的修复没必要起依赖再跑一遍。verifier 自身异常按基础设施
            # 问题 escalate（不假装验证过，也不拿它烧 agent 的重试预算）。
            integration: IntegrationOutcome | None = None
            if (verify is None or verify.passed) and self.integration_verifier is not None:
                try:
                    integration = await self.integration_verifier.verify(job, repo, work_dir)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    await self._event(job.id, "integration_error", f"{type(e).__name__}: {e}")
                    await self._escalate(
                        job.id, f"integration verification raised: {type(e).__name__}: {e}"
                    )
                    return None
                await self._record_integration(job.id, integration)

            if integration is not None and integration.ran and not integration.passed:
                feedback = await self._verify_failed(
                    job,
                    attempt,
                    f"integration verification failed: {integration.summary()}",
                    integration.failure_feedback(),
                )
                if feedback is None:
                    return None
                continue

            if verify is None or verify.passed:
                return ExecutionContext(
                    repo=repo,
                    work_dir=work_dir,
                    prompt=prompt,
                    agent=outcome,
                    changed_files=changed_files,
                    diff=diff,
                    baseline_test=baseline,
                    verify_test=verify,
                    attempts=attempt,
                    integration=integration,
                )

            # 单测失败：记录后重试（是否允许重试由状态机的 attempts 上限决定）
            feedback = await self._verify_failed(
                job, attempt, f"verification failed: {verify.summary()}", verify.output or ""
            )
            if feedback is None:
                return None

    async def _verify_failed(
        self, job: Job, attempt: int, reason: str, output: str
    ) -> str | None:
        """验证失败（单测或集成）的统一收尾：记状态、判上限、准备重试反馈。

        返回下一轮的反馈文本；``None`` 表示已收尾（超限 escalate 或状态机拒绝），
        调用方必须停止循环。
        """
        failed = await self._transition(job.id, "verify_failed", reason=reason)
        if failed is None:
            await self._escalate(job.id, reason)
            return None
        if failed.attempts >= self.store.max_fix_attempts:
            await self._escalate(
                job.id,
                f"verification still failing after {failed.attempts} attempt(s): {reason}",
            )
            return None
        await self._event(
            job.id, "verification_retry", f"retrying fix with test output (attempt {attempt + 1})"
        )
        return (output or "")[-MAX_FEEDBACK_CHARS:]

    async def _record_integration(self, job_id: str, outcome: IntegrationOutcome | None) -> None:
        """把集成验证的每一步落进审计（PR body 的证据链）。"""
        if outcome is None:
            return
        if not outcome.ran:
            await self._event(job_id, "integration_skipped", outcome.skipped)
            return
        services = " ".join(line for line in outcome.services.strip().splitlines()[1:] if line)
        detail = f"project={outcome.project} file={outcome.compose_file} "
        detail += f"up={'ok' if outcome.up_ok else 'failed'}"
        if services:
            detail += f" services: {services[:300]}"
        if not outcome.up_ok:
            detail += "\n" + outcome.up_output.strip()[-1500:]
        await self._event(job_id, "integration_up", detail)
        if outcome.test is not None:
            test_detail = outcome.summary()
            if outcome.test.output:
                test_detail += "\n" + outcome.test.output[-2000:]
            await self._event(job_id, "integration_tests", test_detail)
        down = outcome.down_output.strip()
        if down.startswith("(docker compose down exit"):
            # 清理失败要如实说：容器/网络可能留在宿主上，需要人工看一眼
            detail = f"project {outcome.project}: cleanup issue — {down[-300:]}"
        else:
            detail = f"project {outcome.project} removed (volumes included)"
            if down:
                detail += f"; {down[-300:]}"
        await self._event(job_id, "integration_down", detail)

    async def _publish(self, job: Job, context: ExecutionContext) -> PublishResult | None:
        """发布并落 pr_opened；未配置/失败都不假装成功。"""
        if self.publisher is None:
            await self._escalate(
                job.id,
                "fix verified but no publisher is configured; "
                f"changes kept in worktree: {', '.join(context.changed_files[:10])}",
            )
            return None

        try:
            published = await self.publisher.publish(job, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._event(job.id, "publish_error", f"{type(e).__name__}: {e}")
            await self._escalate(job.id, f"publishing failed: {type(e).__name__}: {e}")
            return None

        for kind, detail in published.extra_events:
            await self._event(job.id, kind, detail)
        if await self._transition(
            job.id,
            "pr_opened",
            reason=f"PR opened for branch {published.branch}",
            pr_url=published.pr_url,
            branch=published.branch,
        ) is None:
            return None
        await self._notify(job.id, "pr_opened", published.pr_url)
        return published

    async def _ci_gate(
        self, job: Job, context: ExecutionContext, published: PublishResult
    ) -> Any | None:
        """等 CI 结论。返回失败状态表示"可重试"，None 表示已收尾。"""
        if self.ci_gate is None:
            return None

        if await self._transition(job.id, "ci_gate", reason="watching CI checks") is None:
            return None
        try:
            status = await self.ci_gate.wait(job, context, published)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._event(job.id, "ci_error", f"{type(e).__name__}: {e}")
            await self._escalate(job.id, f"CI polling failed: {type(e).__name__}: {e}")
            return None

        await self.store.update(job.id, ci_status=status.state)
        await self._event(job.id, "ci_status", f"{status.state}: {status.details}")

        if status.state in ("success", "none"):
            if await self._transition(
                job.id,
                "human_review",
                reason=f"CI {status.state}: {status.details}",
                ci_status=status.state,
            ) is None:
                return None
            await self._notify(job.id, "human_review", status.details)
            return None

        if status.is_failure:
            failed = await self._transition(
                job.id, "ci_failed", reason=f"CI failed: {status.details}", ci_status="failure"
            )
            if failed is None:
                await self._escalate(job.id, f"CI failed: {status.details}")
                return None
            return status

        # 超时未出结论：不假装成功，交给人
        await self._escalate(job.id, f"CI did not conclude: {status.details}")
        return None

    # -- 内部 -------------------------------------------------------------

    def _make_on_event(self, job: Job) -> Callable[[dict[str, Any]], None]:
        """agent 事件 → job 审计记录。

        回调是同步的（agent 内核契约），落库是异步的，因此这里把每条事件
        排成任务，并在 ``__call__`` 的 finally 里统一 drain——不能留下
        未完成的后台任务（仓库已知坑）。
        """
        tasks = self._event_tasks.setdefault(job.id, [])

        def _on_event(event: dict[str, Any]) -> None:
            etype = event.get("type")
            if etype == "tool_use":
                args = event.get("args") or {}
                hint = args.get("command") or args.get("file_path") or args.get("pattern") or ""
                detail = str(event.get("toolName", "?"))
                if hint:
                    detail += f": {str(hint)[:200]}"
            elif etype == "usage":
                usage = event.get("usage") or {}
                detail = f"in={usage.get('inputTokens', 0)} out={usage.get('outputTokens', 0)}"
            elif etype in ("mcp_ready", "mcp_error", "mcp_used", "teardown_cancellation"):
                # 内部工具链的关键节点与收尾异常：进审计（前者是 PR 证据，
                # 后者解释"这次运行有没有被收尾的取消打扰"）
                detail = str(event.get("detail", ""))[:500]
            else:
                return
            tasks.append(asyncio.create_task(self._event(job.id, f"agent_{etype}", detail)))

        return _on_event

    async def _drain_event_tasks(self, job_id: str) -> None:
        tasks = self._event_tasks.pop(job_id, [])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_tests(
        self, job: Job, repo: RepoConfig, work_dir: str, phase: str
    ) -> TestOutcome | None:
        if not repo.test_command:
            await self._event(job.id, f"{phase}_tests", "skipped: no test_command configured for repo")
            return None
        outcome = await self.test_runner.run(work_dir, repo.test_command, repo.test_timeout_seconds)
        detail = outcome.summary()
        if outcome.output:
            detail += "\n" + outcome.output[-2000:]
        await self._event(job.id, f"{phase}_tests", detail)
        return outcome

    async def _collect_diff(self, job_id: str, worktree: Any) -> tuple[list[str], str]:
        """收集 worktree 相对 HEAD 的改动（含未跟踪文件内容）。"""
        work_dir = worktree.path

        def _git(args: list[str]) -> tuple[int, str]:
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
            try:
                result = subprocess.run(
                    ["git"] + args,
                    cwd=work_dir,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    stdin=subprocess.DEVNULL,
                    env=env,
                )
            except (subprocess.SubprocessError, OSError) as e:
                return 1, f"(git failed: {e})"
            return result.returncode, result.stdout

        code, status = await asyncio.to_thread(_git, ["status", "--porcelain"])
        if code != 0:
            await self._event(job_id, "diff_error", status)
            return [], ""

        changed: list[str] = []
        for line in status.splitlines():
            path = line[3:].strip()
            if " -> " in path:  # rename: "old -> new"
                path = path.split(" -> ")[-1]
            if path and not is_ignored_path(path):
                changed.append(path)
        if not changed:
            return [], ""

        # 未跟踪文件先 add -N，否则 diff 里看不到它们的内容；服务自身状态与
        # 缓存目录同样排除（与提交用同一份 exclude）
        excludes = commit_excludes()
        await asyncio.to_thread(_git, ["add", "-N", "--", ".", *excludes])
        code, diff = await asyncio.to_thread(
            _git, ["diff", "--no-color", "--", ".", *excludes]
        )
        if code != 0:
            return changed, ""
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n… (diff truncated)"
        return changed, diff
