"""M1 W5: 通知层测试（钉钉/企微/Slack 三种消息体 + 通知点收敛 + 失败容忍）。

通知不能成为新的故障源：webhook 挂掉、返回 500、URL 缺失都必须只记日志。
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from mewcode.config import NotifyConfig
from mewcode.service.jobs import Job, JobStore
from mewcode.service.notify import NullNotifier, WebhookNotifier, build_notifier

HOOK_URL = "https://hooks.example.com/services/x"


def make_job(pr_url: str = "") -> Job:
    return Job(
        id="job-abc123456789",
        fingerprint="fp-1",
        repo="demo",
        severity="critical",
        status="escalate",
        title="订单接口 5xx 突增",
        payload={"source": "alertmanager", "alertname": "HighErrorRate"},
        pr_url=pr_url,
        last_error="verification failed: pytest → exit 1",
    )


def capture_transport(captured: list[dict], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({
            "url": str(request.url),
            "body": json.loads(request.content),
            "content_type": request.headers.get("content-type", ""),
        })
        return httpx.Response(status, text="ok")

    return httpx.MockTransport(handler)


@asynccontextmanager
async def store_with_events(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.db")
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


# =========================================================================
# A. 消息体适配
# =========================================================================

class TestPayloadFormats:
    def test_slack_format(self):
        captured: list[dict] = []
        notifier = WebhookNotifier(
            NotifyConfig(type="slack", webhook_url=HOOK_URL), transport=capture_transport(captured)
        )
        payload = notifier.build_payload("标题", "正文")
        assert set(payload) == {"text"}
        assert "标题" in payload["text"] and "正文" in payload["text"]

    def test_dingtalk_format(self):
        notifier = WebhookNotifier(NotifyConfig(type="dingtalk", webhook_url=HOOK_URL))
        payload = notifier.build_payload("标题", "正文")
        assert payload["msgtype"] == "markdown"
        assert payload["markdown"]["title"] == "标题"
        assert "正文" in payload["markdown"]["text"]

    def test_wecom_format(self):
        notifier = WebhookNotifier(NotifyConfig(type="wecom", webhook_url=HOOK_URL))
        payload = notifier.build_payload("标题", "正文")
        assert payload["msgtype"] == "markdown"
        assert "正文" in payload["markdown"]["content"]

    def test_unknown_type_rejected(self):
        notifier = WebhookNotifier(NotifyConfig(type="telegram", webhook_url=HOOK_URL))
        with pytest.raises(ValueError, match="unsupported notify type"):
            notifier.build_payload("t", "b")

    @pytest.mark.asyncio
    async def test_send_posts_json(self):
        captured: list[dict] = []
        notifier = WebhookNotifier(
            NotifyConfig(type="slack", webhook_url=HOOK_URL), transport=capture_transport(captured)
        )
        assert await notifier.send("t", "b") is True
        assert captured[0]["url"] == HOOK_URL
        assert "application/json" in captured[0]["content_type"]


# =========================================================================
# B. 通知点收敛（噪音控制）
# =========================================================================

class TestNotificationPoints:
    @pytest.mark.asyncio
    async def test_documented_phases_sent(self, tmp_path: Path):
        captured: list[dict] = []
        async with store_with_events(tmp_path) as store:
            notifier = WebhookNotifier(
                NotifyConfig(type="wecom", webhook_url=HOOK_URL), store, transport=capture_transport(captured)
            )
            for phase in ("received", "pr_opened", "escalated", "human_review"):
                await notifier.notify_job_event(make_job("https://github.com/a/b/pull/1"), phase, "d")
        assert len(captured) == 4

    @pytest.mark.asyncio
    async def test_intermediate_phases_silent(self, tmp_path: Path):
        """fixing 等中间态不推送——噪音会让人忽略通知。"""
        captured: list[dict] = []
        async with store_with_events(tmp_path) as store:
            notifier = WebhookNotifier(
                NotifyConfig(type="slack", webhook_url=HOOK_URL), store, transport=capture_transport(captured)
            )
            for phase in ("fixing", "verifying", "triaging", "reproducing"):
                await notifier.notify_job_event(make_job(), phase, "d")
        assert captured == []

    @pytest.mark.asyncio
    async def test_pr_notification_carries_link(self, tmp_path: Path):
        captured: list[dict] = []
        async with store_with_events(tmp_path) as store:
            notifier = WebhookNotifier(
                NotifyConfig(type="slack", webhook_url=HOOK_URL), store, transport=capture_transport(captured)
            )
            await notifier.notify_job_event(make_job("https://github.com/a/b/pull/1"), "pr_opened", "opened")
        assert "https://github.com/a/b/pull/1" in captured[0]["body"]["text"]


# =========================================================================
# C. escalate 要带"已尝试的分析"
# =========================================================================

class TestEscalationSummary:
    @pytest.mark.asyncio
    async def test_escalation_includes_audit_digest(self, tmp_path: Path):
        captured: list[dict] = []
        async with store_with_events(tmp_path) as store:
            job = await store.create_job(fingerprint="fp", repo="demo", title="t", payload={})
            await store.transition(job.id, "triaging")
            await store.transition(job.id, "reproducing")
            await store.transition(job.id, "fixing")
            await store.add_event(job.id, "baseline_tests", "pytest -q → exit 1")
            await store.add_event(job.id, "agent_finished", "attempt=1 tool_calls=7 tokens_in=900")
            job = await store.transition(job.id, "escalate", reason="verification still failing")

            notifier = WebhookNotifier(
                NotifyConfig(type="slack", webhook_url=HOOK_URL), store, transport=capture_transport(captured)
            )
            await notifier.notify_job_event(job, "escalated", "verification still failing")

        text = captured[0]["body"]["text"]
        assert "已尝试的分析" in text
        assert "baseline_tests" in text
        assert "agent_finished" in text
        assert "verification still failing" in text

    @pytest.mark.asyncio
    async def test_escalation_without_store_still_sends(self):
        captured: list[dict] = []
        notifier = WebhookNotifier(
            NotifyConfig(type="slack", webhook_url=HOOK_URL), None, transport=capture_transport(captured)
        )
        await notifier.notify_job_event(make_job(), "escalated", "boom")
        assert len(captured) == 1


# =========================================================================
# D. 失败容忍
# =========================================================================

class TestFailureTolerance:
    @pytest.mark.asyncio
    async def test_http_error_is_swallowed(self):
        notifier = WebhookNotifier(
            NotifyConfig(type="slack", webhook_url=HOOK_URL),
            transport=capture_transport([], status=500),
        )
        assert await notifier.send("t", "b") is False  # 不抛异常

    @pytest.mark.asyncio
    async def test_network_error_is_swallowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host")

        notifier = WebhookNotifier(
            NotifyConfig(type="slack", webhook_url=HOOK_URL), transport=httpx.MockTransport(handler)
        )
        assert await notifier.send("t", "b") is False

    @pytest.mark.asyncio
    async def test_empty_url_is_skipped(self):
        notifier = WebhookNotifier(NotifyConfig(type="slack", webhook_url=""))
        assert await notifier.send("t", "b") is False

    @pytest.mark.asyncio
    async def test_notify_job_event_never_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise RuntimeError("unexpected")

        notifier = WebhookNotifier(
            NotifyConfig(type="slack", webhook_url=HOOK_URL), transport=httpx.MockTransport(handler)
        )
        await notifier.notify_job_event(make_job(), "pr_opened", "x")  # 不抛


class TestFactory:
    def test_none_type_gives_null_notifier(self):
        assert isinstance(build_notifier(NotifyConfig(type="none")), NullNotifier)

    def test_missing_url_gives_null_notifier(self):
        assert isinstance(build_notifier(NotifyConfig(type="slack", webhook_url="")), NullNotifier)

    def test_configured_gives_webhook_notifier(self):
        assert isinstance(
            build_notifier(NotifyConfig(type="wecom", webhook_url=HOOK_URL)), WebhookNotifier
        )

    @pytest.mark.asyncio
    async def test_null_notifier_is_noop(self):
        assert await NullNotifier().notify_job_event(make_job(), "pr_opened") is None


# =========================================================================
# E. 接入链路：受理即通知（runtime.intake）
# =========================================================================

class TestIntakeNotification:
    @pytest.mark.asyncio
    async def test_intake_notifies_received(self, tmp_path: Path):
        from mewcode.config import ServiceConfig
        from mewcode.service.runtime import ServiceRuntime
        from mewcode.service.triggers.base import JobDraft

        captured: list[dict] = []
        async with store_with_events(tmp_path) as store:
            notifier = WebhookNotifier(
                NotifyConfig(type="wecom", webhook_url=HOOK_URL), store, transport=capture_transport(captured)
            )
            runtime = ServiceRuntime(
                ServiceConfig(data_dir=str(tmp_path / "state")),
                handler=None,  # type: ignore[arg-type]
                store=store,
                notifier=notifier,
            )
            await runtime.intake([JobDraft(fingerprint="fp", repo="demo", title="5xx 突增")])

        assert len(captured) == 1
        assert "5xx 突增" in captured[0]["body"]["markdown"]["content"]

    @pytest.mark.asyncio
    async def test_intake_survives_broken_notifier(self, tmp_path: Path):
        from mewcode.config import ServiceConfig
        from mewcode.service.runtime import ServiceRuntime
        from mewcode.service.triggers.base import JobDraft

        class Boom:
            async def notify_job_event(self, job, phase, detail=""):
                raise RuntimeError("notifier exploded")

        async with store_with_events(tmp_path) as store:
            runtime = ServiceRuntime(
                ServiceConfig(data_dir=str(tmp_path / "state")),
                handler=None,  # type: ignore[arg-type]
                store=store,
                notifier=Boom(),
            )
            result = await runtime.intake([JobDraft(fingerprint="fp", repo="demo", title="t")])
        assert len(result.accepted) == 1   # 通知炸了也要受理成功
