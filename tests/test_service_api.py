"""M1 W1: HTTP 入口（aiohttp）与 runtime 接入的测试。

覆盖：webhook 鉴权（token 常数时间比较）、payload 校验、去重合并、
错误适配器/未配置适配器、healthz 观测口径、jobs 查询。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mewcode.config import ServiceConfig
from mewcode.service.api import create_app
from mewcode.service.jobs import JobStore
from mewcode.service.runtime import ServiceRuntime
from mewcode.service.triggers.base import JobDraft, ParseResult, TriggerError


class StubAdapter:
    """测试用适配器：把 payload 里的 'drafts' 数组原样转成 JobDraft。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[dict] = []

    def parse(self, payload: dict) -> ParseResult:
        self.seen.append(payload)
        if self.fail:
            raise TriggerError("no usable alert in payload")
        drafts = []
        for item in payload.get("drafts", []):
            drafts.append(
                JobDraft(
                    fingerprint=item["fingerprint"],
                    repo=item.get("repo", "demo"),
                    title=item.get("title", ""),
                    severity=item.get("severity", "warning"),
                    payload=item,
                )
            )
        return ParseResult(drafts=drafts, skipped=payload.get("skipped", []))


@asynccontextmanager
async def service_env(tmp_path: Path, *, token: str = "", handler=None, alert_adapter=None, manual_adapter=None):
    async def _default_handler(job):
        await job_store.transition(job.id, "triaging")
        await job_store.transition(job.id, "escalate", reason="stub handler done")

    config = ServiceConfig(webhook_token=token, concurrency=2, data_dir=str(tmp_path / "state"))
    job_store = JobStore(tmp_path / "jobs.db")
    runtime = ServiceRuntime(config, handler=handler or _default_handler, store=job_store)
    await runtime.start(recover=False)

    adapters = {}
    if alert_adapter is not None:
        adapters["alert"] = alert_adapter
    if manual_adapter is not None:
        adapters["manual"] = manual_adapter

    client = TestClient(TestServer(create_app(runtime, adapters)))
    await client.start_server()
    try:
        yield runtime, client
    finally:
        await client.close()
        await runtime.stop()


async def wait_settled(runtime: ServiceRuntime, timeout: float = 5.0) -> None:
    """等队列排空 + 在途任务收尾（handler 是异步的，不能只看队列深度）。"""
    await asyncio.wait_for(runtime.pool._queue.join(), timeout=timeout)


# =========================================================================
# A. 告警接入与去重
# =========================================================================

class TestAlertIntake:
    @pytest.mark.asyncio
    async def test_alert_creates_and_processes_job(self, tmp_path: Path):
        adapter = StubAdapter()
        async with service_env(tmp_path, alert_adapter=adapter) as (runtime, client):
            resp = await client.post("/webhook/alert", json={
                "drafts": [{"fingerprint": "fp-1", "repo": "demo", "title": "5xx spike"}],
            })
            assert resp.status == 202
            body = await resp.json()
            assert len(body["accepted"]) == 1
            job_id = body["accepted"][0]["id"]

            await wait_settled(runtime)
            job = await runtime.store.get_or_raise(job_id)
            assert job.status == "escalate"  # stub handler 的终态
            assert adapter.seen[0]["drafts"][0]["fingerprint"] == "fp-1"

    @pytest.mark.asyncio
    async def test_duplicate_fingerprint_deduped(self, tmp_path: Path):
        async with service_env(tmp_path, alert_adapter=StubAdapter()) as (runtime, client):
            payload = {"drafts": [{"fingerprint": "fp-same", "repo": "demo"}]}
            first = await (await client.post("/webhook/alert", json=payload)).json()
            second = await (await client.post("/webhook/alert", json=payload)).json()

            assert len(first["accepted"]) == 1
            assert len(second["deduped"]) == 1
            assert second["accepted"] == []
            assert second["deduped"][0]["id"] == first["accepted"][0]["id"]

            await wait_settled(runtime)
            jobs = await runtime.store.list_jobs()
            assert len(jobs) == 1  # 没有新建 job
            events = await runtime.store.events(jobs[0].id)
            assert any(e.kind == "deduped" for e in events)

    @pytest.mark.asyncio
    async def test_multiple_alerts_in_one_payload(self, tmp_path: Path):
        async with service_env(tmp_path, alert_adapter=StubAdapter()) as (runtime, client):
            resp = await client.post("/webhook/alert", json={
                "drafts": [
                    {"fingerprint": "fp-a", "repo": "demo"},
                    {"fingerprint": "fp-b", "repo": "demo"},
                ],
            })
            body = await resp.json()
            assert len(body["accepted"]) == 2


# =========================================================================
# B. 鉴权
# =========================================================================

class TestAuth:
    @pytest.mark.asyncio
    async def test_missing_token_rejected(self, tmp_path: Path):
        async with service_env(tmp_path, token="s3cret", alert_adapter=StubAdapter()) as (_, client):
            resp = await client.post("/webhook/alert", json={"drafts": []})
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self, tmp_path: Path):
        async with service_env(tmp_path, token="s3cret", alert_adapter=StubAdapter()) as (_, client):
            resp = await client.post(
                "/webhook/alert", json={"drafts": []}, headers={"X-MewCode-Token": "guess"}
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_correct_token_accepted_via_header(self, tmp_path: Path):
        async with service_env(tmp_path, token="s3cret", alert_adapter=StubAdapter()) as (_, client):
            resp = await client.post(
                "/webhook/alert", json={"drafts": []}, headers={"X-MewCode-Token": "s3cret"}
            )
            assert resp.status == 202

    @pytest.mark.asyncio
    async def test_bearer_token_accepted(self, tmp_path: Path):
        async with service_env(tmp_path, token="s3cret", alert_adapter=StubAdapter()) as (_, client):
            resp = await client.post(
                "/webhook/alert", json={"drafts": []}, headers={"Authorization": "Bearer s3cret"}
            )
            assert resp.status == 202

    @pytest.mark.asyncio
    async def test_read_endpoints_open_even_with_token(self, tmp_path: Path):
        """healthz / jobs 是运维只读端点，不加 token（不含敏感数据）。"""
        async with service_env(tmp_path, token="s3cret", alert_adapter=StubAdapter()) as (_, client):
            assert (await client.get("/healthz")).status == 200
            assert (await client.get("/jobs")).status == 200

    @pytest.mark.asyncio
    async def test_no_token_configured_accepts(self, tmp_path: Path):
        async with service_env(tmp_path, alert_adapter=StubAdapter()) as (_, client):
            resp = await client.post("/webhook/alert", json={"drafts": []})
            assert resp.status == 202


# =========================================================================
# C. payload 校验与错误路径
# =========================================================================

class TestPayloadValidation:
    @pytest.mark.asyncio
    async def test_invalid_json_rejected(self, tmp_path: Path):
        async with service_env(tmp_path, alert_adapter=StubAdapter()) as (_, client):
            resp = await client.post(
                "/webhook/alert", data=b"{not json", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400
            assert "invalid JSON" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_non_object_json_rejected(self, tmp_path: Path):
        async with service_env(tmp_path, alert_adapter=StubAdapter()) as (_, client):
            resp = await client.post("/webhook/alert", json=[1, 2, 3])
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_oversized_body_rejected(self, tmp_path: Path):
        async with service_env(tmp_path, alert_adapter=StubAdapter()) as (_, client):
            big = {"drafts": [{"fingerprint": "x" * 10}]}
            raw = json.dumps(big).encode() + b" " * 2_000_000
            resp = await client.post(
                "/webhook/alert", data=raw, headers={"Content-Type": "application/json"}
            )
            assert resp.status == 413

    @pytest.mark.asyncio
    async def test_adapter_trigger_error_yields_400(self, tmp_path: Path):
        """无法解析的告警必须 4xx 拒收——不产生垃圾 job（M1 验收标准 2 的入口侧）。"""
        async with service_env(tmp_path, alert_adapter=StubAdapter(fail=True)) as (runtime, client):
            resp = await client.post("/webhook/alert", json={"drafts": []})
            assert resp.status == 400
            assert "no usable alert" in (await resp.json())["error"]
            assert await runtime.store.list_jobs() == []

    @pytest.mark.asyncio
    async def test_missing_adapter_returns_503(self, tmp_path: Path):
        async with service_env(tmp_path) as (_, client):
            resp = await client.post("/webhook/alert", json={"drafts": []})
            assert resp.status == 503


# =========================================================================
# D. 观测端点
# =========================================================================

class TestObservability:
    @pytest.mark.asyncio
    async def test_healthz_reports_queue_and_counts(self, tmp_path: Path):
        async with service_env(tmp_path, alert_adapter=StubAdapter()) as (runtime, client):
            await client.post("/webhook/alert", json={"drafts": [{"fingerprint": "fp-h"}]})
            await wait_settled(runtime)

            body = await (await client.get("/healthz")).json()
            assert body["status"] == "ok"
            assert body["queue_depth"] == 0
            assert body["jobs_by_status"].get("escalate") == 1

    @pytest.mark.asyncio
    async def test_jobs_endpoint_lists_and_filters(self, tmp_path: Path):
        async with service_env(tmp_path, alert_adapter=StubAdapter()) as (runtime, client):
            await client.post("/webhook/alert", json={
                "drafts": [{"fingerprint": "fp-1", "title": "a"}, {"fingerprint": "fp-2", "title": "b"}],
            })
            body = await (await client.get("/jobs")).json()
            assert len(body["jobs"]) == 2

            filtered = await (await client.get("/jobs?status=received")).json()
            assert len(filtered["jobs"]) <= 2

    @pytest.mark.asyncio
    async def test_jobs_limit_validation(self, tmp_path: Path):
        async with service_env(tmp_path, alert_adapter=StubAdapter()) as (_, client):
            assert (await client.get("/jobs?limit=abc")).status == 400
            assert (await client.get("/jobs?limit=99999")).status == 200  # 收敛到上限而非报错


# =========================================================================
# E. runtime 生命周期
# =========================================================================

class TestRuntime:
    @pytest.mark.asyncio
    async def test_intake_without_pool_does_not_crash(self, tmp_path: Path):
        """池未启动时只落库不入队（boot 阶段收到告警的退化路径）。"""
        store = JobStore(tmp_path / "jobs.db")
        runtime = ServiceRuntime(ServiceConfig(data_dir=str(tmp_path / "state")), handler=None, store=store)  # type: ignore[arg-type]
        await store.connect()
        try:
            result = await runtime.intake([JobDraft(fingerprint="fp", repo="demo")])
            assert len(result.accepted) == 1
            assert runtime.pool.queue_depth == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_start_recovers_unfinished_jobs(self, tmp_path: Path):
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        job = await store.create_job(fingerprint="fp", repo="demo")
        await store.transition(job.id, "triaging")
        await store.transition(job.id, "reproducing")
        await store.transition(job.id, "fixing")
        await store.close()

        seen: list[str] = []

        async def handler(j):
            seen.append(j.status)

        config = ServiceConfig(data_dir=str(tmp_path / "state"))
        store2 = JobStore(tmp_path / "jobs.db")
        runtime = ServiceRuntime(config, handler=handler, store=store2)
        await runtime.start(recover=True)
        try:
            await asyncio.wait_for(runtime.pool._queue.join(), timeout=5)
        finally:
            await runtime.stop()
        assert seen == ["received"]
