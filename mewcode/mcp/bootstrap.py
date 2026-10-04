"""无头/服务场景的 MCP 接线入口。

TUI 的接线在 ``app.py``（交互式，工具延迟加载、连接失败只提示不阻塞）；
这里服务于**无人值守**场景（``mewcode -p``、服务执行链、沙箱容器内），
两条语义上的差异是刻意的：

1. **工具直接可见**（``defer=False``）：无人值守没有"模型记得先去 ToolSearch"
   的余地——工具要么出现在它眼前的工具表里，要么就等于不存在。
2. **只挂只读工具**（``read_only_only=True``）：agent 拿不到写内部系统的能力，
   即使某个 server 提供了。服务模式里"能查到"和"能改到"的差别是安全边界。

失败语义：任何 server 连不上/超时都只记错误、不抛异常——内部工具是增强项，
不能因为它挂了就把整个修复作业带下去。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from mewcode.config import MCPServerConfig
from mewcode.mcp.manager import MCPManager
from mewcode.tools import ToolRegistry

log = logging.getLogger(__name__)

#: 单个 server 的连接上限（秒）：一个卡死的内部服务不能把作业挂在启动阶段
MCP_CONNECT_TIMEOUT = 30.0


@dataclass
class MCPBootstrapResult:
    manager: MCPManager | None = None
    server_names: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.tool_names)

    def summary(self) -> str:
        """一行审计摘要（写进 job 事件，PR body 的原料之一）。"""
        detail = f"servers={','.join(self.server_names) or 'none'} tools={len(self.tool_names)}"
        if self.tool_names:
            detail += " (" + ", ".join(sorted(self.tool_names)[:12]) + ")"
        if self.errors:
            detail += f" errors={len(self.errors)}: " + "; ".join(self.errors[:3])
        return detail


async def register_mcp_tools(
    registry: ToolRegistry,
    configs: list[MCPServerConfig] | None,
    *,
    defer: bool = False,
    read_only_only: bool = True,
    connect_timeout: float | None = MCP_CONNECT_TIMEOUT,
) -> MCPBootstrapResult:
    """连接配置的 MCP server 并注册其（只读）工具。永不向调用方抛异常。"""
    result = MCPBootstrapResult(server_names=[c.name for c in (configs or [])])
    if not configs:
        return result

    manager = MCPManager()
    result.manager = manager
    before = {t.name for t in registry.list_tools()}
    try:
        manager.load_configs(configs)
        result.errors = await manager.register_all_tools(
            registry,
            connect_timeout=connect_timeout,
            defer=defer,
            read_only_only=read_only_only,
        )
    except Exception as e:  # 连接层之外的意外（导入/配置层面的问题）
        log.warning("MCP bootstrap failed: %s", e)
        result.errors.append(f"MCP bootstrap failed: {type(e).__name__}: {e}")
    result.tool_names = sorted(t.name for t in registry.list_tools() if t.name not in before)
    return result


async def close_mcp(result: MCPBootstrapResult | None) -> None:
    """显式收尾 stdio 子进程（仓库已知坑：不能依赖进程退出兜底）。"""
    if result is None or result.manager is None:
        return
    try:
        await result.manager.shutdown()
    except Exception as e:  # 收尾失败只记日志：作业结果不受影响
        log.warning("MCP shutdown failed: %s", e)
