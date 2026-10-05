"""仓库策略（M3 W2）：<repo>/.mewcode/policy.yaml 的解析与四个生效点。

- 解析（RepoPolicyLoader）：合法 / 缺失 / 损坏 / 未知键——损坏必须 PolicyError，
  不允许静默回退；
- intake 路由（runtime.intake）：severities 白名单在门口拒收，不建 job、不污染去重；
- 执行链（ExecutionChain）：target_branch 决定 worktree 基线并经 ExecutionContext
  传给 publisher；策略损坏 → escalate；
- token 预算（HeadlessAgentRunner）：policy > 服务配置；沙箱模式事后门禁；
- 通知（RepoPolicyNotifier）：policy.notify > 默认渠道，PolicyError 上抛不吞。
"""

from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from mewcode.config import ProviderConfig, RepoConfig, ServiceConfig, VCSConfig
from mewcode.service.execution import (
    AgentRunOutcome,
    ExecutionChain,
    ExecutionContext,
    HeadlessAgentRunner,
    PublishResult,
    TokenBudgetExceeded,
)
from mewcode.service.jobs import Job, JobStore
from mewcode.service.notify import RepoPolicyNotifier
from mewcode.service.policy import PolicyError, RepoPolicy, RepoPolicyLoader
from mewcode.service.publisher import PullRequestPublisher
from mewcode.service.runtime import ServiceRuntime
from mewcode.service.triggers.base import JobDraft
from mewcode.service.vcs import GitHubVCS, PRInfo

# ---------------------------------------------------------------------------
# 工具与策略样例
# ---------------------------------------------------------------------------

POLICY_FULL = """\
triggers:
  severities: [critical, warning]
target_branch: develop
token_budget: 50000
notify:
  type: slack
  webhook_url: https://hooks.example/svc-b
  timeout_seconds: 5
"""

POLICY_CRITICAL_ONLY = """\
triggers:
  severities: [critical]
"""

POLICY_TARGET_DEVELOP = """\
target_branch: develop
"""

POLICY_BUDGET_50 = """\
token_budget: 50
"""

POLICY_NOTIFY_SLACK = """\
notify:
  type: slack
  webhook_url: https://hooks.example/svc-b
"""


def write_policy(repo: Path, text: str | None) -> None:
    d = repo / ".mewcode"
    d.mkdir(exist_ok=True)
    if text is not None:
        (d / "policy.yaml").write_text(text, encoding="utf-8")


def make_repo(tmp_path: Path, policy: str | None = None, name: str = "demo") -> Path:
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    if policy is not None:
        write_policy(repo, policy)
    return repo


def make_loader(tmp_path: Path, policy: str | None = None) -> RepoPolicyLoader:
    repo = make_repo(tmp_path, policy)
    return RepoPolicyLoader({"demo": RepoConfig(name="demo", path=str(repo))})


def make_job(repo: str = "demo", **kw: Any) -> Job:
    fields: dict[str, Any] = {
        "id": "job-1",
        "fingerprint": "fp",
        "repo": repo,
        "severity": "warning",
        "status": "received",
        "payload": {},
    }
    fields.update(kw)
    return Job(**fields)


def draft(**kw: Any) -> JobDraft:
    fields: dict[str, Any] = {
        "fingerprint": "fp-1",
        "repo": "demo",
        "severity": "warning",
        "title": "t",
        "payload": {},
    }
    fields.update(kw)
    return JobDraft(**fields)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def init_worktree_dir(tmp_path: Path, job_id: str) -> Path:
    """链路测试的假 worktree：真 git 仓，_collect_diff 才能看到改动。"""
    wd = tmp_path / f"wt-{job_id}"
    wd.mkdir()
    _git(wd, "init")
    _git(wd, "config", "user.email", "test@test.com")
    _git(wd, "config", "user.name", "Test")
    (wd / "base.txt").write_text("base\n", encoding="utf-8")
    _git(wd, "add", ".")
    _git(wd, "commit", "-m", "init")
    return wd


# ---------------------------------------------------------------------------
# A. 解析：宁要 PolicyError，不要静默回退
# ---------------------------------------------------------------------------


class TestPolicyParsing:
    def test_missing_file_yields_empty_policy(self, tmp_path: Path):
        policy = make_loader(tmp_path).load("demo")
        assert policy.severities == ()
        assert policy.target_branch == ""
        assert policy.token_budget == 0
        assert policy.notify is None
        assert policy.source == ""
        assert not policy.overrides_anything

    def test_unknown_repo_yields_empty_policy(self, tmp_path: Path):
        loader = make_loader(tmp_path, policy=POLICY_FULL)
        assert not loader.load("other").overrides_anything

    def test_full_policy_parses(self, tmp_path: Path):
        policy = make_loader(tmp_path, policy=POLICY_FULL).load("demo")
        assert policy.severities == ("critical", "warning")
        assert policy.target_branch == "develop"
        assert policy.token_budget == 50000
        assert policy.notify is not None
        assert policy.notify.type == "slack"
        assert policy.notify.webhook_url == "https://hooks.example/svc-b"
        assert policy.notify.timeout_seconds == 5
        assert policy.source.endswith("policy.yaml")
        assert policy.overrides_anything

    def test_accepts_semantics(self):
        assert RepoPolicy().accepts("info")
        assert not RepoPolicy(severities=("critical",)).accepts("warning")
        assert RepoPolicy(severities=("critical",)).accepts("critical")

    def test_empty_file_is_no_overrides(self, tmp_path: Path):
        assert not make_loader(tmp_path, policy="").load("demo").overrides_anything

    @pytest.mark.parametrize(
        "text",
        [
            "target_branch: [a, b]\n",  # 类型错
            "token_budget: 0\n",  # 必须 >0（删除键才是"回到服务配置"）
            "token_budget: -5\n",
            "token_budget: lots\n",
            "triggers:\n  severities: [critical, fatal]\n",  # 未知 severity
            "triggers:\n  severities: []\n",  # 空列表
            "triggers:\n  severities: [critical, critical]\n",  # 重复
            "unknown_key: 1\n",  # 未知顶层键
            "triggers:\n  alert_names: [x]\n",  # 未知 triggers 键
            "notify:\n  type: sms\n",  # 未知 notify 类型
            "notify:\n  type: slack\n  bogus: 1\n",  # 未知 notify 键
            "notify:\n  type: slack\n  timeout_seconds: 0\n",
            "just a string\n",  # 顶层不是 mapping
        ],
    )
    def test_malformed_policy_raises(self, tmp_path: Path, text: str):
        loader = make_loader(tmp_path, policy=text)
        with pytest.raises(PolicyError):
            loader.load("demo")

    def test_malformed_yaml_raises(self, tmp_path: Path):
        loader = make_loader(tmp_path, policy="target_branch: [unclosed\n")
        with pytest.raises(PolicyError):
            loader.load("demo")


# ---------------------------------------------------------------------------
# B. 通知路由：policy.notify > 默认渠道；PolicyError 上抛不吞
# ---------------------------------------------------------------------------


class RecordingNotifier:
    def __init__(self, name: str):
        self.name = name
        self.calls: list[tuple[str, str]] = []

    async def notify_job_event(self, job: Job, phase: str, detail: str = "") -> None:
        self.calls.append((job.repo, phase))


class TestRepoPolicyNotifier:
    def make(
        self, tmp_path: Path, policy: str | None = None
    ) -> tuple[RepoPolicyNotifier, RecordingNotifier]:
        loader = make_loader(tmp_path, policy=policy)
        default = RecordingNotifier("default")
        return RepoPolicyNotifier(default, loader), default

    @pytest.mark.asyncio
    async def test_no_policy_uses_default_channel(self, tmp_path: Path):
        notifier, default = self.make(tmp_path)
        await notifier.notify_job_event(make_job(), "received")
        assert default.calls == [("demo", "received")]

    @pytest.mark.asyncio
    async def test_policy_notify_overrides_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        built: list[RecordingNotifier] = []

        def fake_build(config: Any, store: Any = None, transport: Any = None) -> RecordingNotifier:
            channel = RecordingNotifier(f"built-{config.type}-{config.webhook_url}")
            built.append(channel)
            return channel

        monkeypatch.setattr("mewcode.service.notify.build_notifier", fake_build)
        notifier, default = self.make(tmp_path, policy=POLICY_NOTIFY_SLACK)
        await notifier.notify_job_event(make_job(), "fixing")
        assert default.calls == []  # 默认渠道没被用
        assert len(built) == 1 and built[0].calls == [("demo", "fixing")]

    @pytest.mark.asyncio
    async def test_other_repo_without_policy_uses_default(self, tmp_path: Path):
        """同一服务里，没写策略的仓库不受别的仓库策略影响。"""
        loader = make_loader(tmp_path, policy=POLICY_NOTIFY_SLACK)
        default = RecordingNotifier("default")
        notifier = RepoPolicyNotifier(default, loader)
        await notifier.notify_job_event(make_job(repo="plain"), "received")
        assert default.calls == [("plain", "received")]

    @pytest.mark.asyncio
    async def test_unreadable_policy_raises_instead_of_falling_back(self, tmp_path: Path):
        notifier, default = self.make(tmp_path, policy="notify: {type: sms}\n")
        with pytest.raises(PolicyError):
            await notifier.notify_job_event(make_job(), "received")
        assert default.calls == []  # 宁可不发，也不悄悄换渠道


# ---------------------------------------------------------------------------
# C. intake 路由：策略在门口拒收，不建 job、不污染去重
# ---------------------------------------------------------------------------


async def make_runtime(tmp_path: Path, policy: str | None = None):
    repo = make_repo(tmp_path, policy)
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()

    async def noop_handler(job: Job) -> None:  # pragma: no cover - intake 不启动 pool
        return None

    config = ServiceConfig(repos={"demo": RepoConfig(name="demo", path=str(repo))})
    runtime = ServiceRuntime(config, handler=noop_handler, store=store)
    return runtime, store


class TestIntakeRouting:
    @pytest.mark.asyncio
    async def test_rejected_severity_creates_no_job(self, tmp_path: Path):
        runtime, store = await make_runtime(tmp_path, POLICY_CRITICAL_ONLY)
        result = await runtime.intake([draft(severity="warning")])
        assert result.accepted == []
        assert len(result.rejected) == 1
        assert "critical" in result.rejected[0]["reason"]
        assert result.rejected[0]["repo"] == "demo"
        assert await store.list_jobs(status=None, limit=10) == []
        assert result.as_dict()["rejected"] == result.rejected

    @pytest.mark.asyncio
    async def test_matching_severity_accepted(self, tmp_path: Path):
        runtime, _ = await make_runtime(tmp_path, POLICY_CRITICAL_ONLY)
        result = await runtime.intake([draft(severity="critical")])
        assert len(result.accepted) == 1
        assert result.rejected == []

    @pytest.mark.asyncio
    async def test_no_policy_accepts_everything(self, tmp_path: Path):
        runtime, _ = await make_runtime(tmp_path)
        result = await runtime.intake([draft(severity="info")])
        assert len(result.accepted) == 1 and result.rejected == []

    @pytest.mark.asyncio
    async def test_unreadable_policy_rejects_with_reason(self, tmp_path: Path):
        runtime, store = await make_runtime(tmp_path, "token_budget: [1]\n")
        result = await runtime.intake([draft()])
        assert result.accepted == []
        assert "unreadable" in result.rejected[0]["reason"]
        assert await store.list_jobs(status=None, limit=10) == []

    @pytest.mark.asyncio
    async def test_rejection_does_not_pollute_dedup(self, tmp_path: Path):
        """被拒的告警不建 job：之后同指纹的合法告警仍能正常受理。"""
        runtime, _ = await make_runtime(tmp_path, POLICY_CRITICAL_ONLY)
        await runtime.intake([draft(severity="warning", fingerprint="fp-9")])
        result = await runtime.intake([draft(severity="critical", fingerprint="fp-9")])
        assert len(result.accepted) == 1
        assert result.deduped == []


# ---------------------------------------------------------------------------
# D. 执行链：policy.target_branch 决定 worktree 基线并传给 publisher；坏策略 escalate
# ---------------------------------------------------------------------------


class NoChangeRunner:
    """不产生改动的假 runner：链路停在 cant_repro，聚焦断言基线分支。"""

    async def run(self, job: Job, work_dir: str, prompt: str, on_event: Any) -> AgentRunOutcome:
        on_event({"type": "usage", "usage": {"inputTokens": 10, "outputTokens": 5}})
        return AgentRunOutcome(final_text="nothing", tool_calls=0, input_tokens=10, output_tokens=5)


class WritingRunner:
    """写一个文件的假 runner：让链路走到 publish。"""

    async def run(self, job: Job, work_dir: str, prompt: str, on_event: Any) -> AgentRunOutcome:
        (Path(work_dir) / "fix.txt").write_text("fixed\n", encoding="utf-8")
        on_event({"type": "usage", "usage": {"inputTokens": 10, "outputTokens": 5}})
        return AgentRunOutcome(
            final_text="ROOT CAUSE: x\nFIX: y", tool_calls=1, input_tokens=10, output_tokens=5
        )


class RecordingPublisher:
    def __init__(self) -> None:
        self.contexts: list[ExecutionContext] = []

    async def publish(self, job: Job, context: ExecutionContext) -> Any:
        self.contexts.append(context)
        return PublishResult(pr_url="https://x/pr/1", branch=f"mewfix/{job.id}")


@asynccontextmanager
async def chain_env(
    tmp_path: Path,
    policy: str | None,
    runner: Any,
    *,
    publisher: Any = None,
):
    repo = make_repo(tmp_path, policy)
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    config = ServiceConfig(repos={"demo": RepoConfig(name="demo", path=str(repo))})
    bases: list[str] = []

    class Manager:
        async def create(self, job_id: str, base_branch: str | None = None) -> Any:
            bases.append(base_branch or "")
            wd = init_worktree_dir(tmp_path, job_id)
            return SimpleNamespace(path=str(wd), branch=base_branch or "")

    chain = ExecutionChain(
        config,
        store,
        runner,
        publisher=publisher,
        test_runner=None,
        worktree_manager_factory=lambda root: Manager(),
    )
    try:
        yield store, chain, bases
    finally:
        await store.close()


async def make_chain_job(store: JobStore) -> Job:
    return await store.create_job(
        fingerprint="fp-2",
        repo="demo",
        severity="critical",
        title="t",
        payload={"source": "manual", "summary": "s", "logs": "l"},
    )


class TestChainTargetBranch:
    @pytest.mark.asyncio
    async def test_policy_target_branch_reaches_worktree(self, tmp_path: Path):
        async with chain_env(tmp_path, POLICY_TARGET_DEVELOP, NoChangeRunner()) as (
            store,
            chain,
            bases,
        ):
            job = await make_chain_job(store)
            await chain(job)
            assert bases == ["develop"]
            refreshed = await store.get(job.id)
            assert refreshed is not None and refreshed.status == "cant_repro"
            events = await store.events(job.id)
            applied = [e.detail for e in events if e.kind == "policy_applied"]
            assert applied and "target_branch=develop" in applied[0] and "source=" in applied[0]

    @pytest.mark.asyncio
    async def test_repo_falls_back_to_head_without_policy(self, tmp_path: Path):
        """无策略 = 行为与 M1 完全一致：repo.base_branch（缺省）或 HEAD，无 policy 事件。"""
        async with chain_env(tmp_path, None, NoChangeRunner()) as (store, chain, bases):
            job = await make_chain_job(store)
            await chain(job)
            assert bases == ["HEAD"]
            events = await store.events(job.id)
            assert not any(e.kind == "policy_applied" for e in events)

    @pytest.mark.asyncio
    async def test_context_carries_target_branch_to_publisher(self, tmp_path: Path):
        publisher = RecordingPublisher()
        async with chain_env(
            tmp_path, POLICY_TARGET_DEVELOP, WritingRunner(), publisher=publisher
        ) as (store, chain, bases):
            job = await make_chain_job(store)
            await chain(job)
            assert publisher.contexts and publisher.contexts[0].target_branch == "develop"
            refreshed = await store.get(job.id)
            assert refreshed is not None and refreshed.status == "pr_opened"

    @pytest.mark.asyncio
    async def test_unreadable_policy_escalates_without_running_agent(self, tmp_path: Path):
        ran: list[bool] = []

        class BoomRunner:
            async def run(
                self, job: Job, work_dir: str, prompt: str, on_event: Any
            ) -> AgentRunOutcome:
                ran.append(True)  # pragma: no cover - 不应被调用
                raise AssertionError("agent must not run")

        async with chain_env(tmp_path, "token_budget: [x]\n", BoomRunner()) as (
            store,
            chain,
            bases,
        ):
            job = await make_chain_job(store)
            await chain(job)
            assert ran == []
            refreshed = await store.get(job.id)
            assert refreshed is not None and refreshed.status == "escalate"
            events = await store.events(job.id)
            assert any("policy unreadable" in e.detail for e in events if e.kind == "escalated")


# ---------------------------------------------------------------------------
# E. token 预算：policy > 服务配置；沙箱事后门禁；坏策略在 agent 之前失败
# ---------------------------------------------------------------------------


class UsageAgent:
    def __init__(self, tokens: int) -> None:
        self._tokens = tokens

    async def run_to_completion(
        self, prompt: str, conversation: Any = None, event_callback: Any = None
    ) -> str:
        if event_callback is not None:
            event_callback(
                {"type": "usage", "usage": {"inputTokens": self._tokens, "outputTokens": 0}}
            )
        return "done"

    async def cancel_background_tasks(self) -> None:
        return None


def make_runner(tmp_path: Path, policy: str | None, service_budget: int = 0) -> HeadlessAgentRunner:
    repo = make_repo(tmp_path, policy)
    config = ServiceConfig(
        token_budget=service_budget,
        repos={"demo": RepoConfig(name="demo", path=str(repo))},
    )
    provider = ProviderConfig(name="t", protocol="openai", base_url="http://x", model="m", api_key="k")
    return HeadlessAgentRunner(config, provider, policy_loader=RepoPolicyLoader(config.repos))


class TestRunnerTokenBudget:
    @pytest.mark.asyncio
    async def test_policy_budget_overrides_service(self, tmp_path: Path):
        runner = make_runner(tmp_path, POLICY_BUDGET_50, service_budget=10_000)
        runner._build_agent = lambda work_dir: UsageAgent(60)  # type: ignore[method-assign]
        with pytest.raises(TokenBudgetExceeded):
            await runner.run(make_job(), str(tmp_path), "p", lambda e: None)

    @pytest.mark.asyncio
    async def test_service_budget_used_when_policy_absent(self, tmp_path: Path):
        runner = make_runner(tmp_path, None, service_budget=50)
        runner._build_agent = lambda work_dir: UsageAgent(60)  # type: ignore[method-assign]
        with pytest.raises(TokenBudgetExceeded):
            await runner.run(make_job(), str(tmp_path), "p", lambda e: None)

    @pytest.mark.asyncio
    async def test_higher_policy_budget_allows_run(self, tmp_path: Path):
        runner = make_runner(tmp_path, "token_budget: 500\n", service_budget=10)
        runner._build_agent = lambda work_dir: UsageAgent(60)  # type: ignore[method-assign]
        outcome = await runner.run(make_job(), str(tmp_path), "p", lambda e: None)
        assert outcome.total_tokens == 60

    @pytest.mark.asyncio
    async def test_unreadable_policy_fails_before_agent_runs(self, tmp_path: Path):
        runner = make_runner(tmp_path, "token_budget: [x]\n")
        ran: list[bool] = []

        def _boom(work_dir: str) -> Any:  # pragma: no cover - 不应被调用
            ran.append(True)
            raise AssertionError("agent must not be built")

        runner._build_agent = _boom  # type: ignore[method-assign]
        with pytest.raises(PolicyError):
            await runner.run(make_job(), str(tmp_path), "p", lambda e: None)
        assert ran == []

    @pytest.mark.asyncio
    async def test_sandbox_post_run_budget_gate(self, tmp_path: Path):
        runner = make_runner(tmp_path, POLICY_BUDGET_50)
        events: list[dict] = []

        class FakeSandbox:
            async def available(self) -> bool:
                return True

            async def run_agent(self, *args: Any, **kwargs: Any) -> Any:
                return SimpleNamespace(
                    result_text="done",
                    tool_calls=1,
                    input_tokens=100,
                    output_tokens=0,
                    timed_out=False,
                    exit_code=0,
                    extra={},
                    stdout="",
                    stderr="",
                )

        runner.sandbox = FakeSandbox()  # type: ignore[method-assign]
        with pytest.raises(TokenBudgetExceeded):
            await runner.run(make_job(), str(tmp_path), "p", events.append)
        # 用量证据先于熔断落事件——escalate 的记录里看得到这次花了多少
        assert any(e.get("type") == "usage" for e in events)


# ---------------------------------------------------------------------------
# F. publisher → vcs：context.target_branch 显式传 base；vcs 层缺省回落配置
# ---------------------------------------------------------------------------


class FakeVCS:
    def __init__(self) -> None:
        self.bases: list[str] = []

    async def ensure_branch(self, work_dir: str, branch: str) -> None:
        return None

    async def commit_all(self, work_dir: str, message: str, exclude: tuple[str, ...] = ()) -> bool:
        return True

    async def push(self, work_dir: str, branch: str) -> None:
        return None

    async def resolve_repo_slug(self, work_dir: str) -> str:
        return "acme/demo"

    async def find_open_pr(self, slug: str, head_branch: str) -> None:
        return None

    async def create_pr(
        self, slug: str, head_branch: str, title: str, body: str, base: str = ""
    ) -> PRInfo:
        self.bases.append(base)
        return PRInfo(number=1, url="https://x/1", head_branch=head_branch, base_branch=base)


def make_context(tmp_path: Path, target_branch: str = "") -> ExecutionContext:
    return ExecutionContext(
        repo=RepoConfig(name="demo", path=str(tmp_path)),
        work_dir=str(tmp_path),
        prompt="p",
        agent=AgentRunOutcome(),
        changed_files=["a.py"],
        diff="+a\n",
        baseline_test=None,
        verify_test=None,
        target_branch=target_branch,
    )


class TestPublisherBaseBranch:
    @pytest.mark.asyncio
    async def test_publisher_passes_policy_target_branch(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(
                fingerprint="fp", repo="demo", severity="critical", title="t", payload={}
            )
            vcs = FakeVCS()
            publisher = PullRequestPublisher(vcs, store, ServiceConfig())
            await publisher.publish(job, make_context(tmp_path, target_branch="develop"))
            assert vcs.bases == ["develop"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_publisher_without_target_branch_passes_empty(self, tmp_path: Path):
        """空 target_branch 原样传给 vcs 层，由它回落服务配置（M1 行为不变）。"""
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(
                fingerprint="fp", repo="demo", severity="critical", title="t", payload={}
            )
            vcs = FakeVCS()
            publisher = PullRequestPublisher(vcs, store, ServiceConfig())
            await publisher.publish(job, make_context(tmp_path))
            assert vcs.bases == [""]
        finally:
            await store.close()


def make_vcs(handler: Any) -> GitHubVCS:
    return GitHubVCS(
        VCSConfig(base_branch="master", token="t" * 10),
        transport=httpx.MockTransport(handler),
    )


class TestVCSCreatePRBase:
    @pytest.mark.asyncio
    async def test_explicit_base_wins(self):
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.content)
            return httpx.Response(201, json={"number": 1, "html_url": "https://x/1"})

        vcs = make_vcs(handler)
        info = await vcs.create_pr("acme/demo", "mewfix/j1", "t", "b", base="develop")
        assert captured["json"]["base"] == "develop"
        assert info.base_branch == "develop"

    @pytest.mark.asyncio
    async def test_base_defaults_to_service_config(self):
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.content)
            return httpx.Response(201, json={"number": 2, "html_url": "https://x/2"})

        vcs = make_vcs(handler)
        info = await vcs.create_pr("acme/demo", "mewfix/j2", "t", "b")
        assert captured["json"]["base"] == "master"
        assert info.base_branch == "master"
