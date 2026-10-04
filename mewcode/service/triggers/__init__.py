"""Trigger adapters：把各触发源的原始 payload 归一化为内部 Job 草案。

每个 adapter 是纯函数式转换（无 IO），因此可以单测到每个字段；
真正的落库/去重/入队由 :class:`mewcode.service.runtime.ServiceRuntime` 负责。
"""

from __future__ import annotations

from typing import Any

from .alertmanager import AlertmanagerAdapter
from .base import JobDraft, ParseResult, TriggerAdapter, TriggerError
from .manual import ManualAdapter

__all__ = [
    "AlertmanagerAdapter",
    "JobDraft",
    "ManualAdapter",
    "ParseResult",
    "TriggerAdapter",
    "TriggerError",
    "build_adapters",
]


def build_adapters(service_config: Any) -> dict[str, TriggerAdapter]:
    """按配置装配适配器（HTTP 入口按 source 名取用）。"""
    return {
        "alert": AlertmanagerAdapter(
            service_config.repos, repo_label=service_config.repo_label or "repository"
        ),
        "manual": ManualAdapter(service_config.repos),
    }
