"""Trigger adapters：把各触发源的原始 payload 归一化为内部 Job 草案。

每个 adapter 是纯函数式转换（无 IO），因此可以单测到每个字段；
真正的落库/去重/入队由 :class:`mewcode.service.runtime.ServiceRuntime` 负责。
"""

from .base import JobDraft, TriggerAdapter

__all__ = ["JobDraft", "TriggerAdapter"]
