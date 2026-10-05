"""M3 W1: 指标与成本（/metrics、复盘报告、成本聚合）的测试。

数据面三块：JobStore 的 token 累计列（含旧库迁移）与状态轨迹读取；
metrics.py 的聚合/渲染（Prometheus 文本、单 job 复盘、成本报告）；
以及执行链是否真的把 token 用量记进 jobs 表（链级接线测试）。
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mewcode.config import RepoConfig, ServiceConfig
from mewcode.service.api import create_app
from mewcode.service.execution import AgentRunOutcome, ExecutionChain, PublishResult, TestRunner
from mewcode.service.jobs import JobNotFound, JobStore
from mewcode.service.metrics import (
    collect_metrics,
    cost_report,
    job_report,
    render_prometheus,
)
from mewcode.service.runtime import ServiceRuntime

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


async def seed_store(store: JobStore) -> tuple[str, str, str]:
    """造三个 job：merged（带 PR 与用量）、escalate、在途 verifying。"""
    j1 = await store.create_job(fingerprint="fp-1", repo="demo", severity="critical", title="bug one")
    for state in ("triaging", "reproducing", "fixing", "verifying", "pr_opened", "ci_gate", "human_review"):
        await store.transition(j1.id, state)
    await store.transition(j1.id, "merged", reason="approved by review")
    await store.add_usage(j1.id, 120, 80)
    await store.add_event(j1.id, "agent_tool_use", "Edit: calc.py")
    await store.add_event(j1.id, "agent_tool_use", "Bash: pytest -q")
    await store.add_event(j1.id, "agent_finished", "attempt=1 tool_calls=2 tokens_in=120 tokens_out=80")
    await store.add_event(j1.id, "agent_mcp_ready", "in-container: servers=logs,ci")
    await store.add_event(j1.id, "agent_mcp_used", "2 call(s): mcp_logs_query_logs")
    await store.add_event(j1.id, "baseline_tests", "failed: 1 test")
    await store.add_event(j1.id, "verify_tests", "passed: 1 test")
    await store.add_event(j1.id, "ci_status", "success: all checks passed")

    j2 = await store.create_job(fingerprint="fp-2", repo="other", title="bug two")
    await store.transition(j2.id, "escalate", reason="triaging failed: no context")
    await store.add_event(j2.id, "escalated", "triaging failed: no context")

    j3 = await store.create_job(fingerprint="fp-3", repo="demo", title="bug three")
    for state in ("triaging", "reproducing", "fixing", "verifying"):
        await store.transition(j3.id, state)
    return j1.id, j2.id, j3.id


# =========================================================================
# A. JobStore：token 累计列与状态轨迹
# =========================================================================

class TestJobStoreUsage:
    @pytest.mark.asyncio
    async def test_add_usage_accumulates(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(fingerprint="fp", repo="demo")
            await store.add_usage(job.id, 120, 80)
            await store.add_usage(job.id, 10, 5)
            final = await store.get_or_raise(job.id)
            assert final.tokens_in == 130
            assert final.tokens_out == 85
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_add_usage_ignores_zero(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(fingerprint="fp", repo="demo")
            await store.add_usage(job.id, 0, 0)
            final = await store.get_or_raise(job.id)
            assert (final.tokens_in, final.tokens_out) == (0, 0)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_add_usage_unknown_job_raises(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            with pytest.raises(JobNotFound):
                await store.add_usage("job-missing", 1, 1)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_usage_accounting_does_not_touch_updated_at(self, tmp_path: Path):
        """记账不是生命周期变化：状态轨迹的时间戳语义不能被挪动。"""
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(fingerprint="fp", repo="demo")
            await store.add_usage(job.id, 5, 5)
            final = await store.get_or_raise(job.id)
            assert final.updated_at == job.updated_at
        finally:
            await store.close()


class TestSchemaMigration:
    @pytest.mark.asyncio
    async def test_pre_w1_database_is_upgraded_and_rows_preserved(self, tmp_path: Path):
        """旧库（无 token 列）connect 后自动补列，已有行可读、可继续记账。"""
        db = tmp_path / "jobs.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE jobs ("
            " id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, repo TEXT NOT NULL,"
            " severity TEXT NOT NULL DEFAULT 'warning', title TEXT NOT NULL DEFAULT '',"
            " payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,"
            " attempts INTEGER NOT NULL DEFAULT 0, branch TEXT NOT NULL DEFAULT '',"
            " pr_url TEXT NOT NULL DEFAULT '', ci_status TEXT NOT NULL DEFAULT '',"
            " last_error TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO jobs (id, fingerprint, repo, status, created_at, updated_at)"
            " VALUES ('job-old', 'fp', 'demo', 'merged', '2026-01-01T00:00:00Z', '2026-01-01T00:01:00Z')"
        )
        conn.commit()
        conn.close()

        store = JobStore(db)
        await store.connect()
        try:
            old = await store.get_or_raise("job-old")
            assert old.status == "merged"
            assert (old.tokens_in, old.tokens_out) == (0, 0)
            new = await store.create_job(fingerprint="fp-2", repo="demo")
            await store.add_usage(new.id, 7, 3)
            final = await store.get_or_raise(new.id)
            assert (final.tokens_in, final.tokens_out) == (7, 3)
        finally:
            await store.close()


class TestTransitionHistory:
    @pytest.mark.asyncio
    async def test_parses_transitions_and_recovery(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(fingerprint="fp", repo="demo")
            await store.transition(job.id, "triaging")
            await store.transition(job.id, "reproducing")
            # 恢复只允许非终态回退（escalate 是终态，reset 会拒绝）
            await store.reset_for_recovery(job.id, reason="service restart")
            records = await store.transition_history()
            by_state = [(r.from_state, r.to_state, r.reason, r.kind) for r in records]
            assert ("received", "triaging", "", "transition") in by_state
            assert ("triaging", "reproducing", "", "transition") in by_state
            assert ("reproducing", "received", "service restart", "recovery_reset") in by_state
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_reason_containing_colon_and_arrow(self, tmp_path: Path):
        """reason 里有 ": " 或 " -> " 时不串位：只按第一个分隔符切。"""
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            job = await store.create_job(fingerprint="fp", repo="demo")
            await store.transition(job.id, "triaging", reason="retry: a -> b")
            records = await store.transition_history()
            assert len(records) == 1
            assert records[0].from_state == "received"
            assert records[0].to_state == "triaging"
            assert records[0].reason == "retry: a -> b"
        finally:
            await store.close()


# =========================================================================
# B. /metrics 聚合与 Prometheus 渲染
# =========================================================================

class TestCollectAndRender:
    @pytest.mark.asyncio
    async def test_collect_aggregates_from_store(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            await seed_store(store)
            snapshot = await collect_metrics(store)
            assert snapshot.jobs_by_status_repo[("merged", "demo")] == 1
            assert snapshot.jobs_by_status_repo[("verifying", "demo")] == 1
            assert snapshot.jobs_by_status_repo[("escalate", "other")] == 1
            # 时长只统计终态 job（在途的 verifying 不进直方图）
            assert len(snapshot.terminal_durations) == 2
            assert len(snapshot.pr_durations) == 1
            assert snapshot.merged_total == 1
            assert snapshot.escalate_total == 1
            assert snapshot.tokens_by_repo["demo"] == (120, 80)
            assert snapshot.tokens_by_repo["other"] == (0, 0)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_render_prometheus_text_format(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            await seed_store(store)
            snapshot = await collect_metrics(store)
            text = render_prometheus(snapshot, model="deepseek-chat")
            lines = text.splitlines()

            def series(name: str) -> list[str]:
                return [line for line in lines if line.startswith(name)]

            assert '# TYPE mewcode_jobs_total counter' in lines
            assert 'mewcode_jobs_total{status="merged",repo="demo"} 1' in series("mewcode_jobs_total{")
            assert 'mewcode_jobs_total{status="escalate",repo="other"} 1' in series("mewcode_jobs_total{")

            # 直方图三件套：bucket / sum / count（同秒内完成的 job 时长为 0）
            assert 'mewcode_job_duration_seconds_bucket{le="30"} 2' in lines
            assert 'mewcode_job_duration_seconds_bucket{le="+Inf"} 2' in lines
            assert 'mewcode_job_duration_seconds_count 2' in lines
            assert 'mewcode_alert_to_pr_seconds_count 1' in lines

            assert 'mewcode_fix_merged_total 1' in lines
            assert 'mewcode_escalate_total 1' in lines
            assert 'mewcode_token_cost_total{model="deepseek-chat",repo="demo"} 200' in lines
            assert 'mewcode_token_cost_total{model="deepseek-chat",repo="other"} 0' in lines
            assert text.endswith("\n")
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_render_without_model_uses_unknown(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            await seed_store(store)
            snapshot = await collect_metrics(store)
            text = render_prometheus(snapshot)
            assert 'mewcode_token_cost_total{model="unknown",repo="demo"}' in text
        finally:
            await store.close()


# =========================================================================
# C. 单 job 复盘报告
# =========================================================================

class TestJobReport:
    @pytest.mark.asyncio
    async def test_report_projects_full_audit_trail(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            j1, _, _ = await seed_store(store)
            report = await job_report(store, j1)

            assert report["job"]["id"] == j1
            assert report["job"]["status"] == "merged"
            assert report["job"]["tokens"] == {"input": 120, "output": 80, "total": 200}

            # 状态轨迹：从 received 一路到 merged，reason 被解析出来
            assert [t["to"] for t in report["trajectory"]] == [
                "triaging", "reproducing", "fixing", "verifying",
                "pr_opened", "ci_gate", "human_review", "merged",
            ]
            assert report["trajectory"][-1]["reason"] == "approved by review"

            # timing：同秒内完成 → 数值为 0 但有定义
            assert report["timing"]["alert_to_pr_seconds"] == 0.0
            assert report["timing"]["total_duration_seconds"] == 0.0
            assert report["timing"]["pr_opened_at"] is not None

            assert report["agent"]["attempts"][0]["tool_calls"] == 2
            assert report["agent"]["attempts"][0]["tokens_in"] == 120
            assert report["agent"]["tools"] == {"Edit": 1, "Bash": 1, "_total": 2}
            assert report["agent"]["prompt_count"] == 0
            assert report["agent"]["mcp"]["ready"][0]["detail"] == "in-container: servers=logs,ci"
            assert len(report["agent"]["mcp"]["used"]) == 1

            assert len(report["verification"]["tests"]) == 2
            assert len(report["verification"]["ci"]) == 1
            assert report["verification"]["integration"] == []
            assert report["escalations"] == []
            assert "not converted to currency" in report["notes"][0]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_report_of_in_flight_job_has_no_total_duration(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            _, _, j3 = await seed_store(store)
            report = await job_report(store, j3)
            assert report["job"]["status"] == "verifying"
            assert report["timing"]["alert_to_pr_seconds"] is None
            assert report["timing"]["total_duration_seconds"] is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_report_of_escalated_job_carries_reason(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            _, j2, _ = await seed_store(store)
            report = await job_report(store, j2)
            assert report["escalations"][0]["reason"] == "triaging failed: no context"
            assert report["timing"]["alert_to_pr_seconds"] is None
            assert report["timing"]["total_duration_seconds"] == 0.0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_report_unknown_job_raises(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            with pytest.raises(JobNotFound):
                await job_report(store, "job-missing")
        finally:
            await store.close()


# =========================================================================
# D. 成本聚合报告
# =========================================================================

class TestCostReport:
    @pytest.mark.asyncio
    async def test_aggregates_by_repo(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            await seed_store(store)
            report = await cost_report(store, model="deepseek-chat")
            assert report["unit"] == "tokens"
            assert report["model"] == "deepseek-chat"
            assert {"repo": "demo", "jobs": 2, "tokens_in": 120, "tokens_out": 80, "tokens_total": 200} in report["repos"]
            assert {"repo": "other", "jobs": 1, "tokens_in": 0, "tokens_out": 0, "tokens_total": 0} in report["repos"]
            assert report["total"] == {"jobs": 3, "tokens_in": 120, "tokens_out": 80, "tokens_total": 200}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_repo_filter(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            await seed_store(store)
            report = await cost_report(store, repo="demo")
            assert [r["repo"] for r in report["repos"]] == ["demo"]
            assert report["total"]["jobs"] == 2
        finally:
            await store.close()


# =========================================================================
# E. HTTP 端点
# =========================================================================

@asynccontextmanager
async def api_env(tmp_path: Path, *, model: str = "deepseek-chat"):
    async def _handler(job):
        await job_store.transition(job.id, "triaging")
        await job_store.transition(job.id, "escalate", reason="stub handler done")

    config = ServiceConfig(data_dir=str(tmp_path / "state"))
    job_store = JobStore(tmp_path / "jobs.db")
    runtime = ServiceRuntime(config, handler=_handler, store=job_store)
    await runtime.start(recover=False)
    client = TestClient(TestServer(create_app(runtime, {}, model=model)))
    await client.start_server()
    try:
        yield client, job_store
    finally:
        await client.close()
        await runtime.stop()


class TestMetricsEndpoints:
    @pytest.mark.asyncio
    async def test_metrics_content_type_and_body(self, tmp_path: Path):
        async with api_env(tmp_path) as (client, store):
            await seed_store(store)
            resp = await client.get("/metrics")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
            text = await resp.text()
            assert 'mewcode_jobs_total{status="merged",repo="demo"} 1' in text
            assert 'mewcode_token_cost_total{model="deepseek-chat",repo="demo"} 200' in text

    @pytest.mark.asyncio
    async def test_job_report_endpoint(self, tmp_path: Path):
        async with api_env(tmp_path) as (client, store):
            j1, _, _ = await seed_store(store)
            resp = await client.get(f"/jobs/{j1}/report")
            assert resp.status == 200
            body = await resp.json()
            assert body["job"]["id"] == j1
            assert body["job"]["tokens"]["total"] == 200

    @pytest.mark.asyncio
    async def test_job_report_unknown_is_404(self, tmp_path: Path):
        async with api_env(tmp_path) as (client, _):
            resp = await client.get("/jobs/job-nope/report")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_costs_endpoint_with_repo_filter(self, tmp_path: Path):
        async with api_env(tmp_path) as (client, store):
            await seed_store(store)
            resp = await client.get("/costs")
            assert resp.status == 200
            body = await resp.json()
            assert body["unit"] == "tokens"
            assert body["total"]["tokens_total"] == 200

            filtered = await (await client.get("/costs?repo=demo")).json()
            assert [r["repo"] for r in filtered["repos"]] == ["demo"]

    @pytest.mark.asyncio
    async def test_read_endpoints_open_without_token(self, tmp_path: Path):
        async with api_env(tmp_path) as (client, _):
            for path in ("/metrics", "/costs"):
                assert (await client.get(path)).status == 200


# =========================================================================
# F. 执行链接线：token 用量真的记进 jobs 表
# =========================================================================

class _ChainFakeRunner:
    async def run(self, job, work_dir, prompt, on_event):
        on_event({"type": "tool_use", "toolName": "Edit", "args": {"file_path": "calc.py"}})
        (Path(work_dir) / "calc.py").write_text(CALC_FIXED, encoding="utf-8")
        return AgentRunOutcome(
            final_text="ROOT CAUSE: sign error\nFIX: corrected add()",
            tool_calls=1,
            input_tokens=120,
            output_tokens=80,
        )


class _ChainFakePublisher:
    async def publish(self, job, context) -> PublishResult:
        return PublishResult(pr_url="https://github.com/acme/demo/pull/1", branch=f"mewfix/{job.id}")


class TestChainRecordsUsage:
    @pytest.mark.asyncio
    async def test_agent_run_tokens_reach_the_jobs_table(self, tmp_path: Path, demo_repo: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        try:
            service = ServiceConfig(
                data_dir=str(tmp_path / "state"),
                repos={
                    "demo": RepoConfig(
                        name="demo",
                        path=str(demo_repo),
                        test_command=f'"{sys.executable}" test_calc.py',
                    )
                },
            )
            chain = ExecutionChain(
                service,
                store,
                _ChainFakeRunner(),
                publisher=_ChainFakePublisher(),
                test_runner=TestRunner(),
            )
            job = await store.create_job(
                fingerprint="fp-1",
                repo="demo",
                severity="critical",
                title="wrong arithmetic",
                payload={"source": "manual", "summary": "add(2,3) returns -1", "logs": "AssertionError"},
            )
            await chain(job)

            final = await store.get_or_raise(job.id)
            assert final.status == "pr_opened"
            assert (final.tokens_in, final.tokens_out) == (120, 80)

            snapshot = await collect_metrics(store)
            assert snapshot.tokens_by_repo["demo"] == (120, 80)
            assert snapshot.merged_total == 0  # 只到 pr_opened，merged 是人工的事
            assert len(snapshot.pr_durations) == 1

            report = await job_report(store, job.id)
            assert report["agent"]["attempts"][0]["tokens_in"] == 120
            assert report["agent"]["tools"]["Edit"] == 1
            assert len(report["verification"]["tests"]) == 3  # baseline + verify + test_delta
        finally:
            await store.close()
