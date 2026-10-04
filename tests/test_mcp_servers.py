"""M2 W3：内置只读 MCP server（logs / ci）的测试。

这里不 mock 协议层——用**真实的 stdio 客户端**连**真实的 server 子进程**，
后端是进程内的假 Loki / 假 GitHub。这样验证的是完整链路（spawn、握手、
工具表、参数 schema、调用、结果回读），而不是"我们以为会发什么请求"。

只读是硬约束：假后端对任何非 GET 一律 405 并记录，测试断言没有出现过写尝试。
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers.fake_backends import DEFAULT_LOG_LINE, make_github, make_loki  # noqa: E402

from mewcode.config import MCPServerConfig  # noqa: E402
from mewcode.mcp.client import MCPClient  # noqa: E402


@pytest.fixture
def loki():
    backend = make_loki().start()
    try:
        yield backend
    finally:
        backend.stop()


@pytest.fixture
def github():
    backend = make_github().start()
    try:
        yield backend
    finally:
        backend.stop()


@asynccontextmanager
async def mcp_session(module: str, env: dict[str, str]):
    """连一个内置 server 子进程，返回 (client, {tool_name: types.Tool})。"""
    config = MCPServerConfig(
        name=module.rsplit(".", 1)[-1],
        command=sys.executable,
        args=["-m", module],
        env=env,
    )
    client = MCPClient(config)
    await client.connect()
    try:
        yield client, {t.name: t for t in await client.list_tools()}
    finally:
        await client.close()


async def call(client: MCPClient, name: str, **arguments):
    return await client.call_tool(name, arguments)


def text_of(result) -> str:
    from mewcode.mcp.tool_wrapper import _extract_text

    return _extract_text(result.content)


# =========================================================================
# logs server
# =========================================================================

class TestLogsServer:
    @pytest.mark.asyncio
    async def test_tools_are_listed_and_declared_read_only(self, loki) -> None:
        async with mcp_session("mewcode.mcp.servers.logs", {"MEWCODE_LOKI_URL": loki.base_url}) as (_, tools):
            assert set(tools) == {"query_logs", "list_labels"}
            for tool in tools.values():
                assert tool.annotations is not None and tool.annotations.readOnlyHint is True

    @pytest.mark.asyncio
    async def test_query_logs_returns_formatted_lines(self, loki) -> None:
        async with mcp_session("mewcode.mcp.servers.logs", {"MEWCODE_LOKI_URL": loki.base_url}) as (client, tools):
            result = await call(client, "query_logs", query='{app="checkout"}', minutes=15, limit=10)

        assert not result.isError
        output = text_of(result)
        assert DEFAULT_LOG_LINE in output
        assert "app=checkout" in output and "env=prod" in output
        # 请求真的发到了 Loki 的 query_range，参数来自工具入参
        request = next(r for r in loki.requests if r.path.endswith("/query_range"))
        assert request.method == "GET"
        assert request.params["query"] == ['{app="checkout"}']
        assert request.params["limit"] == ["10"]

    @pytest.mark.asyncio
    async def test_query_logs_with_no_matches_says_so(self) -> None:
        backend = make_loki(lines=[]).start()
        try:
            async with mcp_session("mewcode.mcp.servers.logs", {"MEWCODE_LOKI_URL": backend.base_url}) as (client, _):
                result = await call(client, "query_logs", query='{app="nope"}')
        finally:
            backend.stop()
        assert not result.isError
        assert "No log lines matched" in text_of(result)

    @pytest.mark.asyncio
    async def test_unconfigured_platform_reports_clearly(self) -> None:
        async with mcp_session("mewcode.mcp.servers.logs", {}) as (client, _):
            result = await call(client, "query_logs", query='{app="x"}')
        assert result.isError
        assert "MEWCODE_LOKI_URL" in text_of(result)

    @pytest.mark.asyncio
    async def test_backend_http_error_is_reported_not_swallowed(self) -> None:
        backend = make_loki(fail_status=500).start()
        try:
            async with mcp_session("mewcode.mcp.servers.logs", {"MEWCODE_LOKI_URL": backend.base_url}) as (client, _):
                result = await call(client, "query_logs", query='{app="x"}')
        finally:
            backend.stop()
        assert result.isError
        assert "HTTP 500" in text_of(result)

    @pytest.mark.asyncio
    async def test_unreachable_platform_reports_clearly(self, monkeypatch) -> None:
        """连接失败要给出"不可达"而不是抛裸异常。

        这里在 ``_http.get_json`` 这一层注入 ConnectError，而不是真去连一个
        关闭的端口：某些环境（本机就是）把任意端口的 TCP 都劫持成 502，
        "连不上"没法用真实端口构造。
        """
        import httpx

        from mewcode.mcp.servers import _http

        class ExplodingClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc) -> None:
                return None

            async def get(self, *args, **kwargs):
                raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(_http.httpx, "AsyncClient", ExplodingClient)
        with pytest.raises(_http.ToolBackendError, match="unreachable"):
            await _http.get_json("http://127.0.0.1:1/loki/api/v1/query_range")

    @pytest.mark.asyncio
    async def test_list_labels_and_values(self, loki) -> None:
        async with mcp_session("mewcode.mcp.servers.logs", {"MEWCODE_LOKI_URL": loki.base_url}) as (client, _):
            names = await call(client, "list_labels")
            values = await call(client, "list_labels", label="app")
        assert "app" in text_of(names) and "env" in text_of(names)
        assert "checkout" in text_of(values)


# =========================================================================
# ci server
# =========================================================================

class TestCIServer:
    @pytest.mark.asyncio
    async def test_tools_are_listed_and_declared_read_only(self, github) -> None:
        env = {"MEWCODE_GITHUB_API": github.base_url}
        async with mcp_session("mewcode.mcp.servers.ci", env) as (_, tools):
            assert set(tools) == {"list_workflow_runs", "get_check_runs"}
            for tool in tools.values():
                assert tool.annotations is not None and tool.annotations.readOnlyHint is True

    @pytest.mark.asyncio
    async def test_list_workflow_runs_summarises(self, github) -> None:
        env = {"MEWCODE_GITHUB_API": github.base_url}
        async with mcp_session("mewcode.mcp.servers.ci", env) as (client, _):
            result = await call(client, "list_workflow_runs", repo="demo/repo", branch="main")

        output = text_of(result)
        assert not result.isError
        assert "#41" in output and "conclusion=failure" in output and "pytest" in output
        request = next(r for r in github.requests if r.path.endswith("/actions/runs"))
        assert request.params["branch"] == ["main"]

    @pytest.mark.asyncio
    async def test_get_check_runs_summarises(self, github) -> None:
        env = {"MEWCODE_GITHUB_API": github.base_url}
        async with mcp_session("mewcode.mcp.servers.ci", env) as (client, _):
            result = await call(client, "get_check_runs", repo="demo/repo", ref="main")
        assert "pytest (ubuntu-latest)" in text_of(result)
        assert "conclusion=success" in text_of(result)

    @pytest.mark.asyncio
    async def test_unknown_repo_surfaces_http_error(self, github) -> None:
        env = {"MEWCODE_GITHUB_API": github.base_url}
        async with mcp_session("mewcode.mcp.servers.ci", env) as (client, _):
            result = await call(client, "get_check_runs", repo="missing/repo", ref="main")
        assert result.isError
        assert "404" in text_of(result)

    @pytest.mark.asyncio
    async def test_invalid_slug_is_rejected_before_any_request(self, github) -> None:
        env = {"MEWCODE_GITHUB_API": github.base_url}
        async with mcp_session("mewcode.mcp.servers.ci", env) as (client, _):
            result = await call(client, "list_workflow_runs", repo="not-a-slug")
        assert result.isError
        assert "owner/name" in text_of(result)
        assert github.requests == []

    @pytest.mark.asyncio
    async def test_token_is_sent_only_when_configured(self, github, monkeypatch) -> None:
        env = {"MEWCODE_GITHUB_API": github.base_url}
        async with mcp_session("mewcode.mcp.servers.ci", env) as (client, _):
            await call(client, "list_workflow_runs", repo="demo/repo")
        assert "Authorization" not in github.requests[-1].headers

        monkeypatch.setenv("SECRET_CI_TOKEN", "tok-123")
        env = {"MEWCODE_GITHUB_API": github.base_url, "GITHUB_TOKEN": "${SECRET_CI_TOKEN}"}
        async with mcp_session("mewcode.mcp.servers.ci", env) as (client, _):
            await call(client, "list_workflow_runs", repo="demo/repo")
        assert github.requests[-1].headers.get("Authorization") == "Bearer tok-123"


# =========================================================================
# 只读是硬约束：整条链路没有非 GET 请求
# =========================================================================

@pytest.mark.asyncio
async def test_no_write_requests_reach_the_backends(loki, github) -> None:
    async with mcp_session("mewcode.mcp.servers.logs", {"MEWCODE_LOKI_URL": loki.base_url}) as (client, _):
        await call(client, "query_logs", query='{app="checkout"}')
        await call(client, "list_labels")
    env = {"MEWCODE_GITHUB_API": github.base_url}
    async with mcp_session("mewcode.mcp.servers.ci", env) as (client, _):
        await call(client, "list_workflow_runs", repo="demo/repo")
        await call(client, "get_check_runs", repo="demo/repo", ref="main")

    methods = {r.method for r in loki.requests + github.requests}
    assert methods == {"GET"}, f"non-GET requests reached the backends: {methods}"


# =========================================================================
# 只读声明如何影响服务模式下的工具过滤
# =========================================================================

class TestReadOnlyEnforcement:
    def _tool(self, name: str, read_only: bool | None):
        from mcp import types as mcp_types

        annotations = None if read_only is None else mcp_types.ToolAnnotations(readOnlyHint=read_only)
        return mcp_types.Tool(name=name, description=name, inputSchema={"type": "object"}, annotations=annotations)

    def test_wrapper_category_follows_annotation(self) -> None:
        from mewcode.mcp.tool_wrapper import MCPToolWrapper, is_read_only_tool

        class FakeClient:
            is_alive = True

        read_tool = MCPToolWrapper("s", self._tool("read_thing", True), FakeClient())  # type: ignore[arg-type]
        write_tool = MCPToolWrapper("s", self._tool("write_thing", False), FakeClient())  # type: ignore[arg-type]
        undeclared = MCPToolWrapper("s", self._tool("mystery", None), FakeClient())  # type: ignore[arg-type]

        assert is_read_only_tool(self._tool("t", True)) is True
        assert is_read_only_tool(self._tool("t", None)) is False
        assert read_tool.category == "read" and read_tool.is_read_only
        assert write_tool.category == "command" and not write_tool.is_read_only
        assert undeclared.category == "command" and not undeclared.is_read_only

    @pytest.mark.asyncio
    async def test_manager_skips_tools_without_read_only_hint(self) -> None:
        from unittest.mock import AsyncMock, patch

        from mewcode.mcp.manager import MCPManager
        from mewcode.tools import ToolRegistry

        manager = MCPManager()
        manager.load_configs([MCPServerConfig(name="srv", command="whatever")])
        registry = ToolRegistry()

        with patch("mewcode.mcp.manager.MCPClient") as MockClient:
            instance = AsyncMock()
            instance.is_alive = True
            instance.list_tools.return_value = [
                self._tool("read_thing", True),
                self._tool("write_thing", False),
                self._tool("mystery", None),
            ]
            MockClient.return_value = instance

            errors = await manager.register_all_tools(registry, read_only_only=True)

        assert registry.get("mcp_srv_read_thing") is not None
        assert registry.get("mcp_srv_write_thing") is None
        assert registry.get("mcp_srv_mystery") is None
        assert len(errors) == 2 and all("skipped non-read-only" in e for e in errors)

    @pytest.mark.asyncio
    async def test_manager_keeps_all_tools_when_not_restricted(self) -> None:
        from unittest.mock import AsyncMock, patch

        from mewcode.mcp.manager import MCPManager
        from mewcode.tools import ToolRegistry

        manager = MCPManager()
        manager.load_configs([MCPServerConfig(name="srv", command="whatever")])
        registry = ToolRegistry()

        with patch("mewcode.mcp.manager.MCPClient") as MockClient:
            instance = AsyncMock()
            instance.is_alive = True
            instance.list_tools.return_value = [self._tool("write_thing", False)]
            MockClient.return_value = instance

            errors = await manager.register_all_tools(registry, read_only_only=False)

        assert registry.get("mcp_srv_write_thing") is not None
        assert errors == []


# =========================================================================
# -p 模式的 JSON 摘要带上内部工具使用证据
# =========================================================================

class TestHeadlessJsonPayload:
    def test_json_payload_counts_mcp_calls(self) -> None:
        """``-p`` 的摘要契约：服务层（含容器回读）靠这些字段判断内部工具用没用。"""
        from mewcode.__main__ import _summary_payload

        class FakeAgent:
            total_input_tokens = 1200
            total_output_tokens = 300
            session_id = "sess-1"

        payload = _summary_payload(
            FakeAgent(), "ROOT CAUSE: x", {"tool_calls": 3, "mcp_calls": 2},
            {"mcp_ci_get_check_runs", "mcp_logs_query_logs"},
        )
        assert payload == {
            "result": "ROOT CAUSE: x",
            "usage": {"inputTokens": 1200, "outputTokens": 300},
            "toolCalls": 3,
            "mcpCalls": 2,
            "mcpTools": ["mcp_ci_get_check_runs", "mcp_logs_query_logs"],
            "sessionId": "sess-1",
        }

    def test_payload_without_mcp_usage(self) -> None:
        from mewcode.__main__ import _summary_payload

        class FakeAgent:
            total_input_tokens = 1
            total_output_tokens = 1
            session_id = "s"

        payload = _summary_payload(FakeAgent(), "done", {"tool_calls": 0, "mcp_calls": 0}, set())
        assert payload["mcpCalls"] == 0 and payload["mcpTools"] == []


def test_probe_helper_is_importable() -> None:
    """容器内探针与假后端脚本必须能被 import（沙箱真机测试依赖它们）。"""
    assert os.path.exists(Path(__file__).parent / "helpers" / "mcp_probe.py")
    assert os.path.exists(Path(__file__).parent / "helpers" / "fake_backends.py")
