"""Run configured verification commands in their existing isolation boundary."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from mewcode.processes import create_shell_process, kill_process_tree, release_process

from .pytest_scope import PytestScope, pytest_probe
from .sandbox import SandboxUnavailable

log = logging.getLogger(__name__)


@dataclass
class TestOutcome:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False
    pytest_scope: PytestScope | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        if self.timed_out:
            return f"`{self.command}` → TIMEOUT"
        return f"`{self.command}` → exit {self.exit_code}"


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
        with pytest_probe(work_dir, command) as probe:
            result = await self._run(work_dir, command, timeout, probe.host_env(env) if probe else env)
            if probe is not None:
                result.output, result.pytest_scope = probe.read(result.output, result.exit_code)
            return result

    async def _run(self, work_dir: str, command: str, timeout: float, env: dict[str, str]) -> TestOutcome:
        proc = await create_shell_process(
            command,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = (stdout or b"").decode("utf-8", errors="replace")
            return TestOutcome(command=command, exit_code=proc.returncode or 0, output=output)
        except TimeoutError:
            await kill_process_tree(proc)
            return TestOutcome(
                command=command,
                exit_code=-1,
                output=f"(timed out after {timeout:.0f}s)",
                timed_out=True,
            )
        except asyncio.CancelledError:
            await kill_process_tree(proc)
            raise
        finally:
            release_process(proc)


class SandboxTestRunner:
    """在容器里跑仓库测试命令（M2：验证阶段也隔离——它执行的是仓库代码）。

    容器不可用时默认拒绝执行；只有显式允许时才能回退宿主。
    """

    def __init__(
        self, sandbox: Any, *, repo_name: str = "job", fallback: TestRunner | None = None,
        allow_host_fallback: bool = False,
    ) -> None:
        self.sandbox = sandbox
        self.repo_name = repo_name
        self.fallback = fallback or TestRunner()
        self.allow_host_fallback = allow_host_fallback

    async def run(
        self, work_dir: str, command: str, timeout: float, *, job_id: str = "", repo_name: str = ""
    ) -> TestOutcome:
        if not command:
            raise ValueError("empty test command")
        try:
            available = await self.sandbox.available()
        except Exception as e:  # pragma: no cover - 探测异常按不可用处理
            log.warning("sandbox probe failed: %s", e)
            available = False
        if not available:
            if not self.allow_host_fallback:
                raise SandboxUnavailable("sandbox runtime unavailable; refusing host test execution")
            log.warning("sandbox unavailable; running `%s` on the host", command)
            outcome = await self.fallback.run(work_dir, command, timeout)
            outcome.output = "(host fallback explicitly enabled; no OS sandbox)\n" + outcome.output
            return outcome

        with pytest_probe(work_dir, command) as probe:
            result = await self.sandbox.run_command(
                job_id or f"test-{uuid.uuid4().hex}", work_dir, probe.shell_command(command) if probe else command,
                repo_name=repo_name or self.repo_name, timeout=timeout,
            )
            outcome = TestOutcome(command, result.exit_code, result.stdout, result.timed_out)
            if probe is not None:
                outcome.output, outcome.pytest_scope = probe.read(outcome.output, outcome.exit_code)
            return outcome
