"""M2 W4: 自起测试环境验证（compose 精简版）测试。

覆盖四件事的语义与证据链：① 探测仓库 compose 文件；② ``up -d --wait`` 起依赖；
③ 在沙箱容器内（加入 compose 网络）跑集成测试；④ 无论成败 ``down -v`` 清理。
失败与单测同样计入 ``verify_failed`` 重试预算；无容器运行时如实记
``integration_skipped``，不假装验证过。

全部用假 compose 执行器与假沙箱，不依赖真实 Docker。
"""
from __future__ import annotations

import asyncio
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from mewcode.config import RepoConfig, ServiceConfig
from mewcode.service.compose import (
    COMPOSE_FILES,
    ComposeVerifier,
    compose_network,
    compose_project,
    find_compose_file,
)
from mewcode.service.execution import (
    AgentRunOutcome,
    ExecutionChain,
    IntegrationOutcome,
    TestRunner,
)
from mewcode.service.execution import TestOutcome as _TestOutcome
from mewcode.service.jobs import JobStore
from mewcode.service.publisher import build_pr_body

CALC_FIXED = "def add(a, b):\n    return a + b\n"
CALC_BUGGY = "def add(a, b):\n    return a - b\n"
#: 第一次"修错了"的改动：必须与初始内容不同（否则 worktree 无 diff -> cant_repro）
CALC_WRONG_FIX = "def add(a, b):\n    return a + b + 1\n"
TEST_SCRIPT = "import sys\nfrom calc import add\nsys.exit(0 if add(2, 3) == 5 else 1)\n"
COMPOSE_YML = "services:\n  cache:\n    image: redis:7-alpine\n"
INTEGRATION_CMD = "python test_integration.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=True)


@pytest.fixture
def compose_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "calc.py").write_text(CALC_BUGGY, encoding="utf-8")
    (repo / "test_calc.py").write_text(TEST_SCRIPT, encoding="utf-8")
    (repo / "docker-compose.yml").write_text(COMPOSE_YML, encoding="utf-8")
    (repo / "test_integration.py").write_text("# integration\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


# ---------------------------------------------------------------------------
# 探测与命名（纯函数）
# ---------------------------------------------------------------------------


class TestProbe:
    def test_finds_standard_names(self, tmp_path: Path):
        for name in COMPOSE_FILES:
            target = tmp_path / name
            target.write_text(COMPOSE_YML, encoding="utf-8")
            assert find_compose_file(str(tmp_path)) == name
            target.unlink()

    def test_missing_returns_empty(self, tmp_path: Path):
        assert find_compose_file(str(tmp_path)) == ""

    def test_project_name_is_per_job_and_compose_safe(self):
        project = compose_project("job-0A1b2C3d4E5f")
        assert project == "mewfix-job-0a1b2c3d4e5f"
        assert compose_network(project) == "mewfix-job-0a1b2c3d4e5f_default"
        # 异常字符被净化，不会破坏 compose 的命名约束
        assert compose_project("job/x y").startswith("mewfix-job-x-y")


# ---------------------------------------------------------------------------
# ComposeVerifier：探测 → 起依赖 → 跑测试 → 清理
# ---------------------------------------------------------------------------


class FakeSandboxResult:
    def __init__(self, exit_code: int = 0, stdout: str = "", timed_out: bool = False):
        self.exit_code = exit_code
        self.stdout = stdout
        self.timed_out = timed_out


class FakeSandbox:
    def __init__(self, available: bool = True, test_result: FakeSandboxResult | None = None):
        self._available = available
        self.test_result = test_result or FakeSandboxResult(stdout="integration ok")
        self.test_calls: list[dict] = []

    async def available(self) -> bool:
        return self._available

    async def run_command(self, job_id, work_dir, command, *, repo_name, timeout, network=None):
        self.test_calls.append(
            {
                "job_id": job_id,
                "work_dir": work_dir,
                "command": command,
                "repo_name": repo_name,
                "timeout": timeout,
                "network": network,
            }
        )
        return self.test_result


class FakeComposeRunner:
    """按子命令返回脚本化结果的假 compose 执行器（记录每次调用）。"""

    def __init__(self, up: tuple[int, str] = (0, "Container demo-cache-1  Started"),
                 ps: tuple[int, str] = (0, "NAME  IMAGE  STATUS\nx-cache-1  redis  Up (healthy)")):
        self.calls: list[dict] = []
        self.up = up
        self.ps = ps
        self.down: tuple[int, str] = (0, "")
        self.test_hook = None  # 可注入：测试执行期间触发的动作（如取消）

    async def __call__(self, argv: list[str], timeout: float, cwd: str | None):
        self.calls.append({"argv": list(argv), "timeout": timeout, "cwd": cwd})
        sub = argv[6] if len(argv) > 6 else ""
        if sub == "up":
            return self.up
        if sub == "ps":
            return self.ps
        if sub == "down":
            return self.down
        raise AssertionError(f"unexpected compose subcommand: {argv}")


class FakeJob:
    id = "job-abc123"
    repo = "demo"
    title = "boom"
    severity = "critical"
    fingerprint = "fp-1"
    payload: dict = {}


@pytest.fixture
def repo_config(compose_repo: Path) -> RepoConfig:
    return RepoConfig(
        name="demo",
        path=str(compose_repo),
        test_command='python test_calc.py',
        integration_test_command=INTEGRATION_CMD,
    )


class TestComposeVerifier:
    @pytest.mark.asyncio
    async def test_no_compose_file_returns_none(self, tmp_path: Path, repo_config: RepoConfig):
        verifier = ComposeVerifier(FakeSandbox(), runner=FakeComposeRunner())
        assert await verifier.verify(FakeJob(), repo_config, str(tmp_path)) is None

    @pytest.mark.asyncio
    async def test_unconfigured_command_is_skipped(
        self, compose_repo: Path, repo_config: RepoConfig
    ):
        repo_config.integration_test_command = ""
        runner = FakeComposeRunner()
        verifier = ComposeVerifier(FakeSandbox(), runner=runner)
        outcome = await verifier.verify(FakeJob(), repo_config, str(compose_repo))
        assert outcome is not None and not outcome.ran and not outcome.passed
        assert "integration_test_command" in outcome.skipped
        assert runner.calls == []  # 未配置就不该碰 docker

    @pytest.mark.asyncio
    async def test_sandbox_absent_or_unavailable_is_skipped(
        self, compose_repo: Path, repo_config: RepoConfig
    ):
        for sandbox in (None, FakeSandbox(available=False)):
            verifier = ComposeVerifier(sandbox, runner=FakeComposeRunner())
            outcome = await verifier.verify(FakeJob(), repo_config, str(compose_repo))
            assert outcome is not None and not outcome.ran
            assert "container runtime" in outcome.skipped

    @pytest.mark.asyncio
    async def test_happy_path_starts_waits_tests_and_cleans_up(
        self, compose_repo: Path, repo_config: RepoConfig
    ):
        sandbox, runner = FakeSandbox(), FakeComposeRunner()
        verifier = ComposeVerifier(sandbox, runner=runner)
        outcome = await verifier.verify(FakeJob(), repo_config, str(compose_repo))

        assert outcome is not None and outcome.ran and outcome.passed
        assert outcome.compose_file == "docker-compose.yml"
        assert outcome.project == "mewfix-job-abc123"

        # up：独立 project name + 等健康 + 项目文件显式指定；cwd 是 worktree
        up = runner.calls[0]
        assert up["argv"][1:3] == ["compose", "-f"]
        assert up["argv"][4:6] == ["-p", "mewfix-job-abc123"]
        assert up["argv"][6:10] == ["up", "-d", "--wait", "--wait-timeout"]
        assert up["cwd"] == str(compose_repo)
        assert Path(up["argv"][3]).name == "docker-compose.yml"
        assert {call["argv"][6] for call in runner.calls} == {"up", "ps", "down"}
        # ps：证据链里的依赖服务状态
        assert "cache" in outcome.services
        # 测试在沙箱容器里跑，且加入 compose 默认网络（按服务名寻址依赖）
        assert sandbox.test_calls == [
            {
                "job_id": FakeJob.id,
                "work_dir": str(compose_repo),
                "command": INTEGRATION_CMD,
                "repo_name": "demo",
                "timeout": repo_config.integration_timeout_seconds,
                "network": "mewfix-job-abc123_default",
            }
        ]
        # down -v：无论成败都清理，且带卷
        assert runner.calls[-1]["argv"][6:] == ["down", "-v"]
        assert outcome.down_output == ""

    @pytest.mark.asyncio
    async def test_up_failure_still_cleans_up_and_skips_test(
        self, compose_repo: Path, repo_config: RepoConfig
    ):
        sandbox = FakeSandbox()
        runner = FakeComposeRunner(up=(1, "Error: service 'cache' failed to start"))
        verifier = ComposeVerifier(sandbox, runner=runner)
        outcome = await verifier.verify(FakeJob(), repo_config, str(compose_repo))

        assert outcome is not None and outcome.ran and not outcome.passed
        assert not outcome.up_ok and outcome.test is None
        assert "failed to start" in outcome.up_output
        assert sandbox.test_calls == []  # 依赖没起来就别跑测试
        assert runner.calls[-1]["argv"][6:] == ["down", "-v"]
        assert "failed to start" in outcome.failure_feedback()
        assert "docker compose up failed" in outcome.summary()

    @pytest.mark.asyncio
    async def test_test_failure_is_reported_with_output(
        self, compose_repo: Path, repo_config: RepoConfig
    ):
        sandbox = FakeSandbox(
            test_result=FakeSandboxResult(exit_code=1, stdout="AssertionError: redis unreachable")
        )
        verifier = ComposeVerifier(sandbox, runner=FakeComposeRunner())
        outcome = await verifier.verify(FakeJob(), repo_config, str(compose_repo))

        assert outcome is not None and outcome.ran and not outcome.passed
        assert outcome.up_ok and outcome.test is not None
        assert outcome.test.exit_code == 1
        assert "redis unreachable" in outcome.failure_feedback()
        assert "exit 1" in outcome.summary()

    @pytest.mark.asyncio
    async def test_test_timeout_marks_failure(
        self, compose_repo: Path, repo_config: RepoConfig
    ):
        sandbox = FakeSandbox(test_result=FakeSandboxResult(exit_code=124, timed_out=True))
        verifier = ComposeVerifier(sandbox, runner=FakeComposeRunner())
        outcome = await verifier.verify(FakeJob(), repo_config, str(compose_repo))
        assert outcome is not None and not outcome.passed
        assert outcome.summary().endswith("TIMEOUT")

    @pytest.mark.asyncio
    async def test_down_failure_is_recorded_but_does_not_fail_the_fix(
        self, compose_repo: Path, repo_config: RepoConfig
    ):
        runner = FakeComposeRunner()
        runner.down = (1, "network mewfix-job-abc123_default has active endpoints")
        verifier = ComposeVerifier(FakeSandbox(), runner=runner)
        outcome = await verifier.verify(FakeJob(), repo_config, str(compose_repo))
        assert outcome is not None and outcome.passed  # 清理问题不否认"修复已验证"
        assert "active endpoints" in outcome.down_output

    @pytest.mark.asyncio
    async def test_cancellation_still_runs_down(self, compose_repo: Path, repo_config: RepoConfig):
        """取消（优雅退出）也必须清理依赖环境——不能依赖进程退出兜底。"""
        runner = FakeComposeRunner()
        started = asyncio.Event()

        async def cancelling_command(*args, **kwargs):
            started.set()
            raise asyncio.CancelledError()

        sandbox = FakeSandbox()
        sandbox.run_command = cancelling_command  # type: ignore[method-assign]
        verifier = ComposeVerifier(sandbox, runner=runner)

        with pytest.raises(asyncio.CancelledError):
            await verifier.verify(FakeJob(), repo_config, str(compose_repo))

        assert started.is_set()
        assert runner.calls[-1]["argv"][6:] == ["down", "-v"]
        await verifier.drain_cleanup_tasks()

    @pytest.mark.asyncio
    async def test_cancel_during_down_lets_cleanup_finish(
        self, compose_repo: Path, repo_config: RepoConfig
    ):
        """down 进行中被取消：shield 让清理继续跑完（引用留在 drain 里）。"""
        down_entered = asyncio.Event()
        down_release = asyncio.Event()
        down_done = asyncio.Event()

        class SlowDownRunner(FakeComposeRunner):
            async def __call__(self, argv, timeout, cwd):
                if len(argv) > 6 and argv[6] == "down":
                    down_entered.set()
                    await down_release.wait()
                    self.calls.append({"argv": list(argv), "timeout": timeout, "cwd": cwd})
                    down_done.set()
                    return (0, "")
                return await super().__call__(argv, timeout, cwd)

        verifier = ComposeVerifier(FakeSandbox(), runner=SlowDownRunner())

        async def run_verify():
            await verifier.verify(FakeJob(), repo_config, str(compose_repo))

        task = asyncio.create_task(run_verify())
        await down_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 取消没有打断 down：它在后台继续，drain 能等到它跑完
        down_release.set()
        await verifier.drain_cleanup_tasks()
        assert down_done.is_set()


# ---------------------------------------------------------------------------
# 执行链接线：失败计入 verify_failed，证据进 PR body
# ---------------------------------------------------------------------------


def _test_command() -> str:
    import sys

    return f'"{sys.executable}" test_calc.py'


class ChainRunner:
    """按脚本改文件（第一次修错、第二次修对），可注入集成失败。"""

    def __init__(self, scripts: list[str] | None = None) -> None:
        self.scripts = scripts if scripts is not None else [CALC_FIXED]
        self.calls: list[str] = []

    async def run(self, job, work_dir, prompt, on_event):
        index = min(len(self.calls), len(self.scripts) - 1)
        self.calls.append(prompt)
        content = self.scripts[index]
        if content:
            (Path(work_dir) / "calc.py").write_text(content, encoding="utf-8")
        return AgentRunOutcome(
            final_text="ROOT CAUSE: sign error\nFIX: corrected add()\nVERIFICATION: ran tests",
            tool_calls=1,
            input_tokens=10,
            output_tokens=10,
        )


class ScriptedVerifier:
    """按序列返回集成结果；记录每次调用的 job 与 work_dir。"""

    def __init__(self, outcomes: list[IntegrationOutcome | None]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    async def verify(self, job, repo, work_dir):
        self.calls.append(str(work_dir))
        if not self.outcomes:
            return None
        return self.outcomes.pop(0)


def _outcome(
    *, up_ok: bool = True, test_exit: int = 0, project: str = "mewfix-job-x",
    skipped: str = "", output: str = "redis OK",
) -> IntegrationOutcome:
    outcome = IntegrationOutcome(
        compose_file="docker-compose.yml", project=project,
        network=f"{project}_default", up_ok=up_ok, up_output="up output",
        services="NAME STATUS\nx-cache-1 Up (healthy)", skipped=skipped,
    )
    if up_ok and not skipped:
        outcome.test = _TestOutcome(command=INTEGRATION_CMD, exit_code=test_exit, output=output)
    return outcome


class BoomVerifier:
    async def verify(self, job, repo, work_dir):
        raise RuntimeError("docker CLI vanished")


@asynccontextmanager
async def chain_env(
    tmp_path: Path,
    repo: Path,
    *,
    verifier=None,
    scripts: list[str] | None = None,
    publisher=None,
):
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    service = ServiceConfig(
        data_dir=str(tmp_path / "state"),
        repos={
            "demo": RepoConfig(
                name="demo",
                path=str(repo),
                test_command=_test_command(),
                integration_test_command=INTEGRATION_CMD,
            )
        },
    )
    from tests.test_service_execution import FakePublisher  # 复用 M1 的假 publisher

    chain = ExecutionChain(
        service,
        store,
        ChainRunner(scripts),
        publisher=publisher if publisher is not None else FakePublisher(),
        test_runner=TestRunner(),
        integration_verifier=verifier,
    )
    try:
        yield store, chain, service
    finally:
        await store.close()


async def make_job(store: JobStore, **overrides):
    payload = {"source": "manual", "summary": "add(2,3) wrong", "logs": "AssertionError"}
    payload.update(overrides)
    return await store.create_job(
        fingerprint="fp-w4", repo="demo", severity="critical", title="wrong arithmetic",
        payload=payload,
    )


async def _events(store: JobStore, job_id: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for event in await store.events(job_id):
        result.setdefault(event.kind, []).append(event.detail)
    return result


class TestChainIntegration:
    @pytest.mark.asyncio
    async def test_passing_integration_reaches_pr_with_evidence(
        self, tmp_path: Path, compose_repo: Path
    ):
        verifier = ScriptedVerifier([_outcome()])
        async with chain_env(tmp_path, compose_repo, verifier=verifier) as (store, chain, service):
            job = await make_job(store)
            await chain(job)
            updated = await store.get(job.id)
            events = await _events(store, job.id)

            assert updated is not None and updated.status == "pr_opened"
            assert any("up=ok" in d for d in events["integration_up"])
            assert "integration_tests" in events and "integration_down" in events
            # 集成证据进了 PR body（publisher 拿到的 ExecutionContext 里带着它）
            contexts = chain.publisher.contexts
            assert contexts and contexts[0].integration is not None
            assert contexts[0].integration.passed
            body = build_pr_body(job, contexts[0], [])
            assert "集成验证（自起测试环境）" in body
            assert INTEGRATION_CMD in body and "✅ 通过" in body

    @pytest.mark.asyncio
    async def test_integration_failure_retries_with_output_as_feedback(
        self, tmp_path: Path, compose_repo: Path
    ):
        verifier = ScriptedVerifier([
            _outcome(test_exit=1, output="AssertionError: cache not reachable"),
            _outcome(test_exit=0),
        ])
        async with chain_env(
            tmp_path, compose_repo, verifier=verifier, scripts=[CALC_FIXED, CALC_FIXED]
        ) as (store, chain, service):
            job = await make_job(store)
            await chain(job)
            updated = await store.get(job.id)
            events = await _events(store, job.id)

            assert updated is not None and updated.status == "pr_opened" and updated.attempts == 2
            assert len(verifier.calls) == 2  # 两次修复各验证一次
            # 第二次 prompt 带着集成失败的原始输出
            second_prompt = chain.runner.calls[1]
            assert "cache not reachable" in second_prompt
            assert "Previous attempt failed verification" in second_prompt
            # 集成失败 → verify_failed 状态 + 重试事件（状态推进记在 transition 事件里）
            assert "integration_tests" in events and "verification_retry" in events
            assert any("verifying -> verify_failed" in d for d in events["transition"])

    @pytest.mark.asyncio
    async def test_integration_failure_burns_retry_budget_then_escalates(
        self, tmp_path: Path, compose_repo: Path
    ):
        verifier = ScriptedVerifier([_outcome(test_exit=1) for _ in range(3)])
        async with chain_env(
            tmp_path, compose_repo, verifier=verifier, scripts=[CALC_FIXED]
        ) as (store, chain, service):
            job = await make_job(store)
            await chain(job)
            updated = await store.get(job.id)

            assert updated is not None and updated.status == "escalate"
            assert "integration verification" in (updated.last_error or "")
            assert len(chain.publisher.contexts) == 0  # 没验证通过就没 PR

    @pytest.mark.asyncio
    async def test_verifier_exception_escalates_with_event(
        self, tmp_path: Path, compose_repo: Path
    ):
        async with chain_env(tmp_path, compose_repo, verifier=BoomVerifier()) as (store, chain, service):
            job = await make_job(store)
            await chain(job)
            updated = await store.get(job.id)
            events = await _events(store, job.id)

            assert updated is not None and updated.status == "escalate"
            assert any("docker CLI vanished" in d for d in events["integration_error"])

    @pytest.mark.asyncio
    async def test_skipped_integration_still_publishes_single_test_evidence(
        self, tmp_path: Path, compose_repo: Path
    ):
        verifier = ScriptedVerifier([_outcome(skipped="container runtime unavailable")])
        async with chain_env(tmp_path, compose_repo, verifier=verifier) as (store, chain, service):
            job = await make_job(store)
            await chain(job)
            updated = await store.get(job.id)
            events = await _events(store, job.id)

            assert updated is not None and updated.status == "pr_opened"
            assert any("container runtime" in d for d in events["integration_skipped"])
            contexts = chain.publisher.contexts
            body = build_pr_body(job, contexts[0], [])
            assert "⏭️ 未执行" in body and "container runtime" in body

    @pytest.mark.asyncio
    async def test_unit_failure_skips_integration_entirely(
        self, tmp_path: Path, compose_repo: Path
    ):
        """单测都不过时不浪费一次 compose 环境：集成验证不执行，反馈是单测输出。"""
        verifier = ScriptedVerifier([_outcome()])
        async with chain_env(
            tmp_path, compose_repo, verifier=verifier, scripts=[CALC_WRONG_FIX, CALC_FIXED]
        ) as (store, chain, service):
            job = await make_job(store)
            await chain(job)
            updated = await store.get(job.id)

            assert updated is not None and updated.status == "pr_opened"
            assert len(verifier.calls) == 1  # 只在最后一次成功的验证里跑过

    @pytest.mark.asyncio
    async def test_no_compose_file_means_no_integration_section(
        self, tmp_path: Path, compose_repo: Path
    ):
        (compose_repo / "docker-compose.yml").unlink()
        async with chain_env(tmp_path, compose_repo, verifier=ScriptedVerifier([None])) as (
            store,
            chain,
            service,
        ):
            job = await make_job(store)
            await chain(job)
            updated = await store.get(job.id)

            assert updated is not None and updated.status == "pr_opened"
            body = build_pr_body(job, chain.publisher.contexts[0], [])
            assert "集成验证" not in body


# ---------------------------------------------------------------------------
# PR body 小节渲染
# ---------------------------------------------------------------------------


class TestIntegrationSectionRendering:
    def _body(self, integration) -> str:
        context = _context(integration)
        return build_pr_body(FakeJob(), context, [])

    def test_up_failure_rendered_with_output(self):
        outcome = _outcome(up_ok=False)
        outcome.up_output = "Error: port is already allocated"
        body = self._body(outcome)
        assert "❌ 启动失败" in body and "port is already allocated" in body

    def test_test_failure_rendered(self):
        body = self._body(_outcome(test_exit=1, output="boom detail"))
        assert "❌ 失败（exit 1）" in body and "boom detail" in body

    def test_down_output_rendered_when_present(self):
        outcome = _outcome()
        outcome.down_output = "(docker compose down exit 1) network has active endpoints"
        body = self._body(outcome)
        assert "环境清理" in body and "active endpoints" in body

    def test_none_produces_no_section(self):
        assert "集成验证" not in self._body(None)


def _context(integration):
    from mewcode.service.execution import ExecutionContext

    return ExecutionContext(
        repo=RepoConfig(name="demo", path=".", test_command="pytest"),
        work_dir=".",
        prompt="p",
        agent=AgentRunOutcome(final_text="ROOT CAUSE: x\nFIX: y"),
        changed_files=["calc.py"],
        diff="+1",
        baseline_test=_TestOutcome(command="t", exit_code=1, output="before"),
        verify_test=_TestOutcome(command="t", exit_code=0, output="after"),
        integration=integration,
    )
