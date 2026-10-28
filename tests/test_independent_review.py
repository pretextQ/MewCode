"""Pinned real Git fixtures and fake model transport; no credentials/network."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mewcode.client import LLMClient, create_client
from mewcode.config import ProviderConfig, RepoConfig, ReviewConfig, ServiceConfig, load_config
from mewcode.service.execution import AgentRunOutcome, ExecutionChain, ExecutionContext
from mewcode.service.execution import TestOutcome as Outcome
from mewcode.service.jobs import JobStore
from mewcode.service.metrics import build_job_report
from mewcode.service.publisher import GitHubCIGate, PullRequestPublisher, build_pr_body
from mewcode.service.review import ReviewService, review_history
from mewcode.service.review_diff import digest, git
from mewcode.service.review_prompt import REVIEW_SURFACES, render_input
from mewcode.service.review_render import render_review, review_reports
from mewcode.service.vcs import GitHubVCS, PRInfo, VCSError
from mewcode.tools.base import StreamEnd, TextDelta, ToolCallComplete
from mewcode.validator import ConfigError, validate_review, validate_service
from scripts.evaluate_reviews import CASES, evaluate


def run_git(repo, *args):
    return git(str(repo), list(args)).decode("utf-8").strip()


class FakeClient(LLMClient):
    def __init__(self):
        self.events = [TextDelta('{"summary":"scoped assessment","findings":[]}'), StreamEnd("end_turn", 100, 20, 3)]
        self.requests = []
        self.closed = False
        self.delay = 0

    async def stream(self, conversation, system="", tools=None):
        self.requests.append((conversation, system, tools))
        if self.delay:
            await asyncio.sleep(self.delay)
        for event in self.events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def close(self):
        self.closed = True


@pytest_asyncio.fixture
async def env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-b", "master")
    run_git(repo, "config", "user.email", "test@example.invalid")
    run_git(repo, "config", "user.name", "Test")
    (repo / "a.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "guard.py").write_text("if not authorized:\n    raise PermissionError()\n", encoding="utf-8")
    (repo / "旧 name.txt").write_text("unchanged\n" * 20, encoding="utf-8")
    (repo / "blob.bin").write_bytes(b"\0old")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-m", "baseline")
    base = run_git(repo, "rev-parse", "HEAD")
    run_git(repo, "checkout", "-b", "fix")
    (repo / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    run_git(repo, "commit", "-am", "fix")
    config = ServiceConfig(
        data_dir=str(tmp_path / "state"), review=ReviewConfig(enabled=True),
        repos={"demo": RepoConfig(name="demo", path=str(repo), base_branch="master")},
    )
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    job = await store.create_job(
        fingerprint="review-test", repo="demo", severity="high", title="wrong sum",
        payload={"summary": "wrong sum", "logs": "expected 5, got -1"},
    )
    context = ExecutionContext(
        repo=config.repos["demo"], work_dir=str(repo), prompt="fixer private history",
        agent=AgentRunOutcome(final_text="ROOT CAUSE: sign error\nFIX: changed operator"),
        changed_files=["a.py"], diff="legacy display diff", baseline_test=Outcome("pytest", 1, "failed"),
        verify_test=Outcome("pytest", 0, "passed"),
    )
    client = FakeClient()
    created = []

    def factory(provider):
        created.append(provider)
        return client

    reviewer = ReviewService(
        config, store, ProviderConfig("fixture", "openai", "http://unused.invalid", "fixture-model"),
        client_factory=factory,
    )
    value = SimpleNamespace(
        repo=repo, base=base, config=config, store=store, job=job, context=context,
        client=client, reviewer=reviewer, created=created,
    )
    try:
        yield value
    finally:
        await store.close()


async def review(env):
    return await env.reviewer.run(env.job, env.context, run_git(env.repo, "rev-parse", "HEAD"))


def response(env, findings):
    env.client.events[0] = TextDelta(json.dumps({"summary": "fixture", "findings": findings}))


def finding(**changes):
    return {"severity": "high", "path": "a.py", "side": "new", "line": 2,
            "title": "issue", "detail": "trigger and consequence", **changes}


@pytest.mark.asyncio
async def test_complete_review_is_pinned_isolated_and_accounted(env):
    result = await review(env)
    assert result.status == "no_findings" and result.coverage_complete
    assert result.base_sha == env.base
    conversation, system, tools = env.client.requests[0]
    assert len(conversation.history) == 1 and conversation.history[0].role == "user"
    assert "fixer private history" not in conversation.history[0].content
    assert tools == [] and "untrusted" in system
    assert not env.created[0].thinking and env.created[0].max_output_tokens == 2048
    assert env.client.closed
    for kind, path in result.artifacts.items():
        assert digest(Path(env.config.data_dir) / path) == result.hashes[kind]
    saved_request = (Path(env.config.data_dir) / result.artifacts["input"]).read_text(encoding="utf-8")
    assert saved_request == conversation.history[0].content
    assert len(saved_request.encode("utf-8")) <= env.config.review.max_input_bytes
    job = await env.store.get_or_raise(env.job.id)
    assert (job.tokens_in, job.tokens_out) == (103, 20)
    report = build_job_report(job, await env.store.events(job.id))["reviews"][0]
    assert report["head_sha"] == result.head_sha and report["budget_charge"] == 123


@pytest.mark.asyncio
async def test_removed_safety_check_rename_binary_and_full_evidence(env):
    (env.repo / "guard.py").unlink()
    run_git(env.repo, "mv", "旧 name.txt", "新 name.txt")
    (env.repo / "blob.bin").write_bytes(b"\0new")
    run_git(env.repo, "add", "-A")
    run_git(env.repo, "commit", "-m", "unsafe change")
    response(env, [finding(path="guard.py", side="old", line=1, title="removed authorization")])
    result = await review(env)
    assert result.status == "findings" and not result.coverage_complete
    files = {file.path: file for file in result.coverage}
    assert files["guard.py"].status == "full"
    assert files["blob.bin"].reason == "binary" and files["blob.bin"].status == "none"
    assert files["新 name.txt"].old_path == "旧 name.txt" and files["新 name.txt"].status == "full"
    raw = (Path(env.config.data_dir) / result.artifacts["diff"]).read_bytes()
    assert b"GIT binary patch" in raw and b"-if not authorized:" in raw


@pytest.mark.asyncio
async def test_large_diff_budget_never_claims_complete_review(env):
    (env.repo / "a.py").write_text("# 中文\\\"\t\n" * 10_000, encoding="utf-8")
    (env.repo / "z.py").write_text("important_later_change = True\n", encoding="utf-8")
    run_git(env.repo, "add", "-A")
    run_git(env.repo, "commit", "-m", "large diff")
    env.config.review.max_input_bytes = 2000
    result = await review(env)
    assert result.status == "incomplete" and result.reason == "partial_coverage"
    assert not result.coverage_complete
    assert any(file.status == "partial" for file in result.coverage)
    assert any(file.status == "none" for file in result.coverage)
    path = Path(env.config.data_dir) / result.artifacts["input"]
    assert path.stat().st_size <= 2000
    assert (Path(env.config.data_dir) / result.artifacts["diff"]).stat().st_size > 60_000


@pytest.mark.asyncio
@pytest.mark.parametrize(("events", "reason"), [
    ([TextDelta("not JSON"), StreamEnd("end_turn", 100, 20)], "review_error:ValidationError"),
    ([TextDelta('{"summary":"ok","findings":[],"approval":true}'), StreamEnd("end_turn")], "review_error:ValidationError"),
    ([TextDelta('{"summary":"ok","findings":[]}')], "incomplete_response"),
    ([TextDelta('{"summary":"ok","findings":[]}'), StreamEnd("max_tokens")], "incomplete_response"),
    ([ToolCallComplete("id", "Bash", {"command": "evil"})], "unexpected_tool_call"),
    ([RuntimeError("secret-key-in-error")], "review_error:RuntimeError"),
])
async def test_failure_is_incomplete_not_clean(env, events, reason):
    env.client.events = events
    result = await review(env)
    assert result.status == "incomplete" and result.reason == reason
    assert not result.coverage_complete and env.client.closed
    assert "secret-key-in-error" not in json.dumps(result.to_dict())
    assert len(await env.store.events(env.job.id)) == 3  # intake + started + finished


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"line": 1}, {"side": "old", "line": 99}, {"path": "unseen.py"}])
async def test_finding_must_anchor_sent_changed_line(env, changes):
    response(env, [finding(**changes)])
    result = await review(env)
    assert result.reason == "invalid_finding_location" and result.status == "incomplete"


@pytest.mark.asyncio
async def test_timeout_and_cancellation_do_not_look_clean(env):
    env.client.delay = 10
    env.config.review.timeout_seconds = 2
    result = await review(env)
    assert result.reason == "timeout" and result.budget_charge > 0
    assert env.client.closed
    env.config.review.timeout_seconds = 10
    env.config.review.token_budget = 100_000
    task = asyncio.create_task(review(env))
    while len(env.client.requests) < 2:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = review_reports(await env.store.events(env.job.id))
    assert records[-1]["reason"] == "cancelled"


@pytest.mark.asyncio
async def test_limits_skip_calls_and_reset_coverage(env):
    env.config.review.max_calls_per_job = 1
    await review(env)
    result = await review(env)
    assert result.reason == "call_limit" and len(env.client.requests) == 1
    assert all(file.status == "none" and file.sent_bytes == 0 for file in result.coverage)


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_double_spend_call_limit(env):
    env.config.review.max_calls_per_job = 1
    results = await asyncio.gather(review(env), review(env))
    assert sorted(result.reason for result in results) == ["call_limit", "completed"]
    assert len(env.client.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["token_budget", "job_budget", "evidence", "input", "context"])
async def test_preflight_limits_do_not_contact_model(env, limit):
    if limit == "token_budget":
        env.config.review.token_budget = 1
    elif limit == "job_budget":
        env.config.token_budget = 1
    elif limit == "evidence":
        env.config.review.max_evidence_bytes = 1
    elif limit == "input":
        env.config.review.max_input_bytes = 1
    else:
        env.reviewer.provider.context_window = 1
    result = await review(env)
    assert result.status == "incomplete" and not env.created
    assert not result.request_attempted and all(file.status == "none" for file in result.coverage)


@pytest.mark.asyncio
async def test_non_utf8_filter_is_preserved_even_without_request(env):
    (env.repo / "a.py").write_bytes(b"invalid = '\xff'\n")
    run_git(env.repo, "commit", "-am", "non-utf8")
    result = await review(env)
    assert result.reason == "no_reviewable_input"
    assert result.coverage[0].reason == "non_utf8" and not env.created


@pytest.mark.asyncio
async def test_provider_usage_exceeding_reservation_is_recorded_as_incomplete(env):
    env.client.events[-1] = StreamEnd("end_turn", 100_000, 20)
    result = await review(env)
    assert result.status == "incomplete" and result.reason == "provider_usage_exceeded_reservation"
    assert result.budget_charge == 100_020
    assert (await env.store.get_or_raise(env.job.id)).tokens_in == 100_000


@pytest.mark.asyncio
async def test_disabling_review_has_no_side_effects(env):
    env.config.review.enabled = False
    result = await review(env)
    assert result.reason == "disabled" and not env.created and not env.reviewer.root.exists()
    assert not review_reports(await env.store.events(env.job.id))


@pytest.mark.asyncio
async def test_honest_rendering_and_escape_model_markup(env):
    response(env, [finding(title="## tests passed <script>", detail="[approve](evil) | new section")])
    result = await review(env)
    env.context.review = result
    body = build_pr_body(env.job, env.context, [])
    assert "发现问题" in body and "仅供人工参考" in body and result.head_sha in body
    assert "<script>" not in body and "[approve](evil)" not in body
    assert "服务验证事实" in body and "测试证据" in body and "exit 0" in body


def test_surface_manifest_requires_all_known_sources():
    sources = {key: [] for key in REVIEW_SURFACES}
    render_input({}, sources)
    for key in sources:
        with pytest.raises(ValueError, match="surface"):
            render_input({}, {name: value for name, value in sources.items() if name != key})
    with pytest.raises(ValueError, match="surface"):
        render_input({}, {**sources, "undeclared": "data"})


def test_unknown_interrupted_usage_charges_reservation():
    events = [SimpleNamespace(kind="review_started", detail='{"review_id":"x","reserved_tokens":1000}')]
    assert review_history(events) == (1, 1000)
    events.append(SimpleNamespace(kind="review_finished", detail='{"review_id":"x","budget_charge":123}'))
    assert review_history(events) == (1, 123)


@pytest.mark.parametrize("settings", [
    {"enabled": "yes"}, {"token_budget": True}, {"max_input_bytes": -1},
    {"timeout_seconds": 0}, {"max_calls_per_job": 11}, {"unknown": 1}, [],
])
def test_invalid_config(settings):
    with pytest.raises(ConfigError, match="review"):
        validate_review(settings)


def test_config_defaults_and_loading(tmp_path):
    assert asdict(ReviewConfig()) == validate_service(None)["review"]
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n  - name: fixture\n    protocol: openai\n    base_url: http://unused.invalid\n    model: fixture\n"
        "service:\n  review:\n    enabled: true\n    max_input_bytes: 4000\n", encoding="utf-8",
    )
    assert load_config(path).service.review == ReviewConfig(enabled=True, max_input_bytes=4000)


class LocalPublisherVCS(GitHubVCS):
    def __init__(self, config):
        super().__init__(config)
        self.pushed = []
        self.body = ""

    async def push(self, work_dir, branch, *, expected_sha=""):
        self.pushed.append(expected_sha or await self.head_sha(work_dir))

    async def resolve_repo_slug(self, work_dir):
        return "fixture/demo"

    async def find_open_pr(self, repo_slug, head_branch):
        return None

    async def create_pr(self, repo_slug, head_branch, title, body, base=""):
        self.body = body
        return PRInfo(1, "https://example.invalid/pull/1", head_branch, base)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_failure", [False, True])
async def test_publisher_reviews_exact_commit_and_does_not_block_on_opinion(env, model_failure):
    (env.repo / "a.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    if model_failure:
        env.client.events = [RuntimeError("fixture failure")]
    else:
        response(env, [finding(title="still wrong")])
    vcs = LocalPublisherVCS(env.config.vcs)
    publisher = PullRequestPublisher(vcs, env.store, env.config, env.reviewer)
    await publisher.publish(env.job, env.context)
    assert vcs.pushed == [env.context.review.head_sha]
    assert env.context.review.head_sha in vcs.body
    assert env.context.review.status == ("incomplete" if model_failure else "findings")
    assert len(env.client.requests) == 1


@pytest.mark.asyncio
async def test_publisher_refuses_head_changed_during_review(env):
    (env.repo / "a.py").write_text("return_value = 1\n", encoding="utf-8")
    original = env.reviewer.run

    async def mutate_head(job, context, head):
        result = await original(job, context, head)
        run_git(env.repo, "commit", "--allow-empty", "-m", "external change")
        return result

    env.reviewer.run = mutate_head
    vcs = LocalPublisherVCS(env.config.vcs)
    with pytest.raises(VCSError, match="head changed"):
        await PullRequestPublisher(vcs, env.store, env.config, env.reviewer).publish(env.job, env.context)
    assert vcs.pushed == []


@pytest.mark.asyncio
async def test_ci_gate_polls_pinned_review_revision_even_if_head_moves(env):
    env.context.review = await review(env)
    pinned = env.context.review.head_sha
    run_git(env.repo, "commit", "--allow-empty", "-m", "later commit")

    class VCS:
        async def resolve_repo_slug(self, work_dir):
            return "fixture/demo"

        async def poll_checks(self, slug, sha):
            assert sha == pinned
            return "fixture-status"

    assert await GitHubCIGate(VCS()).wait(env.job, env.context, SimpleNamespace()) == "fixture-status"


@pytest.mark.asyncio
async def test_git_push_uses_literal_pinned_revision(env):
    vcs = GitHubVCS(env.config.vcs)
    commands = []
    head = run_git(env.repo, "rev-parse", "HEAD")

    async def token(*args):
        return "fixture-token"

    async def url(*args):
        return "https://example.invalid/fixture/demo.git"

    async def command(work_dir, args, **kwargs):
        commands.append(args)
        return 0, ""

    vcs.resolve_token, vcs.remote_url, vcs._git = token, url, command
    await vcs.push(str(env.repo), "mewfix/fixture", expected_sha=head)
    assert commands == [["push", "https://example.invalid/fixture/demo.git", f"{head}:refs/heads/mewfix/fixture"]]
    with pytest.raises(VCSError, match="invalid pinned"):
        await vcs.push(str(env.repo), "mewfix/fixture", expected_sha="--evil")


@pytest.mark.asyncio
async def test_pr_review_display_is_bounded_without_losing_stored_findings(env):
    result = await review(env)
    result.findings = [finding(detail="long detail " * 150) for _ in range(20)]
    rendered = "\n".join(render_review(result))
    assert len(rendered) < 12_100 and "PR 展示预算已用完" in rendered
    assert result.head_sha in rendered and len(result.findings) == 20


@pytest.mark.asyncio
async def test_enabled_chain_persists_review_and_uses_published_revision(env):
    class Runner:
        async def run(self, job, work_dir, prompt, on_event):
            (Path(work_dir) / "a.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            return AgentRunOutcome(final_text="ROOT CAUSE: wrong operator", input_tokens=10, output_tokens=5)

    class Tests:
        async def run(self, work_dir, command, timeout):
            return Outcome(command, 0, "fixture passed")

    env.config.repos["demo"].test_command = "fixture-tests"
    vcs = LocalPublisherVCS(env.config.vcs)
    chain = ExecutionChain(
        env.config, env.store, Runner(), test_runner=Tests(),
        publisher=PullRequestPublisher(vcs, env.store, env.config, env.reviewer),
    )
    await chain(env.job)
    job = await env.store.get_or_raise(env.job.id)
    assert job.status == "pr_opened" and (job.tokens_in, job.tokens_out) == (113, 25)
    await env.store.close()
    await env.store.connect()
    report = build_job_report(job, await env.store.events(job.id))
    assert report["reviews"][0]["head_sha"] == vcs.pushed[0]
    assert report["reviews"][0]["status"] == "no_findings"


@pytest.mark.asyncio
async def test_reviewer_infrastructure_exception_is_advisory(env):
    async def broken(*args):
        raise OSError("secret text must not be logged")

    env.reviewer.run = broken
    (env.repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    vcs = LocalPublisherVCS(env.config.vcs)
    await PullRequestPublisher(vcs, env.store, env.config, env.reviewer).publish(env.job, env.context)
    assert vcs.pushed and env.context.review.reason == "review_unavailable:OSError"
    assert "secret text" not in vcs.body
    assert review_reports(await env.store.events(env.job.id))[-1]["status"] == "incomplete"


@pytest.mark.asyncio
async def test_repository_token_policy_also_caps_review(env):
    directory = env.repo / ".mewcode"
    directory.mkdir()
    (directory / "policy.yaml").write_text("token_budget: 1\n", encoding="utf-8")
    assert (await review(env)).reason == "token_budget"
    assert not env.created


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "openai-compat", "anthropic"])
async def test_sdk_request_caps_without_connecting(protocol):
    client = create_client(ProviderConfig("fixture", protocol, "http://unused.invalid", "fixture", api_key="fake-key"))
    try:
        client.set_max_retries(0)
        client.set_max_output_tokens(512)
        assert client._client.max_retries == 0
        assert client.max_output_tokens == 512
    finally:
        await client.close()


CASES_DATA = json.loads(CASES.read_text(encoding="utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES_DATA, ids=lambda case: case["id"])
async def test_labeled_scenarios_exercise_contract_not_model_quality(env, case):
    # The response is scripted from the label. This verifies wiring/coverage,
    # deliberately NOT a measurement of a real model's accuracy.
    (env.repo / "a.py").write_text(case["before"], encoding="utf-8")
    run_git(env.repo, "add", "-A")
    run_git(env.repo, "commit", "--allow-empty", "-m", "case baseline")
    run_git(env.repo, "branch", "case-base")
    env.context.target_branch = "case-base"
    (env.repo / "a.py").write_text("# filler\n" * case.get("padding_lines", 0) + case["after"], encoding="utf-8")
    run_git(env.repo, "commit", "-am", "case change")
    if "anchor" in case:
        response(env, [finding(**case["anchor"], detail=case["label"])])
    result = await review(env)
    if case.get("padding_lines"):
        assert result.status == "incomplete" and result.reason == "partial_coverage"
    else:
        assert result.status == ("findings" if case["expected_issue"] else "no_findings")


def test_evaluation_does_not_hide_unreviewed_cases():
    predictions = {
        case["id"]: {"status": "incomplete", "coverage_complete": False, "findings": []}
        for case in CASES_DATA
    }
    metrics = evaluate(CASES_DATA, predictions)
    assert metrics["missed_positives"] == 4 and metrics["incomplete"] == 5
    assert metrics["true_negatives"] == 0 and metrics["unassessed_negatives"] == 1
    assert metrics["precision"] is None and metrics["recall"] == 0
    with pytest.raises(ValueError):
        evaluate(CASES_DATA, {})
