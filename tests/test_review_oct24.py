"""Regression coverage for the October 24 review."""
from types import SimpleNamespace

import pytest

from mewcode.agents.parser import AgentDef
from mewcode.agents.tool_filter import resolve_agent_tools
from mewcode.tools import ToolRegistry


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("name", ["mcp_files_delete", "mcp__files__delete"])
def test_mcp_agent_restrictions(name, background):
    registry = ToolRegistry()
    registry.register(SimpleNamespace(name=name))
    definition = AgentDef("restricted", "test", disallowed_tools=["mcp*files*"])
    assert resolve_agent_tools(registry, definition, background).get(name) is None
    definition.disallowed_tools = []
    definition.tools = ["ReadFile"]
    assert resolve_agent_tools(registry, definition, background).get(name) is None
