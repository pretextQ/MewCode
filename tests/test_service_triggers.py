"""M1 W2: 触发适配器测试（Alertmanager / 手动）。

验收要点：告警字段→job 的映射不能含糊；不可路由的条目必须显式出现
在 skipped 里（"告警为什么没被修"要可回答）；fingerprint 缺省时仍可去重。
"""
from __future__ import annotations

import pytest

from mewcode.service.triggers import AlertmanagerAdapter, ManualAdapter, TriggerError

REPOS = {"demo": object(), "other": object()}


def alertmanager_payload(**overrides):
    alert = {
        "status": "firing",
        "labels": {
            "alertname": "HighErrorRate",
            "severity": "critical",
            "repository": "demo",
            "service": "orders",
        },
        "annotations": {
            "summary": "订单接口 5xx 突增",
            "description": "5xx 比例 12%，阈值 1%",
            "runbook_url": "https://runbook/orders-5xx",
        },
        "startsAt": "2026-10-04T10:00:00Z",
        "generatorURL": "http://prometheus/graph?g0.expr=...",
        "fingerprint": "fp-alertmanager-1",
    }
    alert.update(overrides)
    return {
        "version": "4",
        "status": "firing",
        "receiver": "mewcode",
        "groupKey": '{}:{alertname="HighErrorRate"}',
        "commonLabels": {"alertname": "HighErrorRate", "severity": "critical"},
        "commonAnnotations": {"summary": "订单接口 5xx 突增"},
        "externalURL": "http://alertmanager:9093",
        "alerts": [alert],
    }


# =========================================================================
# A. Alertmanager 映射
# =========================================================================

class TestAlertmanagerMapping:
    def test_core_fields_mapped(self):
        result = AlertmanagerAdapter(REPOS).parse(alertmanager_payload())
        assert len(result.drafts) == 1
        draft = result.drafts[0]
        assert draft.fingerprint == "fp-alertmanager-1"
        assert draft.repo == "demo"
        assert draft.title == "订单接口 5xx 突增"
        assert draft.severity == "critical"
        # 排查需要的上下文全部保留在 payload 里
        assert draft.payload["alertname"] == "HighErrorRate"
        assert draft.payload["annotations"]["runbook_url"] == "https://runbook/orders-5xx"
        assert draft.payload["group_key"].startswith("{")
        assert draft.payload["generator_url"].startswith("http://prometheus")
        assert result.skipped == []

    def test_labels_in_payload_for_prompt(self):
        draft = AlertmanagerAdapter(REPOS).parse(alertmanager_payload()).drafts[0]
        assert draft.payload["labels"]["service"] == "orders"
        assert draft.payload["starts_at"] == "2026-10-04T10:00:00Z"

    def test_severity_defaults_to_warning(self):
        payload = alertmanager_payload(labels={"alertname": "X", "repository": "demo"})
        draft = AlertmanagerAdapter(REPOS).parse(payload).drafts[0]
        assert draft.severity == "warning"

    def test_unknown_severity_downgraded_with_warning(self):
        payload = alertmanager_payload(labels={"alertname": "X", "repository": "demo", "severity": "P1"})
        result = AlertmanagerAdapter(REPOS).parse(payload)
        assert result.drafts[0].severity == "warning"
        assert any("unknown severity" in w for w in result.drafts[0].warnings)

    def test_title_falls_back_to_alertname(self):
        payload = alertmanager_payload(annotations={})
        assert AlertmanagerAdapter(REPOS).parse(payload).drafts[0].title == "HighErrorRate"

    def test_common_labels_used_for_repo(self):
        payload = alertmanager_payload(labels={"alertname": "X"})
        payload["commonLabels"] = {"repository": "demo"}
        result = AlertmanagerAdapter(REPOS).parse(payload)
        assert result.drafts[0].repo == "demo"

    def test_custom_repo_label(self):
        payload = alertmanager_payload(labels={"alertname": "X", "service_repo": "demo"})
        adapter = AlertmanagerAdapter(REPOS, repo_label="service_repo")
        assert adapter.parse(payload).drafts[0].repo == "demo"


# =========================================================================
# B. 只处理 firing；不可路由必须显式跳过
# =========================================================================

class TestAlertmanagerFiltering:
    def test_resolved_alert_skipped(self):
        payload = alertmanager_payload(status="resolved")
        result = AlertmanagerAdapter(REPOS).parse(payload)
        assert result.drafts == []
        assert any("resolved" in s for s in result.skipped)

    def test_unknown_repo_skipped_with_reason(self):
        payload = alertmanager_payload(labels={"alertname": "X", "repository": "ghost"})
        result = AlertmanagerAdapter(REPOS).parse(payload)
        assert result.drafts == []
        assert any("ghost" in s and "routing table" in s for s in result.skipped)

    def test_missing_repo_label_skipped_with_reason(self):
        payload = alertmanager_payload(labels={"alertname": "X"})
        result = AlertmanagerAdapter(REPOS).parse(payload)
        assert result.drafts == []
        assert any("no usable" in s for s in result.skipped)

    def test_single_repo_table_used_as_fallback(self):
        """单仓库部署：没有 repository label 也能落位（但进 payload 便于审计）。"""
        payload = alertmanager_payload(labels={"alertname": "X"})
        result = AlertmanagerAdapter({"solo": object()}).parse(payload)
        assert result.drafts[0].repo == "solo"

    def test_mixed_batch_partial_accept(self):
        payload = alertmanager_payload()
        payload["alerts"].append({
            "status": "firing",
            "labels": {"alertname": "Other", "repository": "other"},
            "annotations": {},
            "fingerprint": "fp-2",
        })
        payload["alerts"].append({"status": "resolved", "labels": {}, "annotations": {}})
        result = AlertmanagerAdapter(REPOS).parse(payload)
        assert [d.repo for d in result.drafts] == ["demo", "other"]
        assert len(result.skipped) == 1

    def test_non_dict_alert_entry_skipped(self):
        payload = alertmanager_payload()
        payload["alerts"] = ["nonsense", payload["alerts"][0]]
        result = AlertmanagerAdapter(REPOS).parse(payload)
        assert len(result.drafts) == 1
        assert any("not an object" in s for s in result.skipped)


# =========================================================================
# C. fingerprint 稳定性（去重的输入）
# =========================================================================

class TestFingerprint:
    def test_explicit_fingerprint_used(self):
        payload = alertmanager_payload()
        payload["alerts"][0]["fingerprint"] = "from-alertmanager"
        assert AlertmanagerAdapter(REPOS).parse(payload).drafts[0].fingerprint == "from-alertmanager"

    def test_missing_fingerprint_derived_stably(self):
        """无 fingerprint 字段时，相同 labels 必须得到相同指纹——否则去重失效。"""
        a = alertmanager_payload()
        b = alertmanager_payload()
        del a["alerts"][0]["fingerprint"]
        del b["alerts"][0]["fingerprint"]
        b["alerts"][0]["annotations"]["description"] = "变化的描述不应影响指纹"

        fa = AlertmanagerAdapter(REPOS).parse(a).drafts[0].fingerprint
        fb = AlertmanagerAdapter(REPOS).parse(b).drafts[0].fingerprint
        assert fa == fb
        assert fa.startswith("auto-")

    def test_different_labels_different_fingerprint(self):
        a = alertmanager_payload()
        b = alertmanager_payload(labels={"alertname": "X", "repository": "demo"})
        del a["alerts"][0]["fingerprint"]
        del b["alerts"][0]["fingerprint"]
        fa = AlertmanagerAdapter(REPOS).parse(a).drafts[0].fingerprint
        fb = AlertmanagerAdapter(REPOS).parse(b).drafts[0].fingerprint
        assert fa != fb


# =========================================================================
# D. 手动触发适配器
# =========================================================================

class TestManualAdapter:
    def test_minimal_payload(self):
        result = ManualAdapter(REPOS).parse({"repo": "demo", "title": "5xx 突增"})
        assert len(result.drafts) == 1
        draft = result.drafts[0]
        assert draft.repo == "demo"
        assert draft.title == "5xx 突增"
        assert draft.severity == "warning"
        assert draft.fingerprint.startswith("manual-")

    def test_full_payload(self):
        result = ManualAdapter(REPOS).parse({
            "repo": "demo",
            "title": "订单超时",
            "summary": "p99 3s（阈值 500ms）",
            "logs": "Traceback ... TimeoutError",
            "severity": "critical",
            "fingerprint": "manual-fixed-fp",
            "payload": {"trace_id": "abc123"},
        })
        draft = result.drafts[0]
        assert draft.severity == "critical"
        assert draft.fingerprint == "manual-fixed-fp"
        assert draft.payload["logs"].endswith("TimeoutError")
        assert draft.payload["extra"] == {"trace_id": "abc123"}

    def test_missing_repo_rejected(self):
        with pytest.raises(TriggerError, match="requires a 'repo'"):
            ManualAdapter(REPOS).parse({"title": "x"})

    def test_unknown_repo_rejected(self):
        with pytest.raises(TriggerError, match="unknown repo"):
            ManualAdapter(REPOS).parse({"repo": "ghost"})

    def test_non_object_extra_payload_rejected(self):
        with pytest.raises(TriggerError, match="must be an object"):
            ManualAdapter(REPOS).parse({"repo": "demo", "payload": "oops"})

    def test_empty_payload_is_allowed_through(self):
        """空告警必须能进来——由 triaging 阶段判"信息不足"并 escalate（验收标准 2）。"""
        result = ManualAdapter(REPOS).parse({"repo": "demo"})
        assert len(result.drafts) == 1
        assert result.drafts[0].title == ""
        assert result.drafts[0].payload["logs"] == ""

    def test_same_repo_title_same_fingerprint(self):
        a = ManualAdapter(REPOS).parse({"repo": "demo", "title": "same"}).drafts[0]
        b = ManualAdapter(REPOS).parse({"repo": "demo", "title": "same", "logs": "x"}).drafts[0]
        assert a.fingerprint == b.fingerprint

    def test_different_title_different_fingerprint(self):
        a = ManualAdapter(REPOS).parse({"repo": "demo", "title": "a"}).drafts[0]
        b = ManualAdapter(REPOS).parse({"repo": "demo", "title": "b"}).drafts[0]
        assert a.fingerprint != b.fingerprint


# =========================================================================
# E. HTTP 集成（skipped 可见性）
# =========================================================================

class TestAdapterHttpIntegration:
    @pytest.mark.asyncio
    async def test_unroutable_alerts_visible_in_response(self, tmp_path):

        from aiohttp.test_utils import TestClient, TestServer

        from mewcode.config import RepoConfig, ServiceConfig
        from mewcode.service.api import create_app
        from mewcode.service.jobs import JobStore
        from mewcode.service.runtime import ServiceRuntime
        from mewcode.service.triggers import build_adapters

        async def handler(job):
            return None

        service = ServiceConfig(
            data_dir=str(tmp_path / "state"),
            repos={"demo": RepoConfig(name="demo", path=str(tmp_path))},
        )
        store = JobStore(tmp_path / "jobs.db")
        runtime = ServiceRuntime(service, handler=handler, store=store)
        await runtime.start(recover=False)
        client = TestClient(TestServer(create_app(runtime, build_adapters(service))))
        await client.start_server()
        try:
            resp = await client.post("/webhook/alert", json={
                "alerts": [
                    {"status": "firing", "labels": {"alertname": "A", "repository": "ghost"},
                     "annotations": {}, "fingerprint": "fp-ghost"},
                    {"status": "firing", "labels": {"alertname": "B", "repository": "demo"},
                     "annotations": {"summary": "b"}, "fingerprint": "fp-b"},
                ],
            })
            assert resp.status == 202
            body = await resp.json()
            assert len(body["accepted"]) == 1
            assert len(body["skipped"]) == 1
            assert "ghost" in body["skipped"][0]
        finally:
            await client.close()
            await runtime.stop()

    @pytest.mark.asyncio
    async def test_all_unroutable_returns_422(self, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer

        from mewcode.config import RepoConfig, ServiceConfig
        from mewcode.service.api import create_app
        from mewcode.service.jobs import JobStore
        from mewcode.service.runtime import ServiceRuntime
        from mewcode.service.triggers import build_adapters

        async def handler(job):
            return None

        service = ServiceConfig(
            data_dir=str(tmp_path / "state"),
            repos={
                "demo": RepoConfig(name="demo", path=str(tmp_path)),
                "other": RepoConfig(name="other", path=str(tmp_path)),
            },
        )
        store = JobStore(tmp_path / "jobs.db")
        runtime = ServiceRuntime(service, handler=handler, store=store)
        await runtime.start(recover=False)
        client = TestClient(TestServer(create_app(runtime, build_adapters(service))))
        await client.start_server()
        try:
            resp = await client.post("/webhook/alert", json={
                "alerts": [{"status": "firing", "labels": {"alertname": "A"}, "annotations": {}}],
            })
            assert resp.status == 422
            assert await store.list_jobs() == []
        finally:
            await client.close()
            await runtime.stop()
