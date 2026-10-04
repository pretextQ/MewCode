"""M1 W4: VCS 集成测试（分支/提交/推送/PR/CI 门禁）。

本地 git 动作打真实的 bare 仓库（相当于 origin），远端 REST 用
httpx.MockTransport 拦下——不需要网络也不需要 GitHub 账号。
安全红线（无 merge 能力、base 分支只能来自配置、token 不进 argv/日志）在这里守。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from mewcode.config import RepoConfig, ServiceConfig, VCSConfig
from mewcode.service.execution import AgentRunOutcome, ExecutionContext
from mewcode.service.execution import TestOutcome as _TestOutcome
from mewcode.service.jobs import JobStore
from mewcode.service.publisher import (
    COMMIT_EXCLUDES,
    PullRequestPublisher,
    build_pr_body,
    build_pr_title,
    filter_timeline,
    parse_agent_report,
)
from mewcode.service.vcs import (
    GitHubVCS,
    VCSAuthError,
    VCSError,
    redact,
)

FAKE_TOKEN = "ghp_" + "t" * 36


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """bare 仓库当作 origin；克隆出工作副本。"""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init")
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "T")
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "init")
    _git(work, "branch", "-M", "master")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-u", "origin", "master")
    return work


def vcs_config(**overrides) -> VCSConfig:
    cfg = dict(
        provider="github",
        token=FAKE_TOKEN,
        remote="origin",
        base_branch="master",
        ci_poll_interval_seconds=1,
        ci_timeout_seconds=5,
        ci_none_grace_seconds=1,
    )
    cfg.update(overrides)
    return VCSConfig(**cfg)


def api_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# =========================================================================
# A. 本地 git 动作
# =========================================================================

class TestLocalGit:
    @pytest.mark.asyncio
    async def test_ensure_branch_renames_worktree_branch(self, tmp_path: Path, origin_repo: Path):
        vcs = GitHubVCS(vcs_config(), transport=api_transport(lambda r: httpx.Response(200, json={})))
        # 造一个 worktree（服务里由 WorktreeManager 建）
        from mewcode.worktree import WorktreeManager

        manager = WorktreeManager(repo_root=str(origin_repo), symlink_directories=[])
        wt = await manager.create("job-0123456789ab")

        await vcs.ensure_branch(wt.path, "mewfix/job-0123456789ab")
        out = _git(Path(wt.path), "branch", "--show-current").stdout
        assert out.strip() == "mewfix/job-0123456789ab"

    @pytest.mark.asyncio
    async def test_ensure_branch_rejects_injection(self, origin_repo: Path):
        vcs = GitHubVCS(vcs_config(), transport=api_transport(lambda r: httpx.Response(200, json={})))
        for bad in ("--exec=rm -rf", "a b", "x..y", "", "a;b"):
            with pytest.raises(VCSError):
                await vcs.ensure_branch(str(origin_repo), bad)

    @pytest.mark.asyncio
    async def test_commit_all_commits_and_excludes_service_dirs(self, tmp_path: Path, origin_repo: Path):
        from mewcode.worktree import WorktreeManager

        manager = WorktreeManager(repo_root=str(origin_repo), symlink_directories=[])
        wt = await manager.create("job-0123456789ab")
        work_dir = Path(wt.path)

        (work_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (work_dir / ".mewcode" / "sessions").mkdir(parents=True, exist_ok=True)
        (work_dir / ".mewcode" / "sessions" / "s.jsonl").write_text("noise", encoding="utf-8")
        (work_dir / "__pycache__").mkdir(exist_ok=True)
        (work_dir / "__pycache__" / "x.pyc").write_text("noise", encoding="utf-8")

        vcs = GitHubVCS(vcs_config(), transport=api_transport(lambda r: httpx.Response(200, json={})))
        committed = await vcs.commit_all(work_dir.as_posix(), "fix: sign error", exclude=COMMIT_EXCLUDES)
        assert committed is True

        out = _git(work_dir, "show", "--name-only", "--pretty=format:").stdout
        assert "calc.py" in out
        assert ".mewcode" not in out
        assert "__pycache__" not in out

    @pytest.mark.asyncio
    async def test_commit_all_returns_false_without_changes(self, origin_repo: Path):
        from mewcode.worktree import WorktreeManager

        manager = WorktreeManager(repo_root=str(origin_repo), symlink_directories=[])
        wt = await manager.create("job-0123456789ab")
        vcs = GitHubVCS(vcs_config(), transport=api_transport(lambda r: httpx.Response(200, json={})))
        assert await vcs.commit_all(wt.path, "nothing", exclude=COMMIT_EXCLUDES) is False

    @pytest.mark.asyncio
    async def test_push_reaches_remote_branch(self, tmp_path: Path, origin_repo: Path):
        """推送到 bare origin 的 mewfix 分支——base 分支不受影响。"""
        from mewcode.worktree import WorktreeManager

        manager = WorktreeManager(repo_root=str(origin_repo), symlink_directories=[])
        wt = await manager.create("job-0123456789ab")
        work_dir = Path(wt.path)
        (work_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        bare = Path(_git(origin_repo, "remote", "get-url", "origin").stdout.strip())
        vcs = GitHubVCS(vcs_config(), transport=api_transport(lambda r: httpx.Response(200, json={})))
        # remote_url 是唯一注入点：本地 bare 仓库当远端，走真实 git push
        vcs.remote_url = lambda work_dir: _noop(str(bare))  # type: ignore[method-assign]
        vcs.resolve_token = lambda work_dir=None: _noop("")  # type: ignore[method-assign]

        await vcs.ensure_branch(work_dir.as_posix(), "mewfix/job-0123456789ab")
        await vcs.commit_all(work_dir.as_posix(), "fix: sign error", exclude=COMMIT_EXCLUDES)
        await vcs.push(work_dir.as_posix(), "mewfix/job-0123456789ab")

        refs = subprocess.run(
            ["git", "ls-remote", str(bare), "refs/heads/mewfix/job-0123456789ab"],
            capture_output=True, text=True,
        ).stdout
        assert "mewfix/job-0123456789ab" in refs
        # base 分支没有被改动
        base_sha = _git(origin_repo, "rev-parse", "origin/master").stdout.strip()
        assert base_sha == _git(origin_repo, "rev-parse", "master").stdout.strip()

    @pytest.mark.asyncio
    async def test_push_rejects_non_fast_forward(self, origin_repo: Path, monkeypatch):
        """远端分支被人推过 -> 不覆盖、报错交给人（分支是服务独占的）。"""
        vcs = GitHubVCS(vcs_config(), transport=api_transport(lambda r: httpx.Response(200, json={})))

        async def fake_git(cwd, args, askpass_token=""):
            if args and args[0] == "push":
                return 1, "! [rejected] HEAD -> mewfix/x (non-fast-forward)"
            return 0, ""

        monkeypatch.setattr(vcs, "_git", fake_git)
        monkeypatch.setattr(vcs, "resolve_token", lambda work_dir=None: _noop(FAKE_TOKEN))
        monkeypatch.setattr(vcs, "remote_url", lambda work_dir: _noop("https://github.com/acme/demo.git"))
        with pytest.raises(VCSError, match="refusing to overwrite"):
            await vcs.push(str(origin_repo), "mewfix/x")


async def _noop(value):
    return value


# =========================================================================
# B. GitHub REST（PR / CI）
# =========================================================================

def make_vcs(handler) -> GitHubVCS:
    return GitHubVCS(vcs_config(), transport=api_transport(handler))


class TestPullRequests:
    @pytest.mark.asyncio
    async def test_create_pr_uses_configured_base(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            captured["auth"] = request.headers.get("authorization", "")
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/acme/demo/pull/7"})

        vcs = make_vcs(handler)
        info = await vcs.create_pr("acme/demo", "mewfix/job-1", "title", "body")
        assert info.number == 7 and info.base_branch == "master"
        assert captured["body"]["base"] == "master"       # base 只能来自配置
        assert captured["body"]["head"] == "mewfix/job-1"
        assert captured["auth"].startswith("Bearer ")

    @pytest.mark.asyncio
    async def test_base_branch_cannot_be_overridden_by_caller(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(201, json={"number": 1, "html_url": "x"})

        vcs = make_vcs(handler)
        await vcs.create_pr("acme/demo", "mewfix/job-1", "t", "b")
        assert captured["body"]["base"] == "master"

    @pytest.mark.asyncio
    async def test_missing_base_branch_refuses(self):
        vcs = make_vcs(lambda r: httpx.Response(201, json={}))
        vcs.config.base_branch = ""
        with pytest.raises(VCSError, match="base_branch"):
            await vcs.create_pr("acme/demo", "b", "t", "b")

    @pytest.mark.asyncio
    async def test_auth_failure_surfaces_clearly(self):
        vcs = make_vcs(lambda r: httpx.Response(401, json={"message": "Bad credentials"}))
        with pytest.raises(VCSAuthError):
            await vcs.create_pr("acme/demo", "b", "t", "b")

    @pytest.mark.asyncio
    async def test_find_open_pr(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "head=acme%3Amewfix%2Fjob-1" in str(request.url)
            return httpx.Response(200, json=[{
                "number": 3, "html_url": "https://x/pull/3", "base": {"ref": "master"},
            }])

        vcs = make_vcs(handler)
        found = await vcs.find_open_pr("acme/demo", "mewfix/job-1")
        assert found is not None and found.number == 3

    @pytest.mark.asyncio
    async def test_find_open_pr_none(self):
        vcs = make_vcs(lambda r: httpx.Response(200, json=[]))
        assert await vcs.find_open_pr("acme/demo", "b") is None

    @pytest.mark.asyncio
    async def test_no_merge_capability_exists(self):
        """安全红线：合并永远是人做的——VCS 层不得存在 merge 能力。"""
        vcs = make_vcs(lambda r: httpx.Response(200, json={}))
        for name in ("merge", "merge_pr", "approve", "merge_branch"):
            assert not hasattr(vcs, name), f"VCS layer must not expose {name}"


class TestCIStatus:
    @pytest.mark.asyncio
    async def test_success_when_all_checks_pass(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"check_runs": [
                {"name": "pytest", "status": "completed", "conclusion": "success"},
                {"name": "ruff", "status": "completed", "conclusion": "success"},
            ]})

        status = await make_vcs(handler).poll_checks("acme/demo", "sha")
        assert status.state == "success" and not status.is_failure

    @pytest.mark.asyncio
    async def test_failure_reported_with_names(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"check_runs": [
                {"name": "pytest (windows)", "status": "completed", "conclusion": "failure"},
                {"name": "ruff", "status": "completed", "conclusion": "success"},
            ]})

        status = await make_vcs(handler).poll_checks("acme/demo", "sha")
        assert status.state == "failure"
        assert "pytest (windows)" in status.details

    @pytest.mark.asyncio
    async def test_pending_then_success(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json={"check_runs": [
                    {"name": "pytest", "status": "in_progress", "conclusion": None},
                ]})
            return httpx.Response(200, json={"check_runs": [
                {"name": "pytest", "status": "completed", "conclusion": "success"},
            ]})

        status = await make_vcs(handler).poll_checks("acme/demo", "sha")
        assert status.state == "success" and calls["n"] >= 2

    @pytest.mark.asyncio
    async def test_no_checks_after_grace_is_none(self):
        status = await make_vcs(lambda r: httpx.Response(200, json={"check_runs": []})).poll_checks(
            "acme/demo", "sha"
        )
        assert status.state == "none"

    @pytest.mark.asyncio
    async def test_timeout_is_pending_not_success(self):
        vcs = make_vcs(lambda r: httpx.Response(200, json={"check_runs": [
            {"name": "pytest", "status": "in_progress", "conclusion": None},
        ]}))
        vcs.config.ci_timeout_seconds = 2
        vcs.config.ci_poll_interval_seconds = 1
        status = await vcs.poll_checks("acme/demo", "sha")
        assert status.state == "pending"          # 超时 != 绿
        assert "did not conclude" in status.details


# =========================================================================
# C. PR body 结构化模板
# =========================================================================

def make_job_stub():
    from mewcode.service.jobs import Job

    return Job(
        id="job-abc123456789", fingerprint="fp-1", repo="demo", severity="critical",
        status="pr_opened", title="订单接口 5xx 突增", attempts=1,
        payload={
            "source": "alertmanager",
            "labels": {"alertname": "HighErrorRate", "service": "orders"},
            "annotations": {"description": "5xx 比例 12%", "runbook_url": "https://rb/x"},
        },
    )


def make_context(work_dir: str = "/tmp/wt") -> ExecutionContext:
    return ExecutionContext(
        repo=RepoConfig(name="demo", path="/srv/demo", test_command="pytest -q"),
        work_dir=work_dir,
        prompt="prompt",
        agent=AgentRunOutcome(
            final_text="ROOT CAUSE: 分母未做零保护\nFIX: 修正 add() 的符号\nVERIFICATION: pytest -q 通过",
            tool_calls=4, input_tokens=1000, output_tokens=200,
        ),
        changed_files=["calc.py", "tests/test_calc.py"],
        diff="--- a/calc.py\n+++ b/calc.py\n+    return a + b\n-    return a - b\n",
        baseline_test=_TestOutcome(command="pytest -q", exit_code=1, output="1 failed"),
        verify_test=_TestOutcome(command="pytest -q", exit_code=0, output="1 passed"),
        attempts=1,
    )


class TestPRBody:
    def test_body_has_all_required_sections(self):
        body = build_pr_body(make_job_stub(), make_context(), [("2026-10-04T10:00:00Z", "worktree_ready", "path=/wt")])
        for heading in ("## 告警摘要", "## 根因分析", "## 修复说明", "## 测试证据", "## 审计记录"):
            assert heading in body, f"missing section {heading}"
        assert "5xx 比例 12%" in body            # 告警证据
        assert "修正 add() 的符号" in body        # 根因/修复来自 agent 报告
        assert "1 failed" in body and "1 passed" in body  # 前后对比
        assert "人工 review" in body
        assert "没有" and "merge" in body

    def test_body_without_report_falls_back_to_raw_text(self):
        ctx = make_context()
        ctx.agent = AgentRunOutcome(final_text="只是随便一段总结")
        body = build_pr_body(make_job_stub(), ctx, [])
        assert "只是随便一段总结" in body

    def test_parse_agent_report(self):
        sections = parse_agent_report("ROOT CAUSE: a\nFIX: b\nVERIFICATION: c")
        assert sections == {"ROOT CAUSE": "a", "FIX": "b", "VERIFICATION": "c"}

    def test_parse_agent_report_with_markdown_headings(self):
        sections = parse_agent_report("### ROOT CAUSE: a\n### FIX: b")
        assert sections["ROOT CAUSE"] == "a" and sections["FIX"] == "b"

    def test_parse_agent_report_with_bold_headings(self):
        """加粗标题是模型最常见的写法；漏掉会让自查段落静默消失（真机踩到）。"""
        text = (
            "**ROOT CAUSE:** the call was unguarded\n\n"
            "**FIX:** wrapped it in try/except\n\n"
            "**VERIFICATION:** ran the test\n\n"
            "**SELF-CHECK:**\n- [x] errors handled\n- [ ] not met: no new test\n"
        )
        sections = parse_agent_report(text)
        assert sections["ROOT CAUSE"] == "the call was unguarded"
        assert sections["FIX"] == "wrapped it in try/except"
        assert sections["SELF-CHECK"].startswith("- [x] errors handled")

    def test_parse_agent_report_with_mixed_decorations(self):
        text = "ROOT CAUSE: a\n> **FIX:** b\n`VERIFICATION`: c"
        sections = parse_agent_report(text)
        assert sections == {"ROOT CAUSE": "a", "FIX": "b", "VERIFICATION": "c"}

    def test_title_includes_alert(self):
        assert build_pr_title(make_job_stub()).startswith("[MewCode]")


# =========================================================================
# D. 发布器（端到端到 PR，用 mock API）
# =========================================================================

class TestPublisher:
    @pytest.mark.asyncio
    async def test_publish_creates_pr_and_returns_events(self, tmp_path: Path, origin_repo: Path):
        from mewcode.worktree import WorktreeManager

        manager = WorktreeManager(repo_root=str(origin_repo), symlink_directories=[])
        wt = await manager.create("job-abc123456789")
        work_dir = Path(wt.path)
        (work_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, str(request.url)))
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.com/acme/demo/pull/7"})

        vcs = make_vcs(handler)
        vcs.resolve_repo_slug = lambda wd: _noop("acme/demo")  # type: ignore[method-assign]

        async def fake_push(work_dir, branch):
            return None

        vcs.push = fake_push  # type: ignore[method-assign]

        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(fingerprint="fp", repo="demo", title="t", payload={})
            publisher = PullRequestPublisher(vcs, store, ServiceConfig())
            ctx = make_context(work_dir.as_posix())
            result = await publisher.publish(job, ctx)

            assert result.pr_url == "https://github.com/acme/demo/pull/7"
            assert result.branch == f"mewfix/{job.id}"
            assert any(method == "POST" for method, _ in requests)
            assert any(kind == "pr_created" for kind, _ in result.extra_events)

            # 分支已重命名为发布分支，提交已产生
            out = _git(work_dir, "branch", "--show-current").stdout
            assert out.strip() == f"mewfix/{job.id}"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_publish_refreshes_existing_pr_on_retry(self, tmp_path: Path, origin_repo: Path):
        from mewcode.worktree import WorktreeManager

        manager = WorktreeManager(repo_root=str(origin_repo), symlink_directories=[])
        wt = await manager.create("job-abc123456789")
        work_dir = Path(wt.path)
        (work_dir / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            if request.method == "GET":
                return httpx.Response(200, json=[{
                    "number": 7, "html_url": "https://github.com/acme/demo/pull/7", "base": {"ref": "master"},
                }])
            return httpx.Response(200, json={})  # PATCH

        vcs = make_vcs(handler)
        vcs.resolve_repo_slug = lambda wd: _noop("acme/demo")  # type: ignore[method-assign]

        async def fake_push(work_dir, branch):
            return None

        vcs.push = fake_push  # type: ignore[method-assign]

        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(fingerprint="fp", repo="demo", title="t", payload={})
            publisher = PullRequestPublisher(vcs, store, ServiceConfig())
            result = await publisher.publish(job, make_context(work_dir.as_posix()))
            assert "PATCH" in methods          # 复用已有 PR，不重复开单
            assert "POST" not in methods
            assert any(kind == "pr_updated" for kind, _ in result.extra_events)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_publish_without_changes_raises(self, tmp_path: Path, origin_repo: Path):
        from mewcode.worktree import WorktreeManager

        manager = WorktreeManager(repo_root=str(origin_repo), symlink_directories=[])
        wt = await manager.create("job-abc123456789")

        vcs = make_vcs(lambda r: httpx.Response(200, json=[]))
        vcs.resolve_repo_slug = lambda wd: _noop("acme/demo")  # type: ignore[method-assign]

        async def fake_push(work_dir, branch):  # 不允许触发真实远端动作
            raise AssertionError("push must not be reached when there is nothing to commit")

        vcs.push = fake_push  # type: ignore[method-assign]

        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(fingerprint="fp", repo="demo", title="t", payload={})
            publisher = PullRequestPublisher(vcs, store, ServiceConfig())
            with pytest.raises(VCSError, match="nothing to commit"):
                await publisher.publish(job, make_context(wt.path))
        finally:
            await store.close()


# =========================================================================
# E. 凭证卫生
# =========================================================================

class TestCredentialHygiene:
    @pytest.mark.asyncio
    async def test_token_from_config_is_used(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"check_runs": []})

        vcs = make_vcs(handler)
        await vcs._collect_checks("acme/demo", "sha")
        assert FAKE_TOKEN in seen["auth"]

    @pytest.mark.asyncio
    async def test_error_output_is_redacted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=f"internal error token={FAKE_TOKEN}")

        vcs = make_vcs(handler)
        with pytest.raises(VCSError) as ei:
            await vcs.create_pr("acme/demo", "h", "t", "b")
        assert FAKE_TOKEN not in str(ei.value)
        assert "***" in str(ei.value)

    def test_redact_helper(self):
        assert redact(f"oops {FAKE_TOKEN}", [FAKE_TOKEN]) == "oops ***"
        assert redact("short", ["abc"]) == "short"   # 太短不当密钥处理

    @pytest.mark.asyncio
    async def test_no_token_anywhere_raises_clear_error(self):
        vcs = GitHubVCS(vcs_config(token=""), transport=api_transport(lambda r: httpx.Response(200, json={})))
        # 让凭证助手也查不到东西：直接断言异常类型与提示
        vcs._remote_transport = lambda wd: _noop(("https", "credential-host-with-no-creds.invalid"))  # type: ignore[method-assign]
        with pytest.raises(VCSAuthError):
            await vcs.resolve_token()


class TestPRBodyTimeline:
    """PR body 的审计表只放状态推进与验证证据，不放 agent 运行细节。"""

    def test_noise_events_filtered_out(self):
        timeline = [
            ("t1", "created", "status=received repo=demo"),
            ("t2", "agent_prompt", "You are an on-call engineer... (huge)"),
            ("t3", "agent_tool_use", "Bash: ls -la"),
            ("t4", "agent_usage", "in=1000 out=200"),
            ("t5", "transition", "fixing -> verifying: re-running tests"),
            ("t6", "verify_tests", "pytest -q → exit 0"),
            ("t7", "ci_status", "success: all checks passed"),
        ]
        body = build_pr_body(make_job_stub(), make_context(), timeline)
        assert "You are an on-call engineer" not in body
        assert "Bash: ls -la" not in body
        assert "in=1000 out=200" not in body
        assert "fixing -> verifying" in body
        assert "verify_tests" in body and "ci_status" in body

    def test_filter_timeline_whitelist(self):
        kept = filter_timeline([
            ("t", "agent_tool_use", "x"),
            ("t", "transition", "y"),
            ("t", "worktree_ready", "z"),
        ])
        assert [k for _, k, _ in kept] == ["transition", "worktree_ready"]
