"""Alertmanager webhook 适配器。

把 Alertmanager 的 ``alerts[]`` 归一化为 JobDraft（payload 结构见
https://prometheus.io/docs/alerting/latest/configuration/#webhook_config）：
- 仓库取自 label（默认 ``repository``，可配 ``repo_label``），必须命中
  路由表 ``service.repos``——未命中即无法定位代码，记入 skipped；
- 只处理 ``status == "firing"`` 的告警（resolved 不触发修复）；
- 指纹优先用 Alertmanager 自带的 ``fingerprint``，缺失时用 labels 的稳定哈希
  兜底（去重语义不依赖上游是否实现了 fingerprint）。

适配器是纯转换：不落库、不访问网络，全部字段可单测。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .base import JobDraft, ParseResult, TriggerError

VALID_SEVERITIES = ("critical", "warning", "info")


class AlertmanagerAdapter:
    def __init__(self, repos: dict[str, Any], repo_label: str = "repository") -> None:
        self.repos = repos or {}
        self.repo_label = repo_label

    # -- 内部 -------------------------------------------------------------

    def _resolve_repo(self, labels: dict[str, str], common_labels: dict[str, str]) -> str:
        value = labels.get(self.repo_label) or common_labels.get(self.repo_label) or ""
        if value:
            return value
        # 单仓库部署的常见形态：路由表只有一个条目时可以直接落位
        if len(self.repos) == 1:
            return next(iter(self.repos))
        return ""

    def _fallback_fingerprint(self, labels: dict[str, str]) -> str:
        canonical = json.dumps(labels, sort_keys=True, ensure_ascii=False)
        return "auto-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    # -- 契约实现 ---------------------------------------------------------

    def parse(self, payload: dict[str, Any]) -> ParseResult:
        alerts = payload.get("alerts")
        if not isinstance(alerts, list):
            raise TriggerError("alertmanager payload must contain an 'alerts' list")

        common_labels = payload.get("commonLabels") or {}
        common_annotations = payload.get("commonAnnotations") or {}
        result = ParseResult()

        for index, alert in enumerate(alerts):
            if not isinstance(alert, dict):
                result.skipped.append(f"alert #{index}: not an object")
                continue

            labels = alert.get("labels") or {}
            annotations = alert.get("annotations") or {}
            status = alert.get("status", "firing")
            if status != "firing":
                result.skipped.append(f"alert #{index}: status={status} (only firing triggers a fix)")
                continue

            repo = self._resolve_repo(labels, common_labels)
            if repo and repo not in self.repos:
                result.skipped.append(
                    f"alert #{index}: repository '{repo}' is not in the service.repos routing table"
                )
                continue
            if not repo:
                result.skipped.append(
                    f"alert #{index}: no usable '{self.repo_label}' label and the routing table "
                    "cannot resolve one"
                )
                continue

            severity = labels.get("severity", "warning")
            warnings: list[str] = []
            if severity not in VALID_SEVERITIES:
                warnings.append(f"alert #{index}: unknown severity '{severity}', treated as 'warning'")
                severity = "warning"

            alertname = labels.get("alertname", "alert")
            drafts_payload = {
                "source": "alertmanager",
                "alertname": alertname,
                "severity": severity,
                "labels": labels,
                "annotations": annotations,
                "common_labels": common_labels,
                "common_annotations": common_annotations,
                "starts_at": alert.get("startsAt", ""),
                "generator_url": alert.get("generatorURL", ""),
                "external_url": payload.get("externalURL", ""),
                "group_key": payload.get("groupKey", ""),
            }

            result.drafts.append(
                JobDraft(
                    fingerprint=alert.get("fingerprint") or self._fallback_fingerprint(labels),
                    repo=repo,
                    title=annotations.get("summary") or alertname,
                    severity=severity,
                    payload=drafts_payload,
                    warnings=warnings,
                )
            )

        if not result.drafts and not result.skipped:
            raise TriggerError("alertmanager payload contains no alerts")

        return result
