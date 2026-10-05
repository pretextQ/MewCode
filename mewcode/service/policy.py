"""仓库级策略（M3 W2）：<repo>/.mewcode/policy.yaml。

优先级：仓库级 policy.yaml > 服务配置 > 默认。四个生效点分别在各自的生命周期
阶段读取本模块：

- 触发路由（``runtime.intake``）：``triggers.severities`` 白名单，不匹配的告警
  在门口拒收（HTTP 响应的 ``rejected`` 里给原因），不建 job、不烧 token；
- 目标分支（``execution.ExecutionChain``）：``target_branch`` 同时决定 worktree
  基线与 PR base（经 ExecutionContext 传给 publisher）；
- token 预算（``execution.HeadlessAgentRunner``）：``token_budget`` 覆盖
  ``service.token_budget``；
- 通知渠道（``notify.RepoPolicyNotifier``）：``notify`` 段覆盖服务级通知配置。

文件按 job 现读现解析（十几行的 YAML，开销与一次 agent 运行相比可忽略）——
改文件即生效、服务无需重启，也没有缓存失效问题。

解析失败（YAML 语法 / 未知键 / 字段类型错）抛 :class:`PolicyError`，调用方
显式处理：intake 拒收并在响应里给原因、执行链 escalate——宁可不修也不带病
运行（与 config 校验同一哲学：写错的策略必须立刻可见，而不是静默回退）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from mewcode.config import NotifyConfig, RepoConfig
from mewcode.service.triggers.alertmanager import VALID_SEVERITIES
from mewcode.validator import VALID_NOTIFY_TYPES

POLICY_DIR = ".mewcode"
POLICY_FILENAME = "policy.yaml"

_ALLOWED_KEYS = ("triggers", "target_branch", "token_budget", "notify")
_TRIGGER_KEYS = ("severities",)
_NOTIFY_KEYS = ("type", "webhook_url", "timeout_seconds")


class PolicyError(Exception):
    """仓库策略文件无法读取或解析——调用方必须显式处理，不允许静默回退。"""


@dataclass(frozen=True)
class RepoPolicy:
    """单个仓库的生效策略；空实例 = 无任何覆盖（走服务配置与默认值）。"""

    #: 触发路由：只接这些严重级；空 = 全接
    severities: tuple[str, ...] = ()
    #: 目标分支（worktree 基线 + PR base）；空 = 未覆盖
    target_branch: str = ""
    #: 单 job token 预算；0 = 未覆盖（删除该键即回到服务配置）
    token_budget: int = 0
    #: 通知渠道覆盖；None = 未覆盖
    notify: NotifyConfig | None = None
    #: 策略文件路径——审计事件里说明覆盖来自哪里；空 = 无策略文件
    source: str = ""

    def accepts(self, severity: str) -> bool:
        """触发路由判定：未声明 severities = 全接；声明了 = 白名单。"""
        if not self.severities:
            return True
        return severity in self.severities

    @property
    def overrides_anything(self) -> bool:
        """是否声明了任一覆盖——决定要不要写 policy_applied 审计事件。"""
        return bool(
            self.severities or self.target_branch or self.token_budget > 0 or self.notify is not None
        )


class RepoPolicyLoader:
    """按仓库名加载策略。仓库不在路由表 / 没写策略文件 => 空策略（默认行为）。"""

    def __init__(self, repos: dict[str, RepoConfig]) -> None:
        self._repos = repos

    def policy_path(self, repo_name: str) -> Path | None:
        repo = self._repos.get(repo_name)
        if repo is None or not repo.path:
            return None
        return Path(repo.path) / POLICY_DIR / POLICY_FILENAME

    def load(self, repo_name: str) -> RepoPolicy:
        path = self.policy_path(repo_name)
        if path is None or not path.is_file():
            return RepoPolicy()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise PolicyError(f"failed to read {path}: {e}") from e
        except yaml.YAMLError as e:
            raise PolicyError(f"failed to parse {path}: {e}") from e
        if raw is None:  # 空文件 = 无覆盖
            return RepoPolicy()
        if not isinstance(raw, dict):
            raise PolicyError(f"{path}: policy must be a mapping")
        return _parse_policy(raw, str(path))


def _parse_policy(raw: dict[str, Any], source: str) -> RepoPolicy:
    unknown = sorted(set(raw) - set(_ALLOWED_KEYS))
    if unknown:
        raise PolicyError(
            f"{source}: unknown key(s) {', '.join(unknown)}; allowed: {', '.join(_ALLOWED_KEYS)}"
        )

    severities: tuple[str, ...] = ()
    triggers = raw.get("triggers")
    if triggers is not None:
        if not isinstance(triggers, dict):
            raise PolicyError(f"{source}: 'triggers' must be a mapping")
        unknown = sorted(set(triggers) - set(_TRIGGER_KEYS))
        if unknown:
            raise PolicyError(f"{source}: unknown 'triggers' key(s): {', '.join(unknown)}")
        severities = _parse_severities(triggers.get("severities"), source)

    target_branch = ""
    if raw.get("target_branch") is not None:
        value = raw["target_branch"]
        if not isinstance(value, str) or not value.strip():
            raise PolicyError(f"{source}: 'target_branch' must be a non-empty string")
        target_branch = value.strip()

    token_budget = 0
    if raw.get("token_budget") is not None:
        value = raw["token_budget"]
        # bool 是 int 的子类，必须先排除
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PolicyError(f"{source}: 'token_budget' must be a positive integer")
        token_budget = value

    notify = _parse_notify(raw["notify"], source) if raw.get("notify") is not None else None

    return RepoPolicy(
        severities=severities,
        target_branch=target_branch,
        token_budget=token_budget,
        notify=notify,
        source=source,
    )


def _parse_severities(value: Any, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(s, str) for s in value):
        raise PolicyError(f"{source}: 'triggers.severities' must be a non-empty list of strings")
    unknown = [s for s in value if s not in VALID_SEVERITIES]
    if unknown:
        raise PolicyError(
            f"{source}: unknown severity {', '.join(unknown)}; valid: {', '.join(VALID_SEVERITIES)}"
        )
    if len(set(value)) != len(value):
        raise PolicyError(f"{source}: 'triggers.severities' has duplicates")
    return tuple(value)


def _parse_notify(value: Any, source: str) -> NotifyConfig:
    if not isinstance(value, dict):
        raise PolicyError(f"{source}: 'notify' must be a mapping")
    unknown = sorted(set(value) - set(_NOTIFY_KEYS))
    if unknown:
        raise PolicyError(f"{source}: unknown 'notify' key(s): {', '.join(unknown)}")
    ntype = value.get("type")
    if ntype not in VALID_NOTIFY_TYPES:
        raise PolicyError(
            f"{source}: 'notify.type' must be one of: {', '.join(VALID_NOTIFY_TYPES)}"
        )
    webhook_url = value.get("webhook_url", "")
    if not isinstance(webhook_url, str):
        raise PolicyError(f"{source}: 'notify.webhook_url' must be a string")
    timeout = value.get("timeout_seconds", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise PolicyError(f"{source}: 'notify.timeout_seconds' must be a positive integer")
    return NotifyConfig(type=ntype, webhook_url=webhook_url, timeout_seconds=timeout)
