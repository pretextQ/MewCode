"""M1 W6：端到端验收测试（全栈，除 LLM 与真实 GitHub 之外都是真件）。

链路：Alertmanager webhook → aiohttp 服务 → JobStore → worker → 执行链
（真实 git worktree）→ 真实 TestRunner → 真实 PullRequestPublisher /
GitHubCIGate（git 打本地 bare 仓库当远端，GitHub REST 用 MockTransport）→
human_review。

demo 仓库按"一个场景一个 bug"组织（三类 bug：空指针 / 配置错误 / 超时未处理）：
同一个仓库同时埋三个 bug 时，仓级测试命令在只修一个的情况下必然还是红的，
无法验证"某个告警被修好"——真实项目里也是每个故障对应一次修复。

覆盖 M1 验收标准：
1. 端到端：模拟告警 → 无人干预 → PR 出现且 CI 绿（三类 bug 均走通）；
2. 失败路径：空 payload 在 triaging escalate，不产生垃圾 PR；
3. 稳定性：重启恢复未完结 job、重复告警去重、跑飞的 job 超时收敛；
4. 安全：无 merge 能力、base 分支不可被 payload 改写、远端动作有审计。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from mewcode.config import NotifyConfig, RepoConfig, ServiceConfig, VCSConfig
from mewcode.service.api import create_app
from mewcode.service.execution import (
    AgentRunOutcome,
    ExecutionChain,
    TestRunner,
)
from mewcode.service.jobs import JobStore
from mewcode.service.notify import WebhookNotifier
from mewcode.service.publisher import GitHubCIGate, PullRequestPublisher
from mewcode.service.runtime import ServiceRuntime
from mewcode.service.triggers import build_adapters
from mewcode.service.vcs import GitHubVCS

FAKE_TOKEN = "ghp_" + "e" * 36

#: 三类 demo bug：每种 = (坏代码, 修好的代码, 只测这一种的测试脚本)
DEMO_BUGS: dict[str, tuple[str, str, str]] = {
    "null_deref": (
        "def owner_name(user):\n    return user['profile']['name'].upper()\n",
        "def owner_name(user):\n    profile = user.get('profile') or {}\n"
        "    return (profile.get('name') or 'unknown').upper()\n",
        "import app\n"
        "try:\n"
        "    name = app.owner_name({'profile': None})\n"
        "except Exception as e:\n"
        "    print('null_deref crash: %r' % e); raise SystemExit(1)\n"
        "print('ok:', name)\n",
    ),
    "config_error": (
        "TIMEOUT_SECONDS = 0\n\n\ndef timeout():\n    return TIMEOUT_SECONDS\n",
        "TIMEOUT_SECONDS = 30\n\n\ndef timeout():\n    return TIMEOUT_SECONDS\n",
        "import app\n"
        "if app.timeout() <= 0:\n"
        "    print('config_error: timeout is %r' % app.timeout()); raise SystemExit(1)\n"
        "print('ok:', app.timeout())\n",
    ),
    "unhandled_timeout": (
        "def fetch(client):\n    return client.get('/data')\n",
        "def fetch(client):\n    try:\n        return client.get('/data')\n"
        "    except TimeoutError:\n        return None\n",
        "import app\n\n"
        "class Client:\n"
        "    def get(self, path):\n"
        "        raise TimeoutError('upstream timed out')\n\n"
        "try:\n"
        "    app.fetch(Client())\n"
        "except TimeoutError as e:\n"
        "    print('unhandled_timeout: %r' % e); raise SystemExit(1)\n"
        "print('ok: timeout handled')\n",
    ),
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def make_demo_environment(tmp_path: Path, bug: str) -> dict:
    """一个"远端 demo 仓库"（bare）+ 本地克隆，埋好指定的一类 bug。"""
    bare = tmp_path / "demo-origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)
    work = tmp_path / "checkout"
    work.mkdir()

    for args in (["init"], ["config", "user.email", "demo@example.com"], ["config", "user.name", "Demo"]):
        _git(work, *args)
    buggy, _fixed, test_source = DEMO_BUGS[bug]
    (work / "app.py").write_text(buggy, encoding="utf-8")
    (work / "test_app.py").write_text(test_source, encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", f"demo: planted {bug}")
    _git(work, "branch", "-M", "main")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "main")
    return {"bare": bare, "checkout": work, "bug": bug}


class ScriptedRunner:
    """替代真实 LLM 的 agent：按告警指到的 bug 真的改文件。"""

    def __init__(self, bug: str) -> None:
        self.bug = bug
        self.calls: list[str] = []

    async def run(self, job, work_dir, prompt, on_event):
        self.calls.append(prompt)
        buggy, fixed, _ = DEMO_BUGS[self.bug]
        source = Path(work_dir) / "app.py"
        text = source.read_text(encoding="utf-8")
        if buggy in text:
            source.write_text(text.replace(buggy, fixed), encoding="utf-8")
        on_event({"type": "tool_use", "toolName": "EditFile", "args": {"file_path": "app.py"}})
        on_event({"type": "usage", "usage": {"inputTokens": 1200, "outputTokens": 300}})
        return AgentRunOutcome(
            final_text=(
                f"ROOT CAUSE: {self.bug} in app.py\n"
                f"FIX: corrected {self.bug}\n"
                "VERIFICATION: ran test_app.py successfully"
            ),
            tool_calls=3,
            input_tokens=1200,
            output_tokens=300,
        )


class FakeGitHub:
    """假的 GitHub API：记录 PR 与 check-runs 交互。"""

    def __init__(self, ci_states: list[str] | None = None) -> None:
        self.ci_states = ci_states or ["success"]
        self.prs: list[dict] = []
        self.check_calls = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/pulls") and request.method == "GET":
                return httpx.Response(200, json=[])
            if path.endswith("/pulls") and request.method == "POST":
                body = json.loads(request.content)
                pr = {
                    "number": len(self.prs) + 1,
                    "html_url": f"https://github.com/acme/demo/pull/{len(self.prs) + 1}",
                    **body,
                }
                self.prs.append(pr)
                return httpx.Response(201, json=pr)
            if path.endswith("/check-runs"):
                self.check_calls += 1
                state = self.ci_states[min(self.check_calls - 1, len(self.ci_states) - 1)]
                if state == "pending":
                    return httpx.Response(200, json={"check_runs": [
                        {"name": "ci", "status": "in_progress", "conclusion": None},
                    ]})
                return httpx.Response(200, json={"check_runs": [
                    {"name": "ci", "status": "completed",
                     "conclusion": "success" if state == "success" else "failure"},
                ]})
            return httpx.Response(404, json={"message": f"unmapped {request.method} {path}"})

        return httpx.MockTransport(handler)


class CaptureNotifier(WebhookNotifier):
    def __init__(self) -> None:
        super().__init__(
            NotifyConfig(type="slack", webhook_url="https://notify.example/x"),
            None,
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")),
        )
        self.sent: list[tuple[str, str]] = []

    async def notify_job_event(self, job, phase, detail=""):
        self.sent.append((phase, detail))
        await super().notify_job_event(job, phase, detail)


def build_service_config(tmp_path: Path, env: dict, **overrides) -> ServiceConfig:
    repos = {"demo": RepoConfig(
        name="demo",
        path=str(env["checkout"]),
        url="https://github.com/acme/demo.git",
        base_branch="main",
        test_command=f'"{sys.executable}" test_app.py',
    )}
    cfg = dict(
        data_dir=str(tmp_path / "state"),
        concurrency=1,
        job_timeout_seconds=120,
        drain_timeout_seconds=5,
        dedup_window_seconds=300,
        repos=repos,
        vcs=VCSConfig(
            provider="github", token=FAKE_TOKEN, remote="origin", base_branch="main",
            ci_poll_interval_seconds=1, ci_timeout_seconds=10, ci_none_grace_seconds=2,
        ),
    )
    cfg.update(overrides)
    return ServiceConfig(**cfg)


class E2EStack:
    """按生产 wiring 组装服务栈（只替换 agent 与 GitHub API）。"""

    def __init__(self, tmp_path: Path, env: dict, *, runner=None, ci_states=None, service=None) -> None:
        self.env = env
        self.github = FakeGitHub(ci_states)
        self.notifier = CaptureNotifier()
        self.runner = runner or ScriptedRunner(env["bug"])
        self.service = service or build_service_config(tmp_path, env)
        self.tmp_path = tmp_path
        self.vcs = None

    async def __aenter__(self):
        self.store = JobStore(self.tmp_path / "jobs.db")
        await self.store.connect()

        self.vcs = GitHubVCS(self.service.vcs, transport=self.github.transport())
        # git 走本地 bare 仓库（真实的 branch/commit/push），slug 用假的 owner/repo
        self.vcs.remote_url = self._local_remote_url  # type: ignore[method-assign]
        self.vcs.resolve_repo_slug = self._slug  # type: ignore[method-assign]

        self.chain = ExecutionChain(
            self.service,
            self.store,
            self.runner,
            test_runner=TestRunner(),
            publisher=PullRequestPublisher(self.vcs, self.store, self.service),
            ci_gate=GitHubCIGate(self.vcs),
            notifier=self.notifier,
        )
        self.runtime = ServiceRuntime(
            self.service,
            handler=self.chain,
            store=self.store,
            worktree_cleanup_cutoff_hours=None,
            notifier=self.notifier,
        )
        await self.runtime.start(recover=False)
        self.client = TestClient(TestServer(create_app(self.runtime, build_adapters(self.service))))
        await self.client.start_server()
        return self

    async def _local_remote_url(self, work_dir: str) -> str:
        return str(self.env["bare"])

    async def _slug(self, work_dir: str) -> str:
        return "acme/demo"

    async def __aexit__(self, *exc) -> None:
        await self.client.close()
        await self.runtime.stop()

    async def settle(self, timeout: float = 60) -> None:
        await asyncio.wait_for(self.runtime.pool._queue.join(), timeout=timeout)

    def alert(self, **overrides) -> dict:
        alert = {
            "status": "firing",
            "labels": {
                "alertname": "DemoBug",
                "severity": "critical",
                "repository": "demo",
                "service": "demo-api",
            },
            "annotations": {
                "summary": f"{self.env['bug']} in production",
                "description": f"traceback points at app.py ({self.env['bug']})",
            },
            "startsAt": "2026-10-04T10:00:00Z",
            "fingerprint": f"demo-fp-{self.env['bug']}",
        }
        alert.update(overrides)
        return {
            "version": "4", "status": "firing", "alerts": [alert],
            "commonLabels": {"alertname": "DemoBug"},
            "groupKey": "{}:{alertname='DemoBug'}",
        }


# =========================================================================
# 1. 端到端：三类 bug 各自走完 告警 → PR → CI 绿 → 等待人工 review
# =========================================================================

class TestEndToEnd:
    @pytest.mark.parametrize("bug", ["null_deref", "config_error", "unhandled_timeout"])
    @pytest.mark.asyncio
    async def test_alert_to_green_ci_pr(self, tmp_path: Path, bug: str):
        """M1 验收标准 1：demo 三类 bug 全部走通（要求至少两类）。"""
        env = make_demo_environment(tmp_path, bug)
        async with E2EStack(tmp_path, env) as stack:
            response = await stack.client.post("/webhook/alert", json=stack.alert())
            assert response.status == 202
            body = await response.json()
            assert len(body["accepted"]) == 1
            job_id = body["accepted"][0]["id"]

            await stack.settle()

            job = await stack.store.get_or_raise(job_id)
            assert job.status == "human_review", f"unexpected status {job.status}: {job.last_error}"
            assert job.ci_status == "success"
            assert job.pr_url.startswith("https://github.com/acme/demo/pull/")

            # PR 内容由服务层结构化拼装
            assert stack.github.prs, "no PR was created"
            pr = stack.github.prs[0]
            assert pr["base"] == "main"                    # 目标分支 = 配置的 base
            assert "## 告警摘要" in pr["body"]
            assert "## 根因分析" in pr["body"]
            assert "## 测试证据" in pr["body"]
            assert bug in pr["body"]

            # 远端真的多了一个修复分支，base 分支没被动过
            refs = subprocess.run(
                ["git", "ls-remote", str(env["bare"])], capture_output=True, text=True
            ).stdout
            assert f"refs/heads/mewfix/{job_id}" in refs
            assert _git(env["checkout"], "rev-parse", "origin/main").stdout == \
                _git(env["checkout"], "rev-parse", "main").stdout

            # 审计：整条状态链可追溯，远端动作有记录
            events = await stack.store.events(job_id)
            details = " ".join(e.detail for e in events)
            for state in ("triaging", "reproducing", "fixing", "verifying", "pr_opened", "ci_gate", "human_review"):
                assert state in details, f"missing transition to {state}"
            assert "pr_created" in {e.kind for e in events}

            # 通知：受理 + PR + CI 绿
            phases = [p for p, _ in stack.notifier.sent]
            assert "received" in phases and "pr_opened" in phases and "human_review" in phases

    @pytest.mark.asyncio
    async def test_prompt_carries_alert_evidence(self, tmp_path: Path):
        env = make_demo_environment(tmp_path, "config_error")
        async with E2EStack(tmp_path, env) as stack:
            await stack.client.post("/webhook/alert", json=stack.alert())
            await stack.settle()
            assert "config_error" in stack.runner.calls[0]
            assert "app.py" in stack.runner.calls[0]


# =========================================================================
# 2. 失败路径：不产生垃圾 PR
# =========================================================================

class TestFailurePaths:
    @pytest.mark.asyncio
    async def test_empty_payload_escalates_without_pr(self, tmp_path: Path):
        env = make_demo_environment(tmp_path, "config_error")
        async with E2EStack(tmp_path, env) as stack:
            response = await stack.client.post("/webhook/manual", json={"repo": "demo"})
            assert response.status == 202
            job_id = (await response.json())["accepted"][0]["id"]
            await stack.settle()

            job = await stack.store.get_or_raise(job_id)
            assert job.status == "escalate"
            assert "insufficient alert context" in job.last_error
            assert stack.github.prs == []          # 没有垃圾 PR
            assert stack.runner.calls == []        # 没跑 agent

    @pytest.mark.asyncio
    async def test_unroutable_alert_creates_no_job(self, tmp_path: Path):
        env = make_demo_environment(tmp_path, "config_error")
        async with E2EStack(tmp_path, env) as stack:
            alert = stack.alert(labels={"alertname": "X", "repository": "ghost"})
            response = await stack.client.post("/webhook/alert", json=alert)
            assert response.status == 422
            body = await response.json()
            assert body["skipped"] and "ghost" in body["skipped"][0]
            assert await stack.store.list_jobs() == []
            assert stack.github.prs == []

    @pytest.mark.asyncio
    async def test_ci_red_escalates_with_audit_trail(self, tmp_path: Path):
        """CI 一直红：带失败信息重试到上限后 escalate，每次尝试都有痕迹。"""
        env = make_demo_environment(tmp_path, "config_error")

        class AlwaysChangingRunner(ScriptedRunner):
            """每次都真的修好 bug、但解法略有不同（真实场景里 agent 会不断换解法），
            本地验证通过而远端 CI 始终红。"""

            async def run(self, job, work_dir, prompt, on_event):
                self.calls.append(prompt)
                source = Path(work_dir) / "app.py"
                text = source.read_text(encoding="utf-8")
                buggy, fixed, _ = DEMO_BUGS[self.bug]
                if buggy in text:
                    text = text.replace(buggy, fixed)
                source.write_text(text + f"\n# attempt {len(self.calls)}\n", encoding="utf-8")
                return AgentRunOutcome(final_text=f"ROOT CAUSE: try {len(self.calls)}\nFIX: attempt")

        async with E2EStack(
            tmp_path, env, runner=AlwaysChangingRunner(env["bug"]), ci_states=["failure"]
        ) as stack:
            response = await stack.client.post("/webhook/alert", json=stack.alert())
            job_id = (await response.json())["accepted"][0]["id"]
            await stack.settle()

            job = await stack.store.get_or_raise(job_id)
            assert job.status == "escalate"
            assert job.attempts == stack.store.max_fix_attempts
            # agent 尝试次数被上限卡住（第 4 次转移被状态机拒绝）
            assert len(stack.runner.calls) == stack.store.max_fix_attempts
            events = await stack.store.events(job_id)
            # 每次 CI 红都留下痕迹（最后一轮重试被上限拒绝时也记录了失败事实）
            assert sum(1 for e in events if e.kind == "ci_retry") == stack.store.max_fix_attempts
            assert any("CI failed" in e.detail for e in events)
            # 升级通知带上了审计摘要
            escalation = [d for p, d in stack.notifier.sent if p == "escalated"]
            assert escalation


# =========================================================================
# 3. 稳定性：去重 / 重启恢复 / 超时收敛
# =========================================================================

class TestStability:
    @pytest.mark.asyncio
    async def test_duplicate_alert_deduped_end_to_end(self, tmp_path: Path):
        env = make_demo_environment(tmp_path, "config_error")
        async with E2EStack(tmp_path, env) as stack:
            payload = stack.alert()
            first = await (await stack.client.post("/webhook/alert", json=payload)).json()
            second = await (await stack.client.post("/webhook/alert", json=payload)).json()
            assert len(first["accepted"]) == 1
            assert len(second["deduped"]) == 1
            assert second["deduped"][0]["id"] == first["accepted"][0]["id"]
            await stack.settle()
            assert len(await stack.store.list_jobs()) == 1
            assert len(stack.github.prs) == 1

    @pytest.mark.asyncio
    async def test_restart_recovers_unfinished_job(self, tmp_path: Path):
        """M1 验收标准 3：服务重启后未完结 job 从最后状态恢复并跑完。

        第一次运行让 agent 卡住（模拟部署/崩溃中断），关闭服务；第二次启动走
        真实的 requeue_unfinished -> reset_for_recovery -> 重跑执行链。
        """
        env = make_demo_environment(tmp_path, "config_error")

        class HangingRunner(ScriptedRunner):
            async def run(self, job, work_dir, prompt, on_event):
                await asyncio.sleep(300)

        service = build_service_config(tmp_path, env, drain_timeout_seconds=1)
        async with E2EStack(tmp_path, env, runner=HangingRunner(env["bug"]), service=service) as stack1:
            response = await stack1.client.post("/webhook/alert", json=stack1.alert())
            job_id = (await response.json())["accepted"][0]["id"]
            for _ in range(200):
                await asyncio.sleep(0.05)
                if (await stack1.store.get_or_raise(job_id)).status == "fixing":
                    break
            assert (await stack1.store.get_or_raise(job_id)).status == "fixing"

        async with E2EStack(tmp_path, env, service=service) as stack2:
            await stack2.runtime.start(recover=True)     # 与 `mewcode serve` 启动路径一致
            await stack2.settle(timeout=90)

            job = await stack2.store.get_or_raise(job_id)
            assert job.status == "human_review", f"not recovered: {job.status} / {job.last_error}"
            events = await stack2.store.events(job_id)
            kinds = {e.kind for e in events}
            assert "interrupted" in kinds
            assert "recovery_reset" in kinds
            assert "recovered" in kinds
            assert len(stack2.github.prs) == 1

    @pytest.mark.asyncio
    async def test_hanging_agent_times_out_and_escalates(self, tmp_path: Path):
        env = make_demo_environment(tmp_path, "config_error")

        class HangingRunner(ScriptedRunner):
            async def run(self, job, work_dir, prompt, on_event):
                await asyncio.sleep(300)

        service = build_service_config(tmp_path, env, job_timeout_seconds=1, drain_timeout_seconds=1)
        async with E2EStack(tmp_path, env, runner=HangingRunner(env["bug"]), service=service) as stack:
            response = await stack.client.post("/webhook/alert", json=stack.alert())
            job_id = (await response.json())["accepted"][0]["id"]
            await stack.settle(timeout=60)

            job = await stack.store.get_or_raise(job_id)
            assert job.status == "escalate"
            assert "timeout" in job.last_error
            assert stack.github.prs == []


# =========================================================================
# 4. 安全不变式
# =========================================================================

class TestSecurityInvariants:
    @pytest.mark.asyncio
    async def test_no_merge_capability_anywhere(self, tmp_path: Path):
        env = make_demo_environment(tmp_path, "config_error")
        async with E2EStack(tmp_path, env) as stack:
            for name in ("merge", "merge_pr", "approve", "merge_branch"):
                assert not hasattr(stack.vcs, name)

    @pytest.mark.asyncio
    async def test_alert_payload_cannot_redirect_base_branch(self, tmp_path: Path):
        """告警里的任何字段都不能改 PR 的目标分支。"""
        env = make_demo_environment(tmp_path, "config_error")
        async with E2EStack(tmp_path, env) as stack:
            alert = stack.alert()
            alert["alerts"][0]["annotations"]["base_branch"] = "attacker"
            alert["alerts"][0]["labels"]["base_branch"] = "attacker"
            await stack.client.post("/webhook/alert", json=alert)
            await stack.settle()
            assert stack.github.prs
            assert all(pr["base"] == "main" for pr in stack.github.prs)

    @pytest.mark.asyncio
    async def test_repo_state_never_committed_into_pr(self, tmp_path: Path):
        """服务自身状态（.mewcode）与缓存不得进入 PR 提交。"""
        env = make_demo_environment(tmp_path, "config_error")
        async with E2EStack(tmp_path, env) as stack:
            response = await stack.client.post("/webhook/alert", json=stack.alert())
            job_id = (await response.json())["accepted"][0]["id"]
            await stack.settle()

            branch = f"mewfix/{job_id}"
            files = _git(env["checkout"], "ls-tree", "-r", "--name-only", branch).stdout
            assert "app.py" in files
            assert ".mewcode" not in files
            assert "__pycache__" not in files
