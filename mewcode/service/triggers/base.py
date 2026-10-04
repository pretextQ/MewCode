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
    #: 归一化过程中的非致命问题（如 severity 取值未知）
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    """一次解析的完整结果。

    批量告警里部分条目不可路由是常态（缺 label、非 firing、仓库不在路由表），
    这些必须**显式**出现在 ``skipped`` 里而不是静默丢弃——"告警为什么没修"
    必须能从 HTTP 响应与审计里回答。
    """

    drafts: list[JobDraft] = field(default_factory=list)
    #: 未被受理的条目及原因（人类可读）
    skipped: list[str] = field(default_factory=list)
    #: 跟 drafts 一起流转的非致命提醒
    warnings: list[str] = field(default_factory=list)


class TriggerAdapter(Protocol):
    """把某触发源的原始 payload 转成 JobDraft 列表。

    Alertmanager 一次 POST 可携带多条 alerts，因此是列表；实现必须是
    纯转换：不访问网络、不落库、不做去重（去重在 runtime 统一做）。
    """

    def parse(self, payload: dict[str, Any]) -> ParseResult: ...


class TriggerError(Exception):
    """payload 无法解析为任何可用 job 时抛出（调用方回 4xx，不产生空 job）。"""
