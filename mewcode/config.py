from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .validator import (
    DEFAULT_CONTEXT_WINDOW,
    ConfigError,
    lookup_model_context_window,
    validate_config_structure,
)

_ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compat": "OPENAI_API_KEY",
}

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class ProviderConfig:
    name: str
    protocol: str
    base_url: str
    model: str
    # repr=False：traceback / 日志里不得出现明文 key。
    api_key: str = field(default="", repr=False)
    thinking: bool = False
    # 0 表示"未设置" — get_context_window() 通过四层 fallback 解析真实窗口大小。
    # 正数表示配置文件里显式指定的覆盖值。
    context_window: int = 0
    max_output_tokens: int = 0
    # 运行时 cache，存放从 provider 的 /v1/models 端点自动拉取的 context window
    # （get_context_window 的第 2 层）。通过 set_fetched_context_window() 写入一次；
    # 0 表示"尚未拉取"。不会持久化。
    _fetched_context_window: int = field(default=0, repr=False)

    def resolve_api_key(self) -> str:
        if self.api_key:
            resolved = resolve_env_vars(self.api_key)
            # 未命中的 ${VAR} 占位符会原样保留——视为未配置，回退到
            # 协议对应的环境变量，避免把字面量占位符发给 provider。
            if resolved and not _ENV_VAR_RE.search(resolved):
                return resolved
        env_var = _ENV_KEY_MAP.get(self.protocol, "")
        return os.environ.get(env_var, "")

    def set_fetched_context_window(self, window: int) -> None:
        """记录从 provider 自动拉取到的 context window（第 2 层）。

        非正数会被忽略，这样一次失败的拉取就不会污染 cache。在解析
        context window 时，每个 provider 只会调用一次。
        """
        if window > 0:
            self._fetched_context_window = window

    def get_context_window(self) -> int:
        """通过四层 fallback 解析模型的 context window，按优先级从高到低：

          1. 配置文件提供的 context_window（> 0）——显式覆盖，永远优先。
          2. 从 provider 的 /v1/models 端点自动拉取并通过 set_fetched_context_window
             缓存的值（只有 anthropic 协议的 provider 才会设置它；拉取失败或缺失时
             保持为 0 并跳过）。
          3. 内置的「模型名 -> window」映射表（按子串匹配）。
          4. 保守的默认值（claude -> 200000，其他 -> 128000）。
        """
        if self.context_window > 0:
            return self.context_window
        if self._fetched_context_window > 0:
            return self._fetched_context_window
        window = lookup_model_context_window(self.model)
        if window > 0:
            return window
        if "claude" in self.model.lower():
            return DEFAULT_CONTEXT_WINDOW
        return 128_000

    def get_max_output_tokens(self) -> int:
        if self.max_output_tokens > 0:
            return self.max_output_tokens
        if self.thinking:
            return 64000
        return 8192


def resolve_env_vars(value: str) -> str:
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def find_env_placeholders(value: str) -> set[str]:
    """取出 ``${VAR}`` 里引用的变量名。

    容器透传白名单用它：MCP 配置里写了 ``${GITHUB_TOKEN}``，容器里就得有
    这个名字——但值只经容器环境变量注入，绝不落进配置文件（配置文件在
    容器里是可读的，密钥不能在里面）。
    """
    return set(_ENV_VAR_RE.findall(value))


# MCP stdio 子进程按白名单继承主机环境变量：Windows 上缺 SystemRoot /
# COMSPEC / TEMP 会导致 npx 等 stdio server 起不来；白名单之外（如
# *_API_KEY）不泄漏给子进程。
# PYTHONPATH 属于"怎么跑 Python"而不是密钥：沙箱容器里 mewcode 源码就挂在
# PYTHONPATH 上，子进程丢掉它就会 ModuleNotFoundError 直接退出（真机踩到）。
_CHILD_ENV_ALLOWLIST = (
    "PATH",
    "PYTHONPATH",
    "SystemRoot",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "LANG",
    "LC_ALL",
)


def build_child_env(declared_env: dict[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _CHILD_ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value:
            env[key] = value
    for key, value in (declared_env or {}).items():
        env[key] = resolve_env_vars(value)
    return env


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"
    #: 一句话说明这个内部系统是干什么的——无人值守的 agent 看不到人，
    #: 只能靠这句话判断该不该用它的工具（M2 W3 注入服务提示词）。
    description: str = ""


    @property
    def is_stdio(self) -> bool:
        return self.transport == "stdio"


@dataclass
class WorktreeConfig:
    symlink_directories: list[str] = field(default_factory=lambda: ["node_modules", ".venv", "vendor"])
    stale_cleanup_interval: int = 3600
    stale_cutoff_hours: int = 24


@dataclass
class NotifyConfig:
    type: str = "none"
    webhook_url: str = ""
    timeout_seconds: int = 10


@dataclass
class VCSConfig:
    provider: str = "github"
    # repr=False：traceback / 日志里不得出现明文 token（同 api_key 的处理）。
    token: str = field(default="", repr=False)
    api_base: str = "https://api.github.com"
    remote: str = "origin"
    base_branch: str = "master"
    #: CI 轮询间隔与总超时
    ci_poll_interval_seconds: int = 20
    ci_timeout_seconds: int = 1800
    #: 推上去多久还没有任何 CI 检查就认为该仓库没配 CI（不阻塞、但记录在案）
    ci_none_grace_seconds: int = 120


@dataclass
class RepoConfig:
    name: str
    path: str
    url: str = ""
    base_branch: str = ""
    #: 验证命令（在 worktree 内执行）；空 = 跳过测试证据环节
    test_command: str = ""
    test_timeout_seconds: int = 300
    #: 集成测试命令（M2 W4）：仓库含 compose 文件时，服务层先起依赖环境，
    #: 再在沙箱容器内执行这条命令；空 = 不做集成验证
    integration_test_command: str = ""
    integration_timeout_seconds: int = 600


@dataclass
class SandboxConfig:
    """Docker 沙箱执行器配置（M2 W1）。"""

    enabled: bool = True
    #: 容器运行时（docker / podman 均可，命令结构一致）
    runtime: str = "docker"
    base_image: str = "python:3.12-slim"
    image_prefix: str = "mewcode-sandbox"
    workdir: str = "/workspace"
    #: 非 root 运行；空 = 自动（POSIX 用宿主 uid:gid，其他平台用 1000:1000）
    user: str = ""
    #: 网络策略：bridge（能出网，agent 需要访问 LLM API）| none（完全隔离）
    network: str = "bridge"
    cpus: float = 2.0
    memory: str = "4g"
    pids_limit: int = 512
    tmpfs_size: str = "512m"
    #: 沙箱不包含任何密钥：LLM key 只在容器环境变量里，运行时注入
    env_passthrough: list[str] = field(
        default_factory=lambda: ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"]
    )
    #: 保留容器用于排障（默认 --rm）
    keep_containers: bool = False


@dataclass
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 8321
    concurrency: int = 3
    job_timeout_seconds: int = 1800
    drain_timeout_seconds: int = 60
    # repr=False：webhook token 与 API key 同级对待，不进日志。
    webhook_token: str = field(default="", repr=False)
    dedup_window_seconds: int = 1800
    data_dir: str = ".mewcode/service"
    #: 告警 label 中承载仓库名的键（Alertmanager adapter 使用）
    repo_label: str = "repository"
    #: 注入提示词的规范类 skill：None = 未配置（用 sop.DEFAULT_SKILLS）；
    #: [] = 明确不注入任何 skill（验收标准里"关闭规范"的写法）；非空 = 指定清单
    skills: list[str] | None = None
    #: 单 job token 预算，0 = 不限制（M1 默认；M3 的成本熔断复用此字段）
    token_budget: int = 0
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    vcs: VCSConfig = field(default_factory=VCSConfig)
    repos: dict[str, RepoConfig] = field(default_factory=dict)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    #: 内部工具链（M2 W3）：只读 MCP server。直跑时在服务进程内连接；沙箱
    #: 模式下随最小配置写进容器、在容器内连接（见 service/sandbox.py）。
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)


@dataclass
class AppConfig:
    providers: list[ProviderConfig]
    permission_mode: str = "default"
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    raw_hooks: list[dict] = field(default_factory=list)
    enable_fork: bool = False
    enable_verification_agent: bool = False
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    teammate_mode: str = ""
    enable_coordinator_mode: bool = False
    service: ServiceConfig = field(default_factory=ServiceConfig)


def _load_single_file(path: Path) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse config {path}: {e}") from e

    validated = validate_config_structure(raw)

    providers = [
        ProviderConfig(
            name=p["name"],
            protocol=p["protocol"],
            base_url=p["base_url"],
            model=p["model"],
            api_key=p["api_key"],
            thinking=p["thinking"],
            context_window=p["context_window"],
            max_output_tokens=p["max_output_tokens"],
        )
        for p in validated["providers"]
    ]

    mcp_servers = [
        MCPServerConfig(
            name=s["name"],
            command=s["command"],
            args=s["args"],
            url=s["url"],
            headers=s["headers"],
            env=s["env"],
            transport=s["transport"],
            description=s.get("description", ""),
        )
        for s in validated["mcp_servers"]
    ]

    wt = validated["worktree"]
    worktree_cfg = WorktreeConfig(
        symlink_directories=wt["symlink_directories"],
        stale_cleanup_interval=wt["stale_cleanup_interval"],
        stale_cutoff_hours=wt["stale_cutoff_hours"],
    )

    svc = validated["service"]
    sbx = svc["sandbox"]
    service_cfg = ServiceConfig(
        host=svc["host"],
        port=svc["port"],
        concurrency=svc["concurrency"],
        job_timeout_seconds=svc["job_timeout_seconds"],
        drain_timeout_seconds=svc["drain_timeout_seconds"],
        webhook_token=svc["webhook_token"],
        dedup_window_seconds=svc["dedup_window_seconds"],
        data_dir=svc["data_dir"],
        repo_label=svc["repo_label"],
        # None 与 [] 语义不同（未配置 vs 明确关闭），不能合并成同一个值
        skills=None if svc["skills"] is None else list(svc["skills"]),
        token_budget=svc["token_budget"],
        notify=NotifyConfig(
            type=svc["notify"]["type"],
            webhook_url=svc["notify"]["webhook_url"],
            timeout_seconds=svc["notify"]["timeout_seconds"],
        ),
        vcs=VCSConfig(
            provider=svc["vcs"]["provider"],
            token=svc["vcs"]["token"],
            api_base=svc["vcs"]["api_base"],
            remote=svc["vcs"]["remote"],
            base_branch=svc["vcs"]["base_branch"],
            ci_poll_interval_seconds=svc["vcs"]["ci_poll_interval_seconds"],
            ci_timeout_seconds=svc["vcs"]["ci_timeout_seconds"],
            ci_none_grace_seconds=svc["vcs"]["ci_none_grace_seconds"],
        ),
        sandbox=SandboxConfig(
            enabled=sbx["enabled"],
            runtime=sbx["runtime"],
            base_image=sbx["base_image"],
            image_prefix=sbx["image_prefix"],
            workdir=sbx["workdir"],
            user=sbx["user"],
            network=sbx["network"],
            cpus=sbx["cpus"],
            memory=sbx["memory"],
            pids_limit=sbx["pids_limit"],
            tmpfs_size=sbx["tmpfs_size"],
            env_passthrough=list(sbx["env_passthrough"]),
            keep_containers=sbx["keep_containers"],
        ),
        repos={
            name: RepoConfig(
                name=entry["name"],
                path=entry["path"],
                url=entry["url"],
                base_branch=entry["base_branch"],
                test_command=entry["test_command"],
                test_timeout_seconds=entry["test_timeout_seconds"],
                integration_test_command=entry["integration_test_command"],
                integration_timeout_seconds=entry["integration_timeout_seconds"],
            )
            for name, entry in svc["repos"].items()
        },
        mcp_servers=[
            MCPServerConfig(
                name=s["name"],
                command=s["command"],
                args=s["args"],
                url=s["url"],
                headers=s["headers"],
                env=s["env"],
                transport=s["transport"],
                description=s.get("description", ""),
            )
            for s in svc["mcp_servers"]
        ],
    )

    return AppConfig(
        providers=providers,
        permission_mode=validated["permission_mode"],
        mcp_servers=mcp_servers,
        raw_hooks=validated["hooks"],
        enable_fork=validated["enable_fork"],
        enable_verification_agent=validated["enable_verification_agent"],
        worktree=worktree_cfg,
        teammate_mode=validated["teammate_mode"],
        enable_coordinator_mode=validated["enable_coordinator_mode"],
        service=service_cfg,
    )


def _merge_config(base: AppConfig, override: AppConfig) -> AppConfig:
    if override.providers:
        base.providers = override.providers
    if override.permission_mode != "default":
        base.permission_mode = override.permission_mode

    if override.mcp_servers:
        by_name = {s.name: i for i, s in enumerate(base.mcp_servers)}
        for s in override.mcp_servers:
            if s.name in by_name:
                base.mcp_servers[by_name[s.name]] = s
            else:
                base.mcp_servers.append(s)
                by_name[s.name] = len(base.mcp_servers) - 1

    base.raw_hooks.extend(override.raw_hooks)
    if override.enable_fork:
        base.enable_fork = True
    if override.enable_verification_agent:
        base.enable_verification_agent = True
    if override.teammate_mode:
        base.teammate_mode = override.teammate_mode
    if override.enable_coordinator_mode:
        base.enable_coordinator_mode = True
    # 与 permission_mode 同样的"非默认才覆盖"语义：解析器无法区分
    # "配置里没写 service 段" 与 "写了但都是默认值"，一律视为未覆盖。
    if override.service != ServiceConfig():
        base.service = override.service
    return base


def load_config(path: Path | None = None) -> AppConfig:
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return _load_single_file(path)

    cwd = Path.cwd()
    home = Path.home()
    candidates = [
        home / ".mewcode" / "config.yaml",
        cwd / ".mewcode" / "config.yaml",
        cwd / ".mewcode" / "config.local.yaml",
    ]

    merged: AppConfig | None = None
    for p in candidates:
        if not p.exists():
            continue
        layer = _load_single_file(p)
        if merged is None:
            merged = layer
        else:
            merged = _merge_config(merged, layer)

    if merged is None:
        raise ConfigError(
            "No config file found. Expected .mewcode/config.yaml "
            "in project or ~/.mewcode/config.yaml"
        )
    return merged
