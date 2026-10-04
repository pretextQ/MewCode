"""M1 W3: 执行链测试。

用真实 git worktree + 假 agent 运行器（不外呼 LLM）覆盖：
信息不足的告警在 triaging 就 escalate（不产生垃圾 PR）、无改动 -> cant_repro、
验证失败的有界重试、token 预算熔断、publish 缺失/失败时不假装成功，
以及每个阶段是否落进审计记录。
"""
from __future__ import annotations

import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from mewcode.config import RepoConfig, ServiceConfig
from mewcode.service import sop
from mewcode.service.execution import (
    MAX_FEEDBACK_CHARS,
    AgentRunOutcome,
    ExecutionChain,
    HeadlessAgentRunner,
    PublishResult,
    TestRunner,
    TokenBudgetExceeded,
)
from mewcode.service.jobs import JobStore

CALC_FIXED = "def add(a, b):\n    return a + b\n"
CALC_BUGGY = "def add(a, b):\n    return a - b\n"
TEST_SCRIPT = (
    "import sys\n"
    "from calc import add\n"
    "sys.exit(0 if add(2, 3) == 5 else 1)\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=True)


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "calc.py").write_text(CALC_BUGGY, encoding="utf-8")
    (repo / "test_calc.py").write_text(TEST_SCRIPT, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _test_command() -> str:
    return f'"{sys.executable}" test_calc.py'


class FakeRunner:
    """按脚本"修 bug"的假 runner：通过改写 worktree 里的文件产生真实 diff。"""

    def __init__(self, scripts: list[str] | None = None) -> None:
        # 每个元素是一次运行的"动作"："" 表示不改动，否则写入 calc.py 的内容
        self.scripts = scripts if scripts is not None else [CALC_FIXED]
        self.calls: list[dict] = []

    async def run(self, job, work_dir, prompt, on_event):
        index = min(len(self.calls), len(self.scripts) - 1)
        self.calls.append({"work_dir": work_dir, "prompt": prompt})
        on_event({"type": "tool_use", "toolName": "Edit", "args": {"file_path": "calc.py"}})
        on_event({"type": "usage", "usage": {"inputTokens": 120, "outputTokens": 80}})
        content = self.scripts[index]
        if content:
            (Path(work_dir) / "calc.py").write_text(content, encoding="utf-8")
        return AgentRunOutcome(
            final_text="ROOT CAUSE: sign error\nFIX: corrected add()\nVERIFICATION: ran tests",
            tool_calls=1,
            input_tokens=120,
            output_tokens=80,
        )


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.contexts: list = []

    async def publish(self, job, context) -> PublishResult:
        self.contexts.append(context)
        if self.fail:
            raise RuntimeError("push rejected: no credentials")
        return PublishResult(
            pr_url="https://github.com/acme/demo/pull/7",
            branch=f"mewfix/{job.id}",
            extra_events=[("pushed", "origin mewfix/x")],
        )


@asynccontextmanager
async def chain_env(tmp_path: Path, repo: Path, *, scripts=None, publisher=None, config: ServiceConfig | None = None,
                    test_command_override: str | None = None):
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    service = config or ServiceConfig(
        data_dir=str(tmp_path / "state"),
        repos={
            "demo": RepoConfig(
                name="demo",
                path=str(repo),
                test_command=_test_command() if test_command_override is None else test_command_override,
            )
        },
    )
    runner = FakeRunner(scripts)
    chain = ExecutionChain(
        service,
        store,
        runner,
        publisher=publisher,
        test_runner=TestRunner(),
    )
    try:
        yield store, chain, runner, service
    finally:
        await store.close()


async def make_job(store: JobStore, **payload_overrides):
    payload = {
        "source": "manual",
        "summary": "add(2,3) returns -1",
        "logs": "AssertionError: expected 5, got -1",
    }
    payload.update(payload_overrides)
    return await store.create_job(
        fingerprint="fp-1", repo="demo", severity="critical",
        title="wrong arithmetic", payload=payload,
    )


# =========================================================================
# A. 正常路径：修复 → 验证 → 发布
# =========================================================================

class TestHappyPath:
    @pytest.mark.asyncio
    async def test_full_chain_reaches_pr_opened(self, tmp_path: Path, demo_repo: Path):
        publisher = FakePublisher()
        async with chain_env(tmp_path, demo_repo, publisher=publisher) as (store, chain, runner, _):
            job = await make_job(store)
            await chain(job)

            final = await store.get_or_raise(job.id)
            assert final.status == "pr_opened"
            assert final.pr_url == "https://github.com/acme/demo/pull/7"
            assert final.branch == f"mewfix/{job.id}"

            # 证据链完整交给 publisher
            assert len(publisher.contexts) == 1
            ctx = publisher.contexts[0]
            assert ctx.changed_files == ["calc.py"]
            assert "+def add" in ctx.diff or "return a + b" in ctx.diff
            assert ctx.baseline_test is not None and not ctx.baseline_test.passed
            assert ctx.verify_test is not None and ctx.verify_test.passed
            assert "ROOT CAUSE" in ctx.agent.final_text
            assert ctx.diff_stat().startswith("1 file(s)")

    @pytest.mark.asyncio
    async def test_events_record_every_phase(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher()) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            kinds = {e.kind for e in await store.events(job.id)}
            for expected in (
                "worktree_ready", "baseline_tests", "agent_prompt", "agent_tool_use",
                "agent_usage", "agent_finished", "verify_tests", "test_delta", "pushed",
            ):
                assert expected in kinds, f"missing audit event: {expected}"

    @pytest.mark.asyncio
    async def test_agent_runs_in_isolated_worktree(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher()) as (store, chain, runner, _):
            job = await make_job(store)
            await chain(job)
            work_dir = Path(runner.calls[0]["work_dir"])
            assert work_dir != demo_repo
            assert str(work_dir).startswith(str(demo_repo / ".mewcode" / "worktrees"))
            # 主仓库不被改动：修复只发生在 worktree 里
            assert (demo_repo / "calc.py").read_text(encoding="utf-8") == CALC_BUGGY

    @pytest.mark.asyncio
    async def test_prompt_carries_alert_context_and_rules(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher()) as (store, chain, runner, _):
            job = await make_job(store)
            await chain(job)
            prompt = runner.calls[0]["prompt"]
            assert "wrong arithmetic" in prompt
            assert "AssertionError" in prompt          # 证据
            assert "git push" in prompt                # 明确禁止发布动作
            assert "test_calc.py" in prompt            # 仓库测试命令
            assert "ROOT CAUSE" in prompt              # 报告格式

    @pytest.mark.asyncio
    async def test_result_text_stored_on_job(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher()) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            assert "ROOT CAUSE" in (await store.get_or_raise(job.id)).result

    @pytest.mark.asyncio
    async def test_untracked_new_file_is_published(self, tmp_path: Path, demo_repo: Path):
        """agent 新建文件也算改动（add -N 后 diff 可见）。"""
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher()) as (store, chain, _, _):
            job = await make_job(store)
            published: list = []

            class NewFilePublisher(FakePublisher):
                async def publish(self, j, context) -> PublishResult:
                    published.append(context)
                    return PublishResult(pr_url="https://x/pr/1", branch=f"mewfix/{j.id}")

            class NewFileRunner(FakeRunner):
                async def run(self, j, work_dir, prompt, on_event):
                    self.calls.append({"work_dir": work_dir, "prompt": prompt})
                    (Path(work_dir) / "calc.py").write_text(CALC_FIXED, encoding="utf-8")
                    (Path(work_dir) / "REGRESSION.md").write_text(
                        "This bug must not come back.\n", encoding="utf-8"
                    )
                    return AgentRunOutcome(final_text="FIX: sign error + regression note")

            chain.runner = NewFileRunner([])
            chain.publisher = NewFilePublisher()
            await chain(job)

            assert (await store.get_or_raise(job.id)).status == "pr_opened"
            assert sorted(published[0].changed_files) == ["REGRESSION.md", "calc.py"]
            assert "REGRESSION.md" in published[0].diff


# =========================================================================
# B. triaging 判据：信息不足不下手
# =========================================================================

class TestTriaging:
    @pytest.mark.asyncio
    async def test_empty_payload_escalates_without_worktree(self, tmp_path: Path, demo_repo: Path):
        publisher = FakePublisher()
        async with chain_env(tmp_path, demo_repo, publisher=publisher) as (store, chain, runner, _):
            job = await store.create_job(fingerprint="fp-empty", repo="demo", payload={})
            await chain(job)

            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "triaging failed" in final.last_error
            assert runner.calls == []          # 没有跑 agent
            assert publisher.contexts == []    # 没有垃圾 PR
            assert not (demo_repo / ".mewcode" / "worktrees").exists()

    @pytest.mark.asyncio
    async def test_contextless_alert_escalates(self, tmp_path: Path, demo_repo: Path):
        """有 repo 但没有任何可定位信息（无 logs/标题/labels）→ escalate。"""
        async with chain_env(tmp_path, demo_repo) as (store, chain, runner, _):
            job = await store.create_job(
                fingerprint="fp-thin", repo="demo", title="", payload={"source": "manual"}
            )
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "insufficient alert context" in final.last_error
            assert runner.calls == []

    @pytest.mark.asyncio
    async def test_title_alone_is_enough(self, tmp_path: Path, demo_repo: Path):
        """只有标题也允许下手——标题本身能定位问题时不该误判为信息不足。"""
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher()) as (store, chain, _, _):
            job = await store.create_job(
                fingerprint="fp-t", repo="demo", title="add() returns wrong result", payload={"source": "manual"}
            )
            await chain(job)
            assert (await store.get_or_raise(job.id)).status == "pr_opened"

    @pytest.mark.asyncio
    async def test_unknown_repo_escalates(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo) as (store, chain, _, _):
            job = await store.create_job(fingerprint="fp-x", repo="ghost", payload={"summary": "x"})
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "routing table" in final.last_error

    @pytest.mark.asyncio
    async def test_missing_repo_path_escalates(self, tmp_path: Path, demo_repo: Path):
        config = ServiceConfig(
            data_dir=str(tmp_path / "state"),
            repos={"demo": RepoConfig(name="demo", path=str(tmp_path / "nonexistent"))},
        )
        async with chain_env(tmp_path, demo_repo, config=config) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "does not exist" in final.last_error


# =========================================================================
# C. 失败与收敛
# =========================================================================

class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_no_changes_means_cant_repro(self, tmp_path: Path, demo_repo: Path):
        publisher = FakePublisher()
        async with chain_env(tmp_path, demo_repo, scripts=[""], publisher=publisher) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "cant_repro"
            assert publisher.contexts == []

    @pytest.mark.asyncio
    async def test_no_publisher_escalates_not_fake_success(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo, publisher=None) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "no publisher" in final.last_error

    @pytest.mark.asyncio
    async def test_publisher_failure_escalates(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher(fail=True)) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "publishing failed" in final.last_error

    @pytest.mark.asyncio
    async def test_agent_exception_escalates(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo) as (store, chain, _, _):
            class BoomRunner:
                async def run(self, job, work_dir, prompt, on_event):
                    raise RuntimeError("LLM endpoint unreachable")

            chain.runner = BoomRunner()
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "LLM endpoint unreachable" in final.last_error

    @pytest.mark.asyncio
    async def test_token_budget_breaker_escalates(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo) as (store, chain, _, _):
            class BudgetRunner:
                async def run(self, job, work_dir, prompt, on_event):
                    raise TokenBudgetExceeded("token budget 1000 exceeded (900 in + 200 out)")

            chain.runner = BudgetRunner()
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "cost guard" in final.last_error

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, tmp_path: Path, demo_repo: Path):
        """worker 取消（服务退出）必须冒泡，不能被执行链吞掉。"""
        import asyncio as _asyncio

        async with chain_env(tmp_path, demo_repo) as (store, chain, _, _):
            class CancelRunner:
                async def run(self, job, work_dir, prompt, on_event):
                    raise _asyncio.CancelledError()

            chain.runner = CancelRunner()
            job = await make_job(store)
            with pytest.raises(_asyncio.CancelledError):
                await chain(job)


# =========================================================================
# D. 有界重试（验证失败 -> 带反馈重跑）
# =========================================================================

class TestVerificationRetry:
    @pytest.mark.asyncio
    async def test_retry_recovers_when_fix_improves(self, tmp_path: Path, demo_repo: Path):
        """第一次改动无效（验证失败），第二次改对 —— 应能走到 pr_opened。"""
        async with chain_env(
            tmp_path, demo_repo, scripts=["# not a fix\n", CALC_FIXED], publisher=FakePublisher()
        ) as (store, chain, runner, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "pr_opened"
            assert final.attempts == 2
            assert len(runner.calls) == 2
            # 第二次的提示词里带上了上一轮的测试输出
            assert "Previous attempt failed verification" in runner.calls[1]["prompt"]

    @pytest.mark.asyncio
    async def test_retry_ceiling_escalates(self, tmp_path: Path, demo_repo: Path):
        """一直修不好：跑满 attempts 上限后 escalate，而不是无限重试。"""
        bad = ["# still broken\n"]
        async with chain_env(tmp_path, demo_repo, scripts=bad, publisher=FakePublisher()) as (store, chain, runner, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "verification" in final.last_error
            assert final.attempts == store.max_fix_attempts
            assert len(runner.calls) == store.max_fix_attempts

    @pytest.mark.asyncio
    async def test_feedback_is_bounded(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo, scripts=["# broken\n", CALC_FIXED], publisher=FakePublisher()) as (store, chain, runner, _):
            long_output = "E" * (MAX_FEEDBACK_CHARS * 2)
            # 基线失败（预期）→ 第一次验证失败（触发重试）→ 修复后通过
            chain.test_runner = _StubTestRunner(
                [
                    _TestOutcomeStub(fail=True),
                    _TestOutcomeStub(fail=True, output=long_output),
                    _TestOutcomeStub(fail=False),
                ]
            )
            job = await make_job(store)
            await chain(job)
            prompt = runner.calls[1]["prompt"]
            assert len(prompt) < MAX_FEEDBACK_CHARS + 5000

    @pytest.mark.asyncio
    async def test_verify_failure_recorded_in_events(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo, scripts=["# broken\n", CALC_FIXED], publisher=FakePublisher()) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            events = await store.events(job.id)
            assert any(e.kind == "verification_retry" for e in events)
            assert any("verify_failed" in e.detail for e in events)


class _TestOutcomeStub:
    def __init__(self, fail: bool, output: str = "") -> None:
        from mewcode.service.execution import TestOutcome

        self._outcome = TestOutcome(command="stub", exit_code=1 if fail else 0, output=output)

    async def run(self, work_dir, command, timeout):
        return self._outcome


class _StubTestRunner:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def run(self, work_dir, command, timeout):
        index = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        return self.outcomes[index]._outcome


# =========================================================================
# E. 测试命令执行器（真实子进程）
# =========================================================================

class TestRealTestRunner:
    @pytest.mark.asyncio
    async def test_passing_command(self, tmp_path: Path):
        runner = TestRunner()
        outcome = await runner.run(str(tmp_path), f'"{sys.executable}" -c "print(1)"', 30)
        assert outcome.passed and outcome.exit_code == 0 and "1" in outcome.output

    @pytest.mark.asyncio
    async def test_failing_command_captures_output(self, tmp_path: Path):
        runner = TestRunner()
        outcome = await runner.run(
            str(tmp_path), f'"{sys.executable}" -c "import sys; print(\'boom\'); sys.exit(3)"', 30
        )
        assert not outcome.passed and outcome.exit_code == 3 and "boom" in outcome.output

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, tmp_path: Path):
        runner = TestRunner()
        outcome = await runner.run(
            str(tmp_path), f'"{sys.executable}" -c "import time; time.sleep(60)"', 1
        )
        assert outcome.timed_out and not outcome.passed

    @pytest.mark.asyncio
    async def test_runs_inside_work_dir(self, tmp_path: Path):
        runner = TestRunner()
        outcome = await runner.run(
            str(tmp_path), f'"{sys.executable}" -c "import os; print(os.getcwd())"', 30
        )
        assert Path(outcome.output.strip()).resolve() == tmp_path.resolve()

    @pytest.mark.asyncio
    async def test_same_second_same_size_edit_is_not_masked_by_pyc(self, tmp_path: Path):
        """回归：.pyc 的 mtime 只到秒，等长改动在同一秒内会被旧字节码掩盖。

        真实场景：agent 把 `a - b` 改成 `a + b`（等长），验证阶段却跑出基线的
        失败结果——修复被误判为无效。TestRunner 在这种时序下也必须拿到真实结果。
        """
        (tmp_path / "calc.py").write_text(CALC_BUGGY, encoding="utf-8")
        (tmp_path / "test_calc.py").write_text(TEST_SCRIPT, encoding="utf-8")
        runner = TestRunner()
        command = f'"{sys.executable}" test_calc.py'

        first = await runner.run(str(tmp_path), command, 30)
        assert not first.passed  # 基线：坏代码必须失败

        (tmp_path / "calc.py").write_text(CALC_FIXED, encoding="utf-8")  # 等长、同一秒
        second = await runner.run(str(tmp_path), command, 30)
        assert second.passed, f"stale bytecode masked the fix: {second.output!r}"


# =========================================================================
# F. prompt 组装（SOP）
# =========================================================================

class TestSop:
    def test_actionable_context_rules(self):
        from mewcode.service.jobs import Job

        def job(**payload) -> Job:
            return Job(id="job-x", fingerprint="f", repo="demo", severity="warning",
                       payload=payload, status="received", title=payload.pop("_title", ""))

        assert sop.has_actionable_context(job())[0] is False
        assert sop.has_actionable_context(job(source="manual"))[0] is False
        ok, _ = sop.has_actionable_context(job(_title="crash on startup", source="manual"))
        assert ok
        ok, _ = sop.has_actionable_context(job(source="manual", logs="Traceback..."))
        assert ok

    def test_alertmanager_payload_rendered(self):
        from mewcode.service.jobs import Job

        j = Job(
            id="job-1", fingerprint="f", repo="demo", severity="critical", status="received",
            title="5xx spike",
            payload={
                "source": "alertmanager",
                "labels": {"alertname": "HighErrorRate", "service": "orders"},
                "annotations": {"description": "12% 5xx"},
                "generator_url": "http://prom",
            },
        )
        context = sop.render_alert_context(j)
        assert "alertmanager" in context and "service=orders" in context and "12% 5xx" in context
        logs = sop.extract_logs(j)
        assert "12% 5xx" in logs

    def test_prompt_truncates_huge_logs(self):
        from mewcode.service.jobs import Job

        j = Job(id="job-1", fingerprint="f", repo="demo", severity="warning", status="received",
                title="t", payload={"source": "manual", "logs": "x" * 50_000})
        prompt = sop.build_alert_prompt(j, "/tmp/repo")
        assert len(prompt) < 20_000


class TestHeadlessRunnerBudget:
    @pytest.mark.asyncio
    async def test_budget_exceeded_raises(self, tmp_path: Path):
        """token 预算在 usage 回调处熔断（真实 runner 的路径）。"""
        from mewcode.config import ProviderConfig

        config = ServiceConfig(token_budget=100)
        provider = ProviderConfig(name="t", protocol="openai", base_url="http://x", model="m", api_key="k")
        runner = HeadlessAgentRunner(config, provider)

        class FakeAgent:
            async def run_to_completion(self, prompt, conversation=None, event_callback=None):
                event_callback({"type": "usage", "usage": {"inputTokens": 80, "outputTokens": 50}})
                return "done"

            async def cancel_background_tasks(self):
                pass

        runner._build_agent = lambda work_dir: FakeAgent()  # type: ignore[method-assign]
        from mewcode.service.jobs import Job

        job = Job(id="job-1", fingerprint="f", repo="demo", severity="w", status="fixing", payload={})
        with pytest.raises(TokenBudgetExceeded):
            await runner.run(job, str(tmp_path), "prompt", lambda e: None)

    @pytest.mark.asyncio
    async def test_background_tasks_cancelled_on_error(self, tmp_path: Path):
        from mewcode.config import ProviderConfig
        from mewcode.service.jobs import Job

        config = ServiceConfig()
        provider = ProviderConfig(name="t", protocol="openai", base_url="http://x", model="m", api_key="k")
        runner = HeadlessAgentRunner(config, provider)
        cancelled = {"done": False}

        class FakeAgent:
            async def run_to_completion(self, prompt, conversation=None, event_callback=None):
                raise RuntimeError("stream died")

            async def cancel_background_tasks(self):
                cancelled["done"] = True

        runner._build_agent = lambda work_dir: FakeAgent()  # type: ignore[method-assign]
        job = Job(id="job-1", fingerprint="f", repo="demo", severity="w", status="fixing", payload={})
        with pytest.raises(RuntimeError):
            await runner.run(job, str(tmp_path), "prompt", lambda e: None)
        assert cancelled["done"] is True


# =========================================================================
# G. CI 门禁（W4）：绿 -> human_review；红 -> 带信息重试；超时不假装成功
# =========================================================================

class FakeCIGate:
    """按脚本返回 CI 结论；记录调用次数。"""

    def __init__(self, states: list[str]) -> None:
        self.states = states
        self.calls = 0

    async def wait(self, job, context, published):
        from mewcode.service.vcs import CheckStatus

        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        if state == "boom":
            raise RuntimeError("github api unreachable")
        details = {"success": "all 2 checks passed", "failure": "failing checks: pytest",
                   "pending": "CI did not conclude within 1800s", "none": "no check runs"}[state]
        return CheckStatus(state=state, details=details)


@asynccontextmanager
async def ci_env(tmp_path: Path, repo: Path, states: list[str], scripts=None, publisher=None):
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    service = ServiceConfig(
        data_dir=str(tmp_path / "state"),
        repos={"demo": RepoConfig(name="demo", path=str(repo), test_command=_test_command())},
    )
    runner = FakeRunner(scripts)
    gate = FakeCIGate(states)
    chain = ExecutionChain(
        service, store, runner, publisher=publisher or FakePublisher(), ci_gate=gate, test_runner=TestRunner()
    )
    try:
        yield store, chain, runner, gate
    finally:
        await store.close()


class TestCIGate:
    @pytest.mark.asyncio
    async def test_green_ci_reaches_human_review(self, tmp_path: Path, demo_repo: Path):
        async with ci_env(tmp_path, demo_repo, ["success"]) as (store, chain, _, gate):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "human_review"
            assert final.ci_status == "success"
            assert final.pr_url
            assert gate.calls == 1
            kinds = {e.kind for e in await store.events(job.id)}
            assert "ci_status" in kinds

    @pytest.mark.asyncio
    async def test_ci_failure_retries_then_succeeds(self, tmp_path: Path, demo_repo: Path):
        """CI 红 -> 带失败信息重跑 agent -> 再次发布 -> 绿。"""
        async with ci_env(
            tmp_path, demo_repo, ["failure", "success"], scripts=[CALC_FIXED, CALC_FIXED]
        ) as (store, chain, runner, gate):
            job = await make_job(store)
            await chain(job)

            final = await store.get_or_raise(job.id)
            assert final.status == "human_review"
            assert final.attempts == 2
            assert len(runner.calls) == 2
            assert gate.calls == 2
            # 第二次的提示词带上了 CI 失败信息
            assert "CI failed" in runner.calls[1]["prompt"]
            events = await store.events(job.id)
            assert any(e.kind == "ci_retry" for e in events)
            assert any("ci_failed" in e.detail for e in events)

    @pytest.mark.asyncio
    async def test_ci_failure_exhausts_retries_and_escalates(self, tmp_path: Path, demo_repo: Path):
        async with ci_env(tmp_path, demo_repo, ["failure"]) as (store, chain, runner, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert final.attempts == store.max_fix_attempts
            assert len(runner.calls) == store.max_fix_attempts

    @pytest.mark.asyncio
    async def test_ci_timeout_escalates_not_pretend_success(self, tmp_path: Path, demo_repo: Path):
        async with ci_env(tmp_path, demo_repo, ["pending"]) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "did not conclude" in final.last_error

    @pytest.mark.asyncio
    async def test_repo_without_ci_does_not_block(self, tmp_path: Path, demo_repo: Path):
        async with ci_env(tmp_path, demo_repo, ["none"]) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "human_review"
            assert final.ci_status == "none"

    @pytest.mark.asyncio
    async def test_ci_polling_error_escalates(self, tmp_path: Path, demo_repo: Path):
        async with ci_env(tmp_path, demo_repo, ["boom"]) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            final = await store.get_or_raise(job.id)
            assert final.status == "escalate"
            assert "CI polling failed" in final.last_error

    @pytest.mark.asyncio
    async def test_no_ci_gate_stops_at_pr_opened(self, tmp_path: Path, demo_repo: Path):
        """未配置门禁时停在 pr_opened（等待外部流程），不擅自判定成功。"""
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher()) as (store, chain, _, _):
            job = await make_job(store)
            await chain(job)
            assert (await store.get_or_raise(job.id)).status == "pr_opened"


# =========================================================================
# H. 隔离不变式：agent 的工具必须绑定到 job 的 worktree
# =========================================================================

class TestWorktreeBinding:
    """真机验收时实测到的坑：工具没绑 work_dir 时，Bash 的 cwd 是服务进程的
    启动目录（主仓库），agent 的 shell 命令会跑在错误的地方。"""

    def test_registry_is_bound_to_worktree(self, tmp_path: Path):
        from mewcode.config import ProviderConfig

        provider = ProviderConfig(name="t", protocol="openai", base_url="http://x", model="m", api_key="k")
        runner = HeadlessAgentRunner(ServiceConfig(), provider)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        agent = runner._build_agent(str(worktree))

        assert agent.work_dir == str(worktree)
        bash = agent.registry.get("Bash")
        assert bash is not None
        assert bash._work_dir == str(worktree), "Bash 必须绑定到 worktree，否则命令跑在主仓库"
        for tool_name in ("ReadFile", "WriteFile", "EditFile", "Glob", "Grep"):
            tool = agent.registry.get(tool_name)
            if tool is not None and hasattr(tool, "_work_dir"):
                assert tool._work_dir == str(worktree), f"{tool_name} 未绑定 worktree"

    @pytest.mark.asyncio
    async def test_bash_tool_runs_inside_worktree(self, tmp_path: Path):
        """端到端确认：Bash 工具的 cwd 落在 worktree 内。"""
        from mewcode.config import ProviderConfig

        provider = ProviderConfig(name="t", protocol="openai", base_url="http://x", model="m", api_key="k")
        runner = HeadlessAgentRunner(ServiceConfig(), provider)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "marker.txt").write_text("inside", encoding="utf-8")

        agent = runner._build_agent(str(worktree))
        bash = agent.registry.get("Bash")
        result = await bash.execute(bash.params_model(command='cat marker.txt'))
        assert "inside" in result.output
        assert not result.is_error


# =========================================================================
# I. 企业规范注入（M2 W2）：skill 内联进提示词 + 自查结论进 PR body
# =========================================================================

class TestSkillInjection:
    def test_builtin_skills_exist_and_parse(self):
        from mewcode.skills.loader import SkillLoader

        skills = SkillLoader(".").load_all()
        for name in ("incident-triage", "org-code-style"):
            assert name in skills, f"missing builtin skill {name}"
            assert skills[name].prompt_body.strip()

    def test_prompt_embeds_skill_bodies(self, tmp_path: Path):
        from mewcode.service.jobs import Job
        from mewcode.service.sop import build_alert_prompt, load_skill_bodies

        bodies = load_skill_bodies(str(tmp_path), ["incident-triage", "org-code-style"])
        assert set(bodies) == {"incident-triage", "org-code-style"}

        job = Job(id="job-1", fingerprint="f", repo="demo", severity="critical", status="received",
                  title="5xx", payload={"source": "manual", "summary": "x"})
        prompt = build_alert_prompt(job, str(tmp_path), skills=bodies)
        assert "Organization skill: incident-triage" in prompt
        assert "Organization skill: org-code-style" in prompt
        assert "SELF-CHECK" in prompt                     # 要求输出自查段落
        assert "先让问题可复现" in prompt                  # 正文真的内联进来了

    def test_repo_skill_overrides_builtin(self, tmp_path: Path):
        """.mewcode/skills/ 同名文件覆盖内置（复用既有三级加载优先级）。"""
        from mewcode.service.sop import load_skill_bodies

        override = tmp_path / ".mewcode" / "skills"
        override.mkdir(parents=True)
        (override / "org-code-style.md").write_text(
            "---\nname: org-code-style\ndescription: 团队自己的规范\n---\n\n公司规范：必须写测试。\n",
            encoding="utf-8",
        )
        bodies = load_skill_bodies(str(tmp_path), ["org-code-style"])
        assert "公司规范：必须写测试。" in bodies["org-code-style"]
        assert "团队代码规范自查清单" not in bodies["org-code-style"]

    def test_skills_can_be_disabled(self, tmp_path: Path):
        from mewcode.service.jobs import Job
        from mewcode.service.sop import build_alert_prompt, load_skill_bodies

        job = Job(id="job-1", fingerprint="f", repo="demo", severity="critical", status="received",
                  title="5xx", payload={"source": "manual", "summary": "x"})
        prompt = build_alert_prompt(job, str(tmp_path), skills=load_skill_bodies(str(tmp_path), []))
        assert "Organization skill" not in prompt
        assert "SELF-CHECK" not in prompt

    def test_missing_skill_is_skipped_gracefully(self, tmp_path: Path):
        from mewcode.service.sop import load_skill_bodies

        bodies = load_skill_bodies(str(tmp_path), ["org-code-style", "no-such-skill"])
        assert set(bodies) == {"org-code-style"}

    @pytest.mark.asyncio
    async def test_chain_passes_skills_into_prompt(self, tmp_path: Path, demo_repo: Path):
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher()) as (store, chain, runner, _):
            job = await make_job(store)
            await chain(job)
            prompt = runner.calls[0]["prompt"]
            assert "Organization skill: org-code-style" in prompt

    @pytest.mark.asyncio
    async def test_config_can_restrict_skills(self, tmp_path: Path, demo_repo: Path):
        config = ServiceConfig(
            data_dir=str(tmp_path / "state"),
            skills=["incident-triage"],
            repos={"demo": RepoConfig(name="demo", path=str(demo_repo), test_command=_test_command())},
        )
        async with chain_env(tmp_path, demo_repo, publisher=FakePublisher(), config=config) as (store, chain, runner, _):
            job = await make_job(store)
            await chain(job)
            prompt = runner.calls[0]["prompt"]
            assert "Organization skill: incident-triage" in prompt
            assert "Organization skill: org-code-style" not in prompt   # 未启用的不注入
            assert "- `SELF-CHECK:`" not in prompt


class TestSelfCheckInPRBody:
    def make_ctx(self, final_text: str):
        from mewcode.service.execution import ExecutionContext

        return ExecutionContext(
            repo=RepoConfig(name="demo", path="/srv/demo", test_command="pytest -q"),
            work_dir="/tmp/wt",
            prompt="p",
            agent=AgentRunOutcome(final_text=final_text),
            changed_files=["app.py"],
            diff="+1/-1",
            baseline_test=None,
            verify_test=None,
        )

    def make_job(self):
        from mewcode.service.jobs import Job

        return Job(id="job-1", fingerprint="f", repo="demo", severity="critical",
                   status="pr_opened", title="5xx", payload={"source": "manual", "summary": "s"})

    def test_self_check_section_rendered(self):
        from mewcode.service.publisher import build_pr_body

        ctx = self.make_ctx(
            "ROOT CAUSE: sign error\nFIX: fixed add()\nVERIFICATION: ran tests\n"
            "SELF-CHECK:\n- [x] errors handled\n- [ ] not met: no new test\n"
        )
        body = build_pr_body(self.make_job(), ctx, [])
        assert "## 规范自查（org-code-style）" in body
        assert "- [x] errors handled" in body
        assert "未满足项" in body          # 提醒 review 者重点看未满足项

    def test_no_self_check_no_section(self):
        from mewcode.service.publisher import build_pr_body

        body = build_pr_body(self.make_job(), self.make_ctx("ROOT CAUSE: a\nFIX: b"), [])
        assert "规范自查" not in body
