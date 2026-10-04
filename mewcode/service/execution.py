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
class AgentRunOutcome:
    final_text: str = ""
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

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


# ---------------------------------------------------------------------------
# 真实 agent 运行器
# ---------------------------------------------------------------------------


class HeadlessAgentRunner:
    """构造并运行 agent 内核（服务模式的权限语义在这里落地）。

    权限语义（架构文档决策 4）：
    - ``PermissionMode.DONT_ASK``：无人值守下没有"询问"的语义，ask 一律视为允许；
    - 沙箱根 = worktree 路径：越界读写被拒；
    - 危险命令检测器照常生效（deny 优先于白名单）；
    - 仓库自己的 ``.mewcode/permissions.yaml`` 作为 per-repo 策略文件参与判定。
    """

    def __init__(self, config: ServiceConfig, provider: Any, hook_engine: Any = None) -> None:
        self.config = config
        self.provider = provider
        self.hook_engine = hook_engine

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
            registry=create_default_registry(),
            protocol=self.provider.protocol,
            work_dir=work_dir,
            permission_checker=checker,
            context_window=self.provider.get_context_window(),
            instructions_content=load_instructions(work_dir),
            hook_engine=self.hook_engine,
        )

    async def run(
        self, job: Job, work_dir: str, prompt: str, on_event: Callable[[dict[str, Any]], None]
    ) -> AgentRunOutcome:
        from mewcode.conversation import ConversationManager

        agent = self._build_agent(work_dir)
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

        try:
            outcome.final_text = await agent.run_to_completion(
                prompt, ConversationManager(), event_callback=_callback
            )
        finally:
            # 显式收尾：agent 可能留下后台任务（子代理 / fire-and-forget）
            await agent.cancel_background_tasks()
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
        notifier: Any = None,
        worktree_manager_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.runner = runner
        self.test_runner = test_runner or TestRunner()
        self.publisher = publisher
        self.ci_gate = ci_gate
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
                )

            # 验证失败：记录后重试（是否允许重试由状态机的 attempts 上限决定）
            failed = await self._transition(
                job.id, "verify_failed", reason=f"verification failed: {verify.summary()}"
            )
            if failed is None:
                await self._escalate(job.id, f"verification failed: {verify.summary()}")
                return None
            if failed.attempts >= self.store.max_fix_attempts:
                await self._escalate(
                    job.id,
                    f"verification still failing after {failed.attempts} attempt(s): {verify.summary()}",
                )
                return None

            feedback = (verify.output or "")[-MAX_FEEDBACK_CHARS:]
            await self._event(job.id, "verification_retry", f"retrying fix with test output (attempt {attempt + 1})")

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
            if path:
                changed.append(path)
        if not changed:
            return [], ""

        # 未跟踪文件先 add -N，否则 diff 里看不到它们的内容
        await asyncio.to_thread(_git, ["add", "-N", "."])
        code, diff = await asyncio.to_thread(_git, ["diff", "--no-color", "--", "."])
        if code != 0:
            return changed, ""
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n… (diff truncated)"
        return changed, diff
