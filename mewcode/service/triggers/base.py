"""触发适配器的公共契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class JobDraft:
    """归一化后的 job 草案（尚未落库）。

    ``fingerprint`` 是幂等键：同一告警重复触发时用于合并到已有 job。
    ``repo`` 是路由表中的仓库名（见 ``service.repos``），不是 git URL。
    """

    fingerprint: str
    repo: str
    title: str = ""
    severity: str = "warning"
    payload: dict[str, Any] = field(default_factory=dict)
    #: 归一化过程中的非致命问题（如 payload 里没有可用的仓库标签）
    warnings: list[str] = field(default_factory=list)


class TriggerAdapter(Protocol):
    """把某触发源的原始 payload 转成 Job 草案列表。

    Alertmanager 一次 POST 可携带多条 alerts，因此返回列表；实现必须是
    纯转换：不访问网络、不落库、不做去重（去重在 runtime 统一做）。
    """

    def parse(self, payload: dict[str, Any]) -> list[JobDraft]: ...


class TriggerError(Exception):
    """payload 无法解析为任何可用 job 时抛出（调用方回 4xx，不产生空 job）。"""
