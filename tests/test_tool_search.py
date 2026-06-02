"""Tests for the Deferred Loading / ToolSearch mechanism."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from mewcode.tools import ToolRegistry
from mewcode.tools.base import Tool, ToolResult
from mewcode.tools.impl.tool_search import ToolSearchTool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _DummyParams(BaseModel):
    text: str = ""

class _NormalTool(Tool):
    name = "NormalTool"
    description = "A normal, non-deferred tool"
    params_model = _DummyParams
    category = "read"
    should_defer = False

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="ok")

class _DeferredTool(Tool):
    name = "DeferredAlpha"
    description = "A deferred tool for testing"
    params_model = _DummyParams
    category = "read"
    should_defer = True

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="deferred ok")

class _DeferredBeta(Tool):
    name = "DeferredBeta"
    description = "Another deferred tool beta variant"
    params_model = _DummyParams
    category = "read"
    should_defer = True

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output="deferred beta ok")

def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_NormalTool())
    reg.register(_DeferredTool())
    reg.register(_DeferredBeta())
    return reg

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_should_defer_default_false():
    """Tool base class defaults should_defer to False."""
    tool = _NormalTool()
    assert tool.should_defer is False

def test_mcp_tool_deferred():
    """MCPToolWrapper sets should_defer = True on construction."""
    # We avoid importing mcp types here; instead check the attribute
    # on a mock-like object that mimics what MCPToolWrapper.__init__ does.
    from unittest.mock import MagicMock

    mock_tool_def = MagicMock()
    mock_tool_def.name = "example"
    mock_tool_def.description = "An MCP tool"
    mock_tool_def.inputSchema = {"type": "object", "properties": {}}

    mock_client = MagicMock()

    from mewcode.mcp.tool_wrapper import MCPToolWrapper

    wrapper = MCPToolWrapper(
        server_name="test_server",
        tool_def=mock_tool_def,
        client=mock_client,
    )
    assert wrapper.should_defer is True

def test_deferred_not_in_schemas():
    """Deferred tools that haven't been discovered should NOT appear in get_all_schemas."""
    reg = _make_registry()
    schemas = reg.get_all_schemas()
    names = {s["name"] for s in schemas}
    assert "NormalTool" in names
    assert "DeferredAlpha" not in names
    assert "DeferredBeta" not in names

@pytest.mark.asyncio
async def test_tool_search_marks_discovered():
    """ToolSearchTool.execute should mark tools as discovered."""
    reg = _make_registry()
    search = ToolSearchTool(reg, protocol="anthropic")
    reg.register(search)

    from mewcode.tools.impl.tool_search import ToolSearchParams

    params = ToolSearchParams(query="select:DeferredAlpha")
    result = await search.execute(params)

    assert not result.is_error
    assert "DeferredAlpha" in result.output
    assert reg.is_discovered("DeferredAlpha")
    assert not reg.is_discovered("DeferredBeta")

def test_discovered_in_schemas():
    """Once a deferred tool is discovered, it should appear in get_all_schemas."""
    reg = _make_registry()
    # Initially not in schemas
    schemas_before = reg.get_all_schemas()
    names_before = {s["name"] for s in schemas_before}
    assert "DeferredAlpha" not in names_before

    # Mark as discovered
    reg.mark_discovered("DeferredAlpha")

    schemas_after = reg.get_all_schemas()
    names_after = {s["name"] for s in schemas_after}
    assert "DeferredAlpha" in names_after
    # DeferredBeta still not discovered
    assert "DeferredBeta" not in names_after

def test_get_deferred_tool_names():
    """get_deferred_tool_names returns only non-discovered deferred tools."""
    reg = _make_registry()
    deferred = reg.get_deferred_tool_names()
    assert "DeferredAlpha" in deferred
    assert "DeferredBeta" in deferred
    assert "NormalTool" not in deferred

    # After discovering one
    reg.mark_discovered("DeferredAlpha")
    deferred2 = reg.get_deferred_tool_names()
    assert "DeferredAlpha" not in deferred2
    assert "DeferredBeta" in deferred2

@pytest.mark.asyncio
async def test_tool_search_keyword():
    """ToolSearchTool keyword search returns matching deferred tools."""
    reg = _make_registry()
    search = ToolSearchTool(reg, protocol="anthropic")
    reg.register(search)

    from mewcode.tools.impl.tool_search import ToolSearchParams

    params = ToolSearchParams(query="beta", max_results=5)
    result = await search.execute(params)

    assert not result.is_error
    assert "DeferredBeta" in result.output
    assert reg.is_discovered("DeferredBeta")

@pytest.mark.asyncio
async def test_tool_search_no_match():
    """ToolSearchTool returns available names when no match is found."""
    reg = _make_registry()
    search = ToolSearchTool(reg, protocol="anthropic")
    reg.register(search)

    from mewcode.tools.impl.tool_search import ToolSearchParams

    params = ToolSearchParams(query="nonexistent_xyz")
    result = await search.execute(params)

    assert "No matching deferred tools" in result.output
    assert "DeferredAlpha" in result.output
    assert "DeferredBeta" in result.output

@pytest.mark.asyncio
async def test_tool_search_select_multiple():
    """select: syntax can load multiple tools at once."""
    reg = _make_registry()
    search = ToolSearchTool(reg, protocol="anthropic")
    reg.register(search)

    from mewcode.tools.impl.tool_search import ToolSearchParams

    params = ToolSearchParams(query="select:DeferredAlpha,DeferredBeta")
    result = await search.execute(params)

    assert not result.is_error
    assert "Found 2 tool(s)" in result.output
    assert reg.is_discovered("DeferredAlpha")
    assert reg.is_discovered("DeferredBeta")

# ---------------------------------------------------------------------------
# Deferred loading: token savings & end-to-end discovery
# ---------------------------------------------------------------------------

class _HeavyParams(BaseModel):
    """A params model with many properties to simulate a realistic schema."""

    alpha: str = ""
    bravo: str = ""
    charlie: int = 0
    delta: float = 0.0
    echo: bool = False
    foxtrot: str = "default_foxtrot_value"
    golf: str = "default_golf_value"
    hotel: int = 42
    india: str = ""
    juliet: bool = True

def _make_deferred_tool(index: int) -> Tool:
    """Dynamically create a deferred tool class with a unique name."""

    class _T(Tool):
        name = f"DeferredHeavy_{index:03d}"
        description = (
            f"Deferred heavy tool number {index} that provides advanced "
            f"functionality for processing, transforming, and analyzing data "
            f"in context {index}."
        )
        params_model = _HeavyParams
        category = "read"
        should_defer = True

        async def execute(self, params: BaseModel) -> ToolResult:
            return ToolResult(output=f"heavy {index}")

    return _T()

def test_deferred_token_savings():
    """Deferred loading should save >= 90% of schema tokens for 50 heavy tools."""
    import json

    reg = ToolRegistry()

    # 2 normal tools
    reg.register(_NormalTool())

    class _Normal2(Tool):
        name = "NormalTool2"
        description = "Second normal tool"
        params_model = _DummyParams
        category = "read"
        should_defer = False

        async def execute(self, params: BaseModel) -> ToolResult:
            return ToolResult(output="ok2")

    reg.register(_Normal2())

    # 50 deferred tools with realistic schemas
    deferred_names: list[str] = []
    for i in range(50):
        tool = _make_deferred_tool(i)
        reg.register(tool)
        deferred_names.append(tool.name)

    # Measure size with deferred tools hidden
    schemas_deferred = reg.get_all_schemas("anthropic")
    size_deferred = len(json.dumps(schemas_deferred))

    # Discover all deferred tools
    for name in deferred_names:
        reg.mark_discovered(name)

    # Measure size with all tools visible
    schemas_all = reg.get_all_schemas("anthropic")
    size_all = len(json.dumps(schemas_all))

    savings = 1 - size_deferred / size_all
    print(
        f"\nDeferred token savings: {savings:.1%} "
        f"(deferred={size_deferred}, all={size_all})"
    )
    assert savings >= 0.90, (
        f"Expected >= 90% savings, got {savings:.1%} "
        f"(deferred={size_deferred}, all={size_all})"
    )

def test_deferred_end_to_end_discovery():
    """End-to-end: deferred tools start hidden, appear after discovery."""
    reg = ToolRegistry()

    # 1 normal tool
    reg.register(_NormalTool())

    # 2 deferred tools
    reg.register(_DeferredTool())   # DeferredAlpha
    reg.register(_DeferredBeta())   # DeferredBeta

    # --- Initially: deferred tools hidden from schemas ---
    schemas = reg.get_all_schemas("anthropic")
    schema_names = {s["name"] for s in schemas}
    assert "NormalTool" in schema_names
    assert "DeferredAlpha" not in schema_names
    assert "DeferredBeta" not in schema_names

    # --- get_deferred_tool_names lists both ---
    deferred = reg.get_deferred_tool_names()
    assert "DeferredAlpha" in deferred
    assert "DeferredBeta" in deferred

    # --- Discover one ---
    reg.mark_discovered("DeferredAlpha")

    schemas2 = reg.get_all_schemas("anthropic")
    schema_names2 = {s["name"] for s in schemas2}
    assert "DeferredAlpha" in schema_names2
    assert "DeferredBeta" not in schema_names2

    # --- get_deferred_tool_names now returns only the other ---
    deferred2 = reg.get_deferred_tool_names()
    assert "DeferredAlpha" not in deferred2
    assert "DeferredBeta" in deferred2
