from __future__ import annotations

import asyncio
import logging

from mewcode.config import MCPServerConfig
from mewcode.mcp.client import MCPClient
from mewcode.mcp.tool_wrapper import MCPToolWrapper, is_read_only_tool
from mewcode.tools import ToolRegistry

logger = logging.getLogger(__name__)


class MCPManager:


    def __init__(self) -> None:
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock


    def load_configs(self, configs: list[MCPServerConfig]) -> None:
        for cfg in configs:
            self._configs[cfg.name] = cfg


    async def register_all_tools(
        self,
        registry: ToolRegistry,
        *,
        connect_timeout: float | None = None,
        defer: bool = True,
        read_only_only: bool = False,
    ) -> list[str]:
        """连接所有已配置 server 并把工具注册进 registry；返回错误列表。

        - ``connect_timeout``：单个 server 的连接上限。无人值守场景必须设——
          一个卡死的内部服务不能把整个作业挂在启动阶段。
        - ``defer``：是否走延迟加载（模型要先 ToolSearch 才看得见）。交互式
          TUI 保留延迟加载省 token；无头模式要确定性，直接可见。
        - ``read_only_only``：只注册声明了 ``readOnlyHint`` 的工具。服务模式
          默认开——agent 不可信，那就别把能写外部系统的工具递给它。
        """
        errors: list[str] = []
        for name, config in self._configs.items():
            try:
                client = MCPClient(config)
                connect = client.connect()
                if connect_timeout is not None:
                    await asyncio.wait_for(connect, timeout=connect_timeout)
                else:
                    await connect
                self._clients[name] = client

                tools = await client.list_tools()
                for tool_def in tools:
                    if read_only_only and not is_read_only_tool(tool_def):
                        logger.warning(
                            "MCP server '%s': skipping non-read-only tool '%s' "
                            "(the service only grants read-only internal tools)",
                            name,
                            tool_def.name,
                        )
                        errors.append(
                            f"MCP server '{name}': skipped non-read-only tool '{tool_def.name}'"
                        )
                        continue
                    wrapper = MCPToolWrapper(name, tool_def, client)
                    if not defer:
                        wrapper.should_defer = False
                    registry.register(wrapper)
                    logger.info("Registered MCP tool: %s", wrapper.name)

            except Exception as e:
                msg = f"MCP server '{name}': {e}"
                logger.warning(msg)
                errors.append(msg)

        return errors


    async def get_client(self, name: str) -> MCPClient | None:
        # Locking per server name keeps concurrent callers from building two
        # clients for the same config; the re-entrant reconnect reuses the
        # instance so wrappers holding a reference stay valid.
        async with self._lock_for(name):
            client = self._clients.get(name)
            if client is None:
                config = self._configs.get(name)
                if config is None:
                    return None
                client = MCPClient(config)
                await client.connect()
                self._clients[name] = client
                return client

            if not client.is_alive:
                logger.info("Reconnecting MCP server '%s'", name)
                await client.close()
                await client.connect()

            return client


    async def shutdown(self) -> None:
        for name, client in self._clients.items():
            try:
                await client.close()
                logger.info("MCP server '%s' closed", name)
            except Exception:
                logger.debug("Error closing MCP server '%s'", name, exc_info=True)
        self._clients.clear()
