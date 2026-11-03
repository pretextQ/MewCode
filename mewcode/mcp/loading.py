"""Explicit MCP loading strategies and identity resolution before existing Agent gates."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError

from mewcode.mcp.tool_wrapper import MCPToolWrapper
from mewcode.tools.base import Tool, ToolCallComplete, ToolResult

if TYPE_CHECKING:
    from mewcode.tools import ToolRegistry

MODES = {'legacy', 'eager', 'dispatch', 'auto'}


class MCPCallParams(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    tool_name: str
    arguments: dict[str, Any]


class MCPCall(Tool[MCPCallParams]):
    name = 'MCPCall'
    description = ('Call an MCP tool by its exact registered tool_name and arguments. '
                   'Use ToolSearch to obtain its complete schema first.')
    params_model = MCPCallParams
    category = 'command'

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def for_registry(self, registry: ToolRegistry) -> MCPCall:
        return MCPCall(registry)

    def target(self, params: MCPCallParams) -> MCPToolWrapper | None:
        tool = self._registry.get(params.tool_name)
        if isinstance(tool, MCPToolWrapper) and tool.dispatch_only and self._registry.is_enabled(tool.name):
            return tool
        return None

    async def execute(self, params: MCPCallParams) -> ToolResult:
        # No execution here: only the Agent can apply all real-target gates.
        reason = 'unknown, disabled, filtered, unavailable or non-MCP target'
        if self.target(params) is not None:
            reason = 'MCPCall requires the Agent execution pipeline'
        return ToolResult(output=f'MCP dispatch rejected: {reason}', is_error=True)


def resolve_dispatch(registry: ToolRegistry, call: ToolCallComplete) -> ToolCallComplete:
    dispatcher = registry.get(call.tool_name)
    if not isinstance(dispatcher, MCPCall) or not registry.is_enabled(call.tool_name):
        return call
    try:
        params = MCPCallParams.model_validate(call.arguments)
    except ValidationError:
        return call  # Existing validation produces the paired error result.
    target = dispatcher.target(params)
    if target is None:
        return call
    return ToolCallComplete(call.tool_id, target.name, params.arguments)


@dataclass(frozen=True)
class LoadingChoice:
    requested: str
    selected: str
    schema_bytes: int
    estimated_tokens: int
    threshold_tokens: int


def configure_loading(
    registry: ToolRegistry, mode: str = 'legacy', context_window: int = 200_000, protocol: str = 'anthropic',
) -> LoadingChoice:
    if mode not in MODES:
        raise ValueError(f'Unknown MCP loading mode: {mode}')
    tools = [t for t in registry.list_tools() if isinstance(t, MCPToolWrapper) and registry.is_enabled(t.name)]
    schema_bytes = len(json.dumps([t.get_schema() for t in tools], ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8'))
    estimated = (schema_bytes + 3) // 4
    threshold = min(8192, max(1, context_window // 10))
    selected = ('dispatch' if estimated > threshold else 'eager') if mode == 'auto' else mode
    if selected == 'dispatch' and tools and registry.get('MCPCall') is not None and not isinstance(
        registry.get('MCPCall'), MCPCall
    ):
        for tool in tools:
            registry.reject_collision(tool.name)
        raise ValueError('MCP dispatcher name collision: MCPCall; MCP targets blocked')
    for original in tools:
        tool = copy.copy(original)
        tool.dispatch_only = selected == 'dispatch'
        tool.should_defer = tool.legacy_defer if selected == 'legacy' else selected == 'dispatch'
        tool.is_concurrency_safe = tool.category == 'read' and not tool.dispatch_only
        registry.register(tool)
    if selected == 'dispatch' and tools:
        from mewcode.tools.impl.tool_search import ToolSearchTool
        registry.register(MCPCall(registry))
        if registry.get('ToolSearch') is None:
            registry.register(ToolSearchTool(registry, protocol))
    elif isinstance(registry.get('MCPCall'), MCPCall):
        registry.disable('MCPCall')
    return LoadingChoice(mode, selected, schema_bytes, estimated, threshold)
