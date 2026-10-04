"""手动触发适配器：``POST /webhook/manual``。

给本机 demo 与测试用：直接用本地 JSON 描述一次告警，不需要真的接
Alertmanager。字段少而明确，缺省值尽量宽容（title 可为空 -> 由 runtime
在 triaging 阶段判"信息不足"并 escalate，这条路径是 M1 验收标准 2 的输入）。

示例::

    {
      "repo": "demo",                     # 路由表名，必填
      "title": "订单接口 5xx 突增",         # 可选
      "summary": "5xx 比例 12%（阈值 1%）",  # 可选
      "logs": "Traceback (most recent ...", # 可选：日志/堆栈片段
      "severity": "critical",              # 可选：critical/warning/info
      "fingerprint": "manual-orders-5xx",  # 可选：默认由 repo+title 派生
      "payload": {...}                     # 可选：任意附加结构化上下文
    }
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .alertmanager import VALID_SEVERITIES
from .base import JobDraft, ParseResult, TriggerError


class ManualAdapter:
    def __init__(self, repos: dict[str, Any]) -> None:
        self.repos = repos or {}

    def _fingerprint(self, repo: str, title: str, explicit: str) -> str:
        if explicit:
            return explicit
        canonical = json.dumps({"repo": repo, "title": title}, sort_keys=True, ensure_ascii=False)
        return "manual-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def parse(self, payload: dict[str, Any]) -> ParseResult:
        repo = payload.get("repo") or ""
        if not repo:
            raise TriggerError("manual trigger requires a 'repo' field")
        if repo not in self.repos:
            raise TriggerError(
                f"unknown repo '{repo}': add it to service.repos before triggering"
            )

        title = str(payload.get("title") or "")
        severity = str(payload.get("severity") or "warning")
        warnings: list[str] = []
        if severity not in VALID_SEVERITIES:
            warnings.append(f"unknown severity '{severity}', treated as 'warning'")
            severity = "warning"

        extra = payload.get("payload") or {}
        if not isinstance(extra, dict):
            raise TriggerError("'payload' must be an object when provided")

        draft = JobDraft(
            fingerprint=self._fingerprint(repo, title, str(payload.get("fingerprint") or "")),
            repo=repo,
            title=title,
            severity=severity,
            payload={
                "source": "manual",
                "summary": str(payload.get("summary") or ""),
                "logs": str(payload.get("logs") or ""),
                "extra": extra,
            },
            warnings=warnings,
        )
        return ParseResult(drafts=[draft], warnings=warnings)
