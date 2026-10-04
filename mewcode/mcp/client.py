from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from mewcode.config import MCPServerConfig, build_child_env, resolve_env_vars

logger = logging.getLogger(__name__)


class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.name = config.name
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._alive = False
        # Serializes connect/close: concurrent callers would each build an
        # AsyncExitStack and the loser's stdio child process would leak.
        self._lock = asyncio.Lock()


    @property
    def is_alive(self) -> bool:
        return self._alive


    def mark_unhealthy(self) -> None:
        """Flag the connection as broken so the next call reconnects."""
        self._alive = False


    async def connect(self) -> None:
        async with self._lock:
            if self._alive:
                return

            self._stack = AsyncExitStack()
            await self._stack.__aenter__()

            try:
                if self.config.is_stdio:
                    read, write = await self._connect_stdio()
                else:
                    read, write = await self._connect_http()

                session = await self._stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()
                self._session = session
                self._alive = True
                logger.info("MCP server '%s' connected", self.name)
            except Exception:
                await self._cleanup_stack()
                raise


    async def _connect_stdio(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.command is not None

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=build_child_env(self.config.env),
        )
        devnull = open(os.devnull, "w")  # noqa: SIM115 — 生命周期由 AsyncExitStack 管理
        self._stack.callback(devnull.close)
        read, write = await self._stack.enter_async_context(
            stdio_client(params, errlog=devnull)
        )
        return read, write

    async def _connect_http(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.url is not None

        resolved_headers = {
            k: resolve_env_vars(v) for k, v in self.config.headers.items()
        }
        http_client = httpx.AsyncClient(
            headers=resolved_headers,
            follow_redirects=True,
        )
        await self._stack.enter_async_context(http_client)

        result = await self._stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=http_client)
        )
        read, write = result[0], result[1]
        return read, write


    async def list_tools(self) -> list[types.Tool]:
        assert self._session is not None
        result = await self._session.list_tools()
        return list(result.tools)


    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        assert self._session is not None
        return await self._session.call_tool(name, arguments)

    async def close(self) -> None:
        async with self._lock:
            self._alive = False
            self._session = None
            await self._cleanup_stack()

    async def _cleanup_stack(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug("Cancel scope cleanup (expected during shutdown): %s", e)
                else:
                    raise
            except asyncio.CancelledError as e:
                # MCP 的 stdio 传输基于 anyio cancel scope：收尾时它会以
                # CancelledError 的形式抛出"作用域内取消"（"Cancelled via
                # cancel scope ..."），这不是调用方在取消我们。真机踩到：
                # 容器里 agent 已经干完活，这个伪取消让进程 exit 1，服务把
                # 成果整份丢掉。
                #
                # 为什么吞掉是安全的：如果调用方**真的**在取消这个任务，原始
                # CancelledError 会在 finally（本函数就在这里被调用）结束后由
                # 解释器继续传播——收尾里吞掉的是这份"内层伪取消"，丢不了
                # 真正的取消信号。`task.cancelling()` 不能用来判别：anyio 用它
                # 自己的作用域取消了宿主任务，计数器此时同样非零。
                logger.debug("MCP stdio cleanup cancelled (scoped cancellation): %s", e)
            except Exception:
                logger.debug("Error closing stack for '%s'", self.name, exc_info=True)
            self._stack = None
