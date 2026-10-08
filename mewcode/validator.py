"""MewCode 的配置校验逻辑。"""

from __future__ import annotations

VALID_PROTOCOLS = {"anthropic", "openai", "openai-compat"}

VALID_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "custom",
    "dontAsk",
}

VALID_TEAMMATE_MODES = {"", "in-process"}

DEFAULT_CONTEXT_WINDOW = 200_000

# 内置的"模型名子串 -> context window（最大输入 token 数）"映射表，
# 是 context window 回退链的第 3 层（见 ProviderConfig.get_context_window）。
# 按从最具体到最通用排序，第一个子串命中即生效。值仅为合理起始点，
# 模型更新/重命名后可能过时。如果值不准确，在配置中设置 context_window 覆盖（最高优先级）。
MODEL_CONTEXT_WINDOWS: list[tuple[str, int]] = [
    ("1m", 1_000_000),       # 也覆盖 "-1m" 后缀（如 claude-...-1m）
    ("gpt-4.1", 1_000_000),  # GPT-4.1 系列的 window 为 1M
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o1", 200_000),         # OpenAI 推理模型 o1 / o3 / o4
    ("o3", 200_000),
    ("o4", 200_000),
    ("gpt-3.5", 16_385),
    ("claude", 200_000),
]


def lookup_model_context_window(model: str) -> int:
    """通过子串匹配（第 3 层），返回内置映射表中该模型对应的
    context window；没有匹配则返回 0。"""
    m = model.lower()
    for substr, window in MODEL_CONTEXT_WINDOWS:
        if substr in m:
            return window
    return 0


class ConfigError(Exception):
    pass


def validate_providers(raw_providers: list) -> list[dict]:
    """校验 providers 列表，返回清洗后的 provider 字典列表。"""
    if not isinstance(raw_providers, list) or len(raw_providers) == 0:
        raise ConfigError("At least one provider must be configured")

    providers: list[dict] = []
    for i, entry in enumerate(raw_providers):
        if not isinstance(entry, dict):
            raise ConfigError(f"Provider #{i + 1}: must be a mapping")

        missing = [f for f in ("name", "protocol", "base_url", "model") if f not in entry]
        if missing:
            raise ConfigError(f"Provider #{i + 1}: missing fields: {', '.join(missing)}")

        protocol = entry["protocol"]
        if protocol not in VALID_PROTOCOLS:
            raise ConfigError(
                f"Provider #{i + 1}: invalid protocol '{protocol}', "
                f"must be one of: {', '.join(sorted(VALID_PROTOCOLS))}"
            )

        # 默认为 0（"未设置"）而非硬编码的 window 值：0 会让
        # ProviderConfig.get_context_window() 走四层回退链解析
        #（自动拉取 / 映射表 / 默认值）。配置中显式指定的值仍须为正整数，
        # 且作为最高优先级覆盖。
        context_window = entry.get("context_window", 0)
        if not isinstance(context_window, int) or isinstance(context_window, bool) or context_window < 0:
            raise ConfigError(
                f"Provider #{i + 1}: context_window must be a positive integer"
            )

        thinking = entry.get("thinking", False)
        if not isinstance(thinking, bool):
            raise ConfigError(f"Provider #{i + 1}: thinking must be a boolean")

        max_output_tokens = entry.get("max_output_tokens", 0)
        if not isinstance(max_output_tokens, int) or max_output_tokens < 0:
            raise ConfigError(
                f"Provider #{i + 1}: max_output_tokens must be a non-negative integer"
            )

        providers.append(
            {
                "name": entry["name"],
                "protocol": protocol,
                "base_url": entry["base_url"],
                "model": entry["model"],
                "api_key": entry.get("api_key", ""),
                "thinking": thinking,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
            }
        )

    return providers


def validate_permission_mode(mode: str) -> str:
    """校验 permission_mode 取值。"""
    if mode not in VALID_PERMISSION_MODES:
        raise ConfigError(
            f"Invalid permission_mode '{mode}', "
            f"must be one of: {', '.join(sorted(VALID_PERMISSION_MODES))}"
        )
    return mode


VALID_MCP_TRANSPORTS = ("stdio", "http")


def validate_mcp_servers(raw_mcp: list | None) -> list[dict]:
    """校验 mcp_servers 配置段，返回清洗后的 server 配置字典列表。"""
    if raw_mcp is None:
        return []

    if not isinstance(raw_mcp, list):
        raise ConfigError("'mcp_servers' must be a list of server configs")

    servers: list[dict] = []
    for i, entry in enumerate(raw_mcp):
        if not isinstance(entry, dict):
            raise ConfigError(f"MCP server #{i + 1}: must be a mapping")
        name = entry.get("name")
        if not name:
            raise ConfigError(f"MCP server #{i + 1}: missing 'name'")
        has_command = "command" in entry
        has_url = "url" in entry
        if has_command and has_url:
            raise ConfigError(
                f"MCP server '{name}': cannot have both 'command' and 'url'"
            )
        if not has_command and not has_url:
            raise ConfigError(
                f"MCP server '{name}': must have either 'command' or 'url'"
            )
        transport = entry.get("transport")
        if transport is None:
            # 未显式声明时按连接字段推断，保持旧配置的语义。
            transport = "stdio" if has_command else "http"
        elif transport not in VALID_MCP_TRANSPORTS:
            raise ConfigError(
                f"MCP server '{name}': invalid transport '{transport}' "
                f"(expected one of {', '.join(VALID_MCP_TRANSPORTS)})"
            )
        if transport == "http" and not has_url:
            raise ConfigError(
                f"MCP server '{name}': transport 'http' requires 'url'"
            )
        if transport == "stdio" and not has_command:
            raise ConfigError(
                f"MCP server '{name}': transport 'stdio' requires 'command'"
            )
        servers.append(
            {
                "name": name,
                "command": entry.get("command"),
                "args": entry.get("args", []),
                "url": entry.get("url"),
                "headers": entry.get("headers", {}),
                "env": entry.get("env", {}),
                "transport": transport,
                "description": _optional_str(
                    entry.get("description", ""), f"MCP server '{name}'.description"
                ),
            }
        )

    return servers


def validate_hooks(raw_hooks: list | None) -> list:
    """校验 hooks 配置段。"""
    if raw_hooks is None:
        return []
    if not isinstance(raw_hooks, list):
        raise ConfigError("'hooks' must be a list of hook definitions")
    return raw_hooks


def validate_bool_field(value: object, field_name: str) -> bool:
    """校验一个布尔类型的配置字段。"""
    if not isinstance(value, bool):
        raise ConfigError(f"'{field_name}' must be a boolean")
    return value


def validate_worktree(raw_wt: dict | None) -> dict:
    """校验 worktree 配置段，返回清洗后的配置字典。"""
    defaults = {
        "symlink_directories": ["node_modules", ".venv", "vendor"],
        "stale_cleanup_interval": 3600,
        "stale_cutoff_hours": 24,
    }

    if raw_wt is None:
        return defaults

    if not isinstance(raw_wt, dict):
        raise ConfigError("'worktree' must be a mapping")

    sym = raw_wt.get("symlink_directories", defaults["symlink_directories"])
    if not isinstance(sym, list) or not all(isinstance(s, str) for s in sym):
        raise ConfigError("'worktree.symlink_directories' must be a list of strings")

    interval = raw_wt.get("stale_cleanup_interval", defaults["stale_cleanup_interval"])
    if not isinstance(interval, int) or interval <= 0:
        raise ConfigError("'worktree.stale_cleanup_interval' must be a positive integer")

    cutoff = raw_wt.get("stale_cutoff_hours", defaults["stale_cutoff_hours"])
    if not isinstance(cutoff, int) or cutoff <= 0:
        raise ConfigError("'worktree.stale_cutoff_hours' must be a positive integer")

    return {
        "symlink_directories": sym,
        "stale_cleanup_interval": interval,
        "stale_cutoff_hours": cutoff,
    }


def validate_teammate_mode(mode: object) -> str:
    """校验 teammate_mode 取值。"""
    if not isinstance(mode, str) or mode not in VALID_TEAMMATE_MODES:
        raise ConfigError(
            f"Invalid teammate_mode '{mode}', "
            f"must be one of: {', '.join(repr(m) for m in sorted(VALID_TEAMMATE_MODES))}"
        )
    return mode


VALID_NOTIFY_TYPES = ("none", "slack", "dingtalk", "wecom")
VALID_SANDBOX_NETWORKS = ("bridge", "none")
VALID_VCS_PROVIDERS = ("github", "none")


def _positive_int(value: object, field_name: str, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"'{field_name}' must be an integer >= {minimum}")
    return value


def _optional_str(value: object, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"'{field_name}' must be a string")
    return value


def _validate_sandbox(raw: dict | None) -> dict:
    """校验 ``service.sandbox`` 段（M2 W1 Docker 沙箱执行器）。"""
    defaults: dict = {
        "enabled": True,
        "allow_host_fallback": False,
        "runtime": "docker",
        "base_image": "python:3.12-slim",
        "image_prefix": "mewcode-sandbox",
        "workdir": "/workspace",
        "user": "",
        "network": "bridge",
        "cpus": 2.0,
        "memory": "4g",
        "pids_limit": 512,
        "tmpfs_size": "512m",
        "env_passthrough": ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"],
        "keep_containers": False,
    }
    if raw is None:
        return defaults
    if not isinstance(raw, dict):
        raise ConfigError("'service.sandbox' must be a mapping")
    merged = {**defaults, **raw}

    merged["enabled"] = validate_bool_field(merged["enabled"], "service.sandbox.enabled")
    merged["allow_host_fallback"] = validate_bool_field(
        merged["allow_host_fallback"], "service.sandbox.allow_host_fallback"
    )
    merged["keep_containers"] = validate_bool_field(
        merged["keep_containers"], "service.sandbox.keep_containers"
    )
    for key in ("runtime", "base_image", "image_prefix", "workdir", "user", "memory", "tmpfs_size"):
        merged[key] = _optional_str(merged[key], f"service.sandbox.{key}")
    for key in ("runtime", "base_image", "image_prefix", "workdir", "memory", "tmpfs_size"):
        if not merged[key]:
            raise ConfigError(f"'service.sandbox.{key}' must not be empty")
    if merged["network"] not in VALID_SANDBOX_NETWORKS:
        raise ConfigError(
            f"'service.sandbox.network' must be one of: {', '.join(VALID_SANDBOX_NETWORKS)}"
        )
    cpus = merged["cpus"]
    if not isinstance(cpus, (int, float)) or isinstance(cpus, bool) or cpus <= 0:
        raise ConfigError("'service.sandbox.cpus' must be a positive number")
    merged["cpus"] = float(cpus)
    merged["pids_limit"] = _positive_int(merged["pids_limit"], "service.sandbox.pids_limit")
    env = merged["env_passthrough"]
    if not isinstance(env, list) or not all(isinstance(x, str) for x in env):
        raise ConfigError("'service.sandbox.env_passthrough' must be a list of strings")
    return merged


def validate_service(raw_service: dict | None) -> dict:
    """校验 ``service:`` 配置段（M1 无头服务），返回清洗后的字典。

    ``service:`` 缺省时为 None 等价于服务未配置——``mewcode serve`` 会以
    默认值启动，但需要 repos 路由表的执行链会在运行到具体 job 时拒绝。
    """
    defaults: dict = {
        "host": "127.0.0.1",
        "port": 8321,
        "concurrency": 3,
        "job_timeout_seconds": 1800,
        "drain_timeout_seconds": 60,
        "webhook_token": "",
        "dedup_window_seconds": 1800,
        "data_dir": ".mewcode/service",
        "repo_label": "repository",
        # None = 未配置（执行链用内置默认 skill 包）；[] = 明确要求不注入任何 skill。
        # 两者必须可区分：验收标准里的"关闭规范"就是写 skills: []
        "skills": None,
        "token_budget": 0,
        "mcp_servers": [],
        "notify": {"type": "none", "webhook_url": "", "timeout_seconds": 10},
        # 沙箱默认值同样走校验器：两条返回路径（有/无 service 段）必须给出同一份默认值
        "sandbox": _validate_sandbox(None),
        "vcs": {
            "provider": "github",
            "token": "",
            "api_base": "https://api.github.com",
            "remote": "origin",
            "base_branch": "master",
            "ci_poll_interval_seconds": 20,
            "ci_timeout_seconds": 1800,
            "ci_none_grace_seconds": 120,
        },
        "repos": {},
    }
    if raw_service is None:
        return defaults
    if not isinstance(raw_service, dict):
        raise ConfigError("'service' must be a mapping")

    merged = {
        **defaults,
        **{
            k: v
            for k, v in raw_service.items()
            if k not in ("notify", "vcs", "repos", "sandbox", "mcp_servers")
        },
    }

    host = _optional_str(merged["host"], "service.host")
    port = _positive_int(merged["port"], "service.port")
    if port > 65535:
        raise ConfigError("'service.port' must be <= 65535")
    concurrency = _positive_int(merged["concurrency"], "service.concurrency")
    job_timeout = _positive_int(merged["job_timeout_seconds"], "service.job_timeout_seconds")
    drain_timeout = _positive_int(merged["drain_timeout_seconds"], "service.drain_timeout_seconds")
    dedup_window = _positive_int(merged["dedup_window_seconds"], "service.dedup_window_seconds")
    # 0 = 不设预算（M1 默认）；正数 = 单 job token 上限
    token_budget = _positive_int(merged["token_budget"], "service.token_budget", minimum=0)

    skills = merged["skills"]
    if skills is not None and (
        not isinstance(skills, list) or not all(isinstance(x, str) for x in skills)
    ):
        raise ConfigError("'service.skills' must be a list of skill names")

    # notify 段
    raw_notify = raw_service.get("notify") or {}
    if not isinstance(raw_notify, dict):
        raise ConfigError("'service.notify' must be a mapping")
    notify = {**defaults["notify"], **raw_notify}
    if notify["type"] not in VALID_NOTIFY_TYPES:
        raise ConfigError(
            f"'service.notify.type' must be one of: {', '.join(VALID_NOTIFY_TYPES)}"
        )
    notify["webhook_url"] = _optional_str(notify["webhook_url"], "service.notify.webhook_url")
    notify["timeout_seconds"] = _positive_int(notify["timeout_seconds"], "service.notify.timeout_seconds")

    # vcs 段
    raw_vcs = raw_service.get("vcs") or {}
    if not isinstance(raw_vcs, dict):
        raise ConfigError("'service.vcs' must be a mapping")
    vcs = {**defaults["vcs"], **raw_vcs}
    if vcs["provider"] not in VALID_VCS_PROVIDERS:
        raise ConfigError(
            f"'service.vcs.provider' must be one of: {', '.join(VALID_VCS_PROVIDERS)}"
        )
    for key in ("token", "api_base", "remote", "base_branch"):
        vcs[key] = _optional_str(vcs[key], f"service.vcs.{key}")
    for key in ("ci_poll_interval_seconds", "ci_timeout_seconds", "ci_none_grace_seconds"):
        vcs[key] = _positive_int(vcs[key], f"service.vcs.{key}")

    # repos 路由表：告警 label 'repository' -> 本地 checkout 与远端信息
    raw_repos = raw_service.get("repos") or {}
    if not isinstance(raw_repos, dict):
        raise ConfigError("'service.repos' must be a mapping of name -> repo config")
    repos: dict = {}
    for name, entry in raw_repos.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"'service.repos.{name}' must be a mapping")
        if "path" not in entry:
            raise ConfigError(f"'service.repos.{name}' must define 'path'")
        repos[name] = {
            "name": name,
            "path": _optional_str(entry["path"], f"service.repos.{name}.path"),
            "url": _optional_str(entry.get("url"), f"service.repos.{name}.url"),
            "base_branch": _optional_str(entry.get("base_branch"), f"service.repos.{name}.base_branch"),
            "test_command": _optional_str(entry.get("test_command"), f"service.repos.{name}.test_command"),
            "test_timeout_seconds": _positive_int(
                entry.get("test_timeout_seconds", 300), f"service.repos.{name}.test_timeout_seconds"
            ),
            "integration_test_command": _optional_str(
                entry.get("integration_test_command"),
                f"service.repos.{name}.integration_test_command",
            ),
            "integration_timeout_seconds": _positive_int(
                entry.get("integration_timeout_seconds", 600),
                f"service.repos.{name}.integration_timeout_seconds",
            ),
        }

    return {
        "host": host,
        "port": port,
        "concurrency": concurrency,
        "job_timeout_seconds": job_timeout,
        "drain_timeout_seconds": drain_timeout,
        "webhook_token": _optional_str(merged["webhook_token"], "service.webhook_token"),
        "dedup_window_seconds": dedup_window,
        "data_dir": _optional_str(merged["data_dir"], "service.data_dir"),
        "repo_label": _optional_str(merged["repo_label"], "service.repo_label") or "repository",
        "skills": skills,
        "token_budget": token_budget,
        "mcp_servers": validate_mcp_servers(raw_service.get("mcp_servers")),
        "notify": notify,
        "vcs": vcs,
        "repos": repos,
        "sandbox": _validate_sandbox(raw_service.get("sandbox")),
    }


def validate_config_structure(raw: object) -> dict:
    """校验的主入口。校验解析后的原始配置，返回清洗后的字典。

    返回的字典包含以下键：
        providers、permission_mode、mcp_servers、hooks、
        enable_fork、enable_verification_agent、worktree、
        teammate_mode、enable_coordinator_mode、service
    """
    if not isinstance(raw, dict) or "providers" not in raw:
        raise ConfigError("Config must contain a 'providers' list")

    return {
        "providers": validate_providers(raw["providers"]),
        "permission_mode": validate_permission_mode(raw.get("permission_mode", "default")),
        "mcp_servers": validate_mcp_servers(raw.get("mcp_servers")),
        "hooks": validate_hooks(raw.get("hooks")),
        "enable_fork": validate_bool_field(raw.get("enable_fork", False), "enable_fork"),
        "enable_verification_agent": validate_bool_field(
            raw.get("enable_verification_agent", False), "enable_verification_agent"
        ),
        "worktree": validate_worktree(raw.get("worktree")),
        "teammate_mode": validate_teammate_mode(raw.get("teammate_mode", "")),
        "enable_coordinator_mode": validate_bool_field(
            raw.get("enable_coordinator_mode", False), "enable_coordinator_mode"
        ),
        "service": validate_service(raw.get("service")),
    }
