"""F3.10: MCP connect 并发保护。

回归场景（审查报告）：
- ``MCPClient.connect`` 先查 ``_alive`` 再整体覆盖 ``_stack/_session`` 且无锁，
  两个协程并发进入会让第一个 stdio 子进程句柄失去引用（僵尸进程）。
- ``MCPManager.get_client`` 并发冷启动会构造两个 client，一个被覆盖泄漏；
  重连分支替换实例会让 ``MCPToolWrapper._client`` 指向失效的旧实例。
- ``tool_wrapper`` 曾跨模块写 ``client._alive`` 私有属性，改用公开
  ``mark_unhealthy()``。
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

import mewcode.mcp.manager as manager_mod
from mewcode.config import MCPServerConfig
from mewcode.mcp.client import MCPClient
from mewcode.mcp.manager import MCPManager


class FakeSession:
    """Stand-in for ClientSession recording how many were constructed."""

    instances: list["FakeSession"] = []

    def __init__(self, read: Any, write: Any) -> None:
        FakeSession.instances.append(self)

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def initialize(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_fake_session() -> None:
    FakeSession.instances = []
    yield
    FakeSession.instances = []


@pytest.fixture()
def fake_transport(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch stdio transport so connect() never spawns a real process.

    Returns a list recording each transport creation (one entry per
    stdio handshake). The sleep widens the race window so an unguarded
    connect() deterministically runs twice concurrently.
    """
    creations: list[str] = []

    async def fake_connect_stdio(self: MCPClient) -> tuple[Any, Any]:
        creations.append(self.name)
        await asyncio.sleep(0.05)
        return AsyncMock(), AsyncMock()

    monkeypatch.setattr(MCPClient, "_connect_stdio", fake_connect_stdio)
    monkeypatch.setattr("mewcode.mcp.client.ClientSession", FakeSession)
    return creations


@pytest.mark.asyncio
async def test_concurrent_connect_creates_single_session(
    fake_transport: list[str],
) -> None:
    client = MCPClient(MCPServerConfig(name="srv", command="fake"))

    await asyncio.gather(client.connect(), client.connect())

    assert len(fake_transport) == 1
    assert client.is_alive is True


@pytest.mark.asyncio
async def test_concurrent_get_client_creates_single_client(
    fake_transport: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed: list[str] = []

    class CountingClient(MCPClient):
        def __init__(self, config: MCPServerConfig) -> None:
            constructed.append(config.name)
            super().__init__(config)

    monkeypatch.setattr(manager_mod, "MCPClient", CountingClient)

    manager = MCPManager()
    manager.load_configs([MCPServerConfig(name="srv", command="fake")])

    first, second = await asyncio.gather(
        manager.get_client("srv"), manager.get_client("srv")
    )

    assert constructed == ["srv"]
    assert first is second
    assert manager._clients["srv"] is first


@pytest.mark.asyncio
async def test_reconnect_reuses_same_client_instance(
    fake_transport: list[str],
) -> None:
    manager = MCPManager()
    manager.load_configs([MCPServerConfig(name="srv", command="fake")])

    first = await manager.get_client("srv")
    assert first is not None
    first.mark_unhealthy()

    second = await manager.get_client("srv")

    assert first is second
    assert first.is_alive is True


def test_mark_unhealthy_is_public_api() -> None:
    client = MCPClient(MCPServerConfig(name="srv", command="fake"))
    client._alive = True

    client.mark_unhealthy()

    assert client.is_alive is False


@pytest.mark.asyncio
async def test_wrapper_marks_unhealthy_on_call_failure(
    fake_transport: list[str],
) -> None:
    from mewcode.mcp.tool_wrapper import MCPToolWrapper

    client = MCPClient(MCPServerConfig(name="srv", command="fake"))
    await client.connect()

    tool_def = _tool_def("echo")
    wrapper = MCPToolWrapper("srv", tool_def, client)

    async def failing_call(name: str, arguments: dict[str, Any]) -> Any:
        raise RuntimeError("connection closed")

    client.call_tool = failing_call  # type: ignore[method-assign]

    params = wrapper.params_model()
    result = await wrapper.execute(params)

    assert result.is_error is True
    assert client.is_alive is False


def _tool_def(name: str) -> Any:
    from mcp import types as mcp_types

    return mcp_types.Tool(
        name=name,
        description="test tool",
        inputSchema={"type": "object", "properties": {}},
    )
