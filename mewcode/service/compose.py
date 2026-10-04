"""自起测试环境验证（M2 W4，精简版）：仓库带 compose 文件时，起依赖 → 跑集成测试 → 清理。

范围（2026-10-04 精简，见 docs/evolution/04-m2-enterprise-capability.md）：
① 探测仓库里的 compose 文件；② ``docker compose -p mewfix-<job-id> up -d --wait``
起依赖并等健康；③ 在沙箱容器内跑仓库的集成测试命令；④ 无论成败 ``down -v`` 清理。
失败与单测同样计入 ``verify_failed`` 重试预算（由执行链统一处理）。

**明确不做**：端口段分配器（靠 compose project name + 现有 worktree 隔离；
需要固定端口的仓库由 compose 文件自己用 ``${COMPOSE_PROJECT_NAME}`` 前缀解决）、
镜像预热、override / 多 compose 文件支持。

隔离语义：compose 服务由**宿主**的容器运行时拉起（服务层的编排动作）；集成
测试命令在**沙箱容器**内执行，并加入该 compose project 的默认网络——依赖
用服务名互相寻址（``redis:6379`` 这类 compose 惯例），与容器外的宿主端口
发布无关。容器运行时不可用（或无沙箱）时不做降级直跑，而是如实记录
``integration_skipped``：集成验证的前提就是容器运行时，没有它就无从谈起。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from mewcode.config import RepoConfig

from .execution import IntegrationOutcome, TestOutcome

log = logging.getLogger(__name__)

#: 仓库根目录下按优先级探测的 compose 文件名（compose v2 两者都认）
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yaml", "compose.yml")

#: compose up 的宿主侧硬超时：要覆盖首次拉镜像的时间（--wait-timeout 只约束健康等待）
COMPOSE_UP_TIMEOUT = 600
#: --wait-timeout：依赖服务被判定健康的上限
COMPOSE_WAIT_TIMEOUT = 180
COMPOSE_PS_TIMEOUT = 60
COMPOSE_DOWN_TIMEOUT = 120

#: 宿主命令执行器（argv, timeout, cwd）-> (exit_code, output)
CommandRunner = Callable[[list[str], float, "str | None"], Awaitable[tuple[int, str]]]


def find_compose_file(work_dir: str) -> str:
    """探测仓库根目录的 compose 文件；返回文件名（相对路径）或空串。"""
    root = Path(work_dir)
    for name in COMPOSE_FILES:
        if (root / name).is_file():
            return name
    return ""


def compose_project(job_id: str) -> str:
    """每 job 独立的 compose project name（并发 job 互不干扰的关键）。

    compose 只接受 ``[a-z0-9_-]``，job id 由服务生成（``job-<hex>``），
    这里仍做一次净化以防将来换 id 方案。
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in job_id.lower())
    return f"mewfix-{safe or 'job'}"


def compose_network(project: str) -> str:
    """compose 默认网络的固定名字：``<project>_default``。

    集成测试容器加入这个网络后，可以按服务名访问依赖——这正是 compose
    生态的寻址惯例，不需要发布任何宿主端口。
    """
    return f"{project}_default"


@dataclass
class _ComposeStep:
    exit_code: int
    output: str


class ComposeVerifier:
    """实现 ``IntegrationVerifier`` 契约（见 execution.py）。

    ``sandbox`` 提供 ``available()`` 与 ``run_command()``（DockerSandbox 或测试
    替身）；``runner`` 是宿主命令执行器，默认复用沙箱模块的 ``run_host_command``
    （带硬超时与进程树收尾）。
    """

    def __init__(
        self,
        sandbox: object | None,
        *,
        runtime_bin: str = "docker",
        runner: CommandRunner | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.runtime = runtime_bin
        if runner is None:
            from .sandbox import run_host_command  # 延迟导入：避免模块级循环

            runner = run_host_command
        self.runner = runner
        #: 取消路径上仍在跑的 down 任务（持引用防被 GC 回收）
        self._cleanup_tasks: set[asyncio.Task[tuple[int, str]]] = set()

    # -- IntegrationVerifier 契约 -----------------------------------------

    async def verify(
        self, job, repo: RepoConfig, work_dir: str
    ) -> IntegrationOutcome | None:
        compose_file = find_compose_file(work_dir)
        if not compose_file:
            return None
        project = compose_project(job.id)
        outcome = IntegrationOutcome(
            compose_file=compose_file, project=project, network=compose_network(project)
        )
        if not repo.integration_test_command:
            outcome.skipped = "no integration_test_command configured for this repository"
            return outcome
        if not await self._sandbox_ready():
            outcome.skipped = (
                "container runtime unavailable or sandbox disabled; "
                "integration verification requires it"
            )
            return outcome

        compose_path = str(Path(work_dir) / compose_file)
        try:
            up = await self._run(
                self._argv(compose_path, project, "up", "-d", "--wait",
                           "--wait-timeout", str(COMPOSE_WAIT_TIMEOUT)),
                COMPOSE_UP_TIMEOUT,
                work_dir,
            )
            outcome.up_ok = up.exit_code == 0
            outcome.up_output = up.output
            if outcome.up_ok:
                ps = await self._run(
                    self._argv(compose_path, project, "ps"), COMPOSE_PS_TIMEOUT, work_dir
                )
                outcome.services = ps.output
                outcome.test = await self._run_test(job, repo, work_dir, outcome.network)
        finally:
            # 无论成败（含超时与取消）都必须清理：容器 + 卷 + 网络
            outcome.down_output = await self._down(compose_path, project, work_dir)
        return outcome

    # -- 内部 -------------------------------------------------------------

    def _argv(self, compose_path: str, project: str, *args: str) -> list[str]:
        return [self.runtime, "compose", "-f", compose_path, "-p", project, *args]

    async def _run(self, argv: list[str], timeout: float, cwd: str) -> _ComposeStep:
        code, output = await self.runner(argv, timeout, cwd)
        return _ComposeStep(exit_code=code, output=output)

    async def _sandbox_ready(self) -> bool:
        if self.sandbox is None:
            return False
        try:
            return bool(await self.sandbox.available())  # type: ignore[attr-defined]
        except Exception as e:  # pragma: no cover - 探测异常按不可用处理
            log.warning("compose verifier: sandbox probe failed: %s", e)
            return False

    async def _run_test(
        self, job, repo: RepoConfig, work_dir: str, network: str
    ) -> TestOutcome:
        """在沙箱容器内跑集成测试（加入 compose 网络，按服务名寻址依赖）。"""
        command = repo.integration_test_command
        result = await self.sandbox.run_command(  # type: ignore[attr-defined]
            job.id,
            work_dir,
            command,
            repo_name=job.repo,
            timeout=repo.integration_timeout_seconds,
            network=network,
        )
        return TestOutcome(
            command=command,
            exit_code=result.exit_code,
            output=result.stdout,
            timed_out=result.timed_out,
        )

    async def _down(self, compose_path: str, project: str, work_dir: str) -> str:
        """``down -v`` 清理；取消也让它跑完（不能依赖进程退出兜底——仓库已知坑）。"""
        task = asyncio.ensure_future(
            self.runner(
                self._argv(compose_path, project, "down", "-v"), COMPOSE_DOWN_TIMEOUT, work_dir
            )
        )
        try:
            code, output = await asyncio.shield(task)
        except asyncio.CancelledError:
            self._cleanup_tasks.add(task)
            task.add_done_callback(self._cleanup_tasks.discard)
            raise
        if code != 0:
            return f"(docker compose down exit {code}) {output.strip()[-500:]}"
        return output

    async def drain_cleanup_tasks(self) -> None:
        """等服务停止时等待取消路径上的 down 收尾任务（供优雅退出调用）。"""
        pending = list(self._cleanup_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
