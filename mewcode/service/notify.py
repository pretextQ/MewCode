"""IM 通知（W5）：把关键节点推到达钉钉/企业微信/Slack。

设计要点：
- 一个通用 webhook 适配三种消息体（配置选一），不引入三家 SDK；
- 通知点收敛在"人需要知道的时刻"：job 收到、PR 开出、escalate（升级人工）、
  CI 绿（human_review）。agent 的中间步骤不推送——通知噪音会让人忽略它；
- 通知失败绝不影响修复主流程（这里自己吞掉异常并记日志）。
"""

from __future__ import annotations

import logging
from typing import Any

from mewcode.config import NotifyConfig

from .jobs import Job, JobStore
from .policy import RepoPolicyLoader

log = logging.getLogger(__name__)

#: 只推这些阶段；其余（fixing 等中间态）由 notifier 静默过滤
NOTIFY_PHASES = frozenset({"received", "pr_opened", "escalated", "human_review"})

_PHASE_LABEL = {
    "received": "🚨 收到告警",
    "fixing": "🔧 开始修复",
    "pr_opened": "📬 PR 已开出",
    "human_review": "✅ CI 通过，等待人工 review",
    "escalated": "⛔ 升级人工",
}


class NullNotifier:
    """未配置通知（type: none）时的空实现。"""

    async def notify_job_event(self, job: Job, phase: str, detail: str = "") -> None:
        return None


class WebhookNotifier:
    def __init__(
        self,
        config: NotifyConfig,
        store: JobStore | None = None,
        *,
        transport: Any = None,
    ) -> None:
        self.config = config
        self.store = store
        self._transport = transport

    # -- 消息构造 ---------------------------------------------------------

    def build_text(self, job: Job, phase: str, detail: str, extra_lines: list[str] | None = None) -> str:
        label = _PHASE_LABEL.get(phase, phase)
        lines = [
            f"{label}：{job.title or job.payload.get('alertname') or 'alert'}",
            f"仓库 `{job.repo}` | 级别 `{job.severity}` | 状态 `{job.status}`",
        ]
        if job.pr_url:
            lines.append(f"PR：{job.pr_url}")
        if detail:
            lines.append(f"说明：{detail}")
        if job.last_error:
            lines.append(f"错误：{job.last_error[:500]}")
        if extra_lines:
            lines.extend(extra_lines)
        lines.append(f"job `{job.id}`")
        return "\n".join(lines)

    def build_payload(self, title: str, text: str) -> dict[str, Any]:
        """按配置的消息格式组装 webhook body。"""
        kind = self.config.type
        if kind == "slack":
            return {"text": f"*{title}*\n{text}"}
        if kind == "dingtalk":
            # 钉钉 markdown 消息：title 是通知栏标题，text 是正文
            return {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{text}"}}
        if kind == "wecom":
            return {"msgtype": "markdown", "markdown": {"content": f"### {title}\n{text}"}}
        raise ValueError(f"unsupported notify type: {kind}")

    async def _escalation_summary(self, job: Job) -> list[str]:
        """escalate 时附上"已尝试的分析"——人接手时需要知道之前发生过什么。"""
        if self.store is None:
            return []
        try:
            events = await self.store.events(job.id)
        except Exception:  # pragma: no cover - 只读路径，失败就少给点上下文
            return []
        interesting = [
            e for e in events
            if e.kind in ("transition", "agent_finished", "baseline_tests", "verify_tests",
                          "test_delta", "ci_status", "verification_retry", "escalated")
        ]
        if not interesting:
            return []
        lines = ["", "已尝试的分析："]
        for event in interesting[-6:]:
            lines.append(f"- [{event.kind}] {event.detail[:180]}")
        return lines

    # -- 发送 -------------------------------------------------------------

    async def send(self, title: str, text: str) -> bool:
        if not self.config.webhook_url:
            log.warning("notify webhook_url is empty; skipping notification")
            return False
        import httpx

        payload = self.build_payload(title, text)
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=self.config.timeout_seconds) as client:
                response = await client.post(self.config.webhook_url, json=payload)
        except httpx.HTTPError as e:
            log.warning("notify request failed: %s", e)
            return False
        if response.status_code >= 400:
            log.warning("notify endpoint returned %s: %s", response.status_code, response.text[:200])
            return False
        return True

    async def notify_job_event(self, job: Job, phase: str, detail: str = "") -> None:
        if phase not in NOTIFY_PHASES:
            return
        extra = await self._escalation_summary(job) if phase == "escalated" else None
        title = _PHASE_LABEL.get(phase, phase)
        try:
            await self.send(title, self.build_text(job, phase, detail, extra))
        except Exception as e:  # 通知永远不能让主流程失败
            log.warning("notify failed for %s (%s): %s", job.id, phase, e)


def build_notifier(config: NotifyConfig, store: JobStore | None = None, transport: Any = None) -> Any:
    if config.type == "none" or not config.webhook_url:
        return NullNotifier()
    return WebhookNotifier(config, store, transport=transport)


class RepoPolicyNotifier:
    """按仓库策略路由通知渠道（M3 W2）：policy.notify > 服务级 notify。

    包装默认 notifier：job 所属仓库的策略声明了 notify 段时用策略渠道，
    否则走默认。策略读取失败时上抛 PolicyError——调用方对通知本就有
    catch-all（记 warning、不影响主流程），而"读不出策略就悄悄换渠道"
    等于把"策略失效"掩盖掉，宁可不发。
    """

    def __init__(
        self, default: Any, loader: RepoPolicyLoader, store: JobStore | None = None
    ) -> None:
        self._default = default
        self._loader = loader
        self._store = store

    def notifier_for(self, repo: str) -> Any:
        policy = self._loader.load(repo)  # PolicyError 原样上抛
        if policy.notify is None:
            return self._default
        return build_notifier(policy.notify, self._store)

    async def notify_job_event(self, job: Job, phase: str, detail: str = "") -> None:
        await self.notifier_for(job.repo).notify_job_event(job, phase, detail)
