from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mewcode.tools import ToolRegistry

if TYPE_CHECKING:
    from mewcode.agents.parser import AgentDef
    from mewcode.teams.manager import TeamManager

ALL_AGENT_DISALLOWED_TOOLS: frozenset[str] = frozenset({
    "TaskOutput",
    "ExitPlanMode",
    "EnterPlanMode",
    "Agent",
    "AskUserQuestion",
    "TaskStop",
    "Workflow",
})

CUSTOM_AGENT_DISALLOWED_TOOLS: frozenset[str] = frozenset({
    "TaskOutput",
    "ExitPlanMode",
    "EnterPlanMode",
    "Agent",
    "AskUserQuestion",
    "TaskStop",
    "Workflow",
})

ASYNC_AGENT_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "ReadFile",
    "WebSearch",
    "TodoWrite",
    "Grep",
    "WebFetch",
    "Glob",
    "Bash",
    "EditFile",
    "WriteFile",
    "NotebookEdit",
    "Skill",
    "LoadSkill",
    "SyntheticOutput",
    "ToolSearch",
    "EnterWorktree",
    "ExitWorktree",
})

TEAMMATE_COORDINATION_TOOLS: frozenset[str] = frozenset({
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "SendMessage",
})

IN_PROCESS_TEAMMATE_ALLOWED_TOOLS: frozenset[str] = (
    ASYNC_AGENT_ALLOWED_TOOLS | TEAMMATE_COORDINATION_TOOLS | frozenset({
        "CronCreate",
        "CronDelete",
        "CronList",
    })
)

COORDINATOR_MODE_ALLOWED_TOOLS: frozenset[str] = frozenset({
    "Agent",
    "TaskStop",
    "SendMessage",
    "SyntheticOutput",
    "TeamCreate",
    "TeamDelete",
})


def _is_mcp_tool(name: str) -> bool:
    return name.startswith("mcp__")


def resolve_agent_tools(
    parent_registry: ToolRegistry,
    definition: AgentDef,
    is_background: bool = False,
) -> ToolRegistry:
    all_tools = {t.name: t for t in parent_registry.list_tools()}

    # Layer 0: MCP tools always pass through — separate before filtering
    mcp_tools = {name: tool for name, tool in all_tools.items() if _is_mcp_tool(name)}
    all_tools = {name: tool for name, tool in all_tools.items() if not _is_mcp_tool(name)}

    # Layer 1: global disallowed
    for name in ALL_AGENT_DISALLOWED_TOOLS:
        all_tools.pop(name, None)

    # Layer 2: custom agent extra restrictions
    if definition.source in ("project", "user", "plugin"):
        for name in CUSTOM_AGENT_DISALLOWED_TOOLS:
            all_tools.pop(name, None)

    # Layer 3: background whitelist
    if is_background:
        all_tools = {
            name: tool
            for name, tool in all_tools.items()
            if name in ASYNC_AGENT_ALLOWED_TOOLS
        }

    # Layer 4: definition disallowed + allowed
    if definition.disallowed_tools:
        for name in definition.disallowed_tools:
            all_tools.pop(name, None)

    if definition.tools:
        allowed_set = set(definition.tools)
        all_tools = {
            name: tool
            for name, tool in all_tools.items()
            if name in allowed_set
        }

    filtered = ToolRegistry()
    for tool in mcp_tools.values():
        filtered.register(tool)
    for tool in all_tools.values():
        filtered.register(tool)
    return filtered


def build_teammate_tools(
    parent_registry: ToolRegistry,
    team_manager: TeamManager,
    team_name: str,
    agent_id: str,
    agent_name: str,
    backend_type: str,
    definition: AgentDef | None = None,
) -> ToolRegistry:
    from mewcode.teams.models import BackendType
    from mewcode.tools.send_message import SendMessageTool
    from mewcode.tools.task_create import TaskCreateTool
    from mewcode.tools.task_get import TaskGetTool
    from mewcode.tools.task_list import TaskListTool
    from mewcode.tools.task_update import TaskUpdateTool

    if backend_type == BackendType.IN_PROCESS.value:
        all_tools = {t.name: t for t in parent_registry.list_tools()}
        filtered = {
            name: tool
            for name, tool in all_tools.items()
            if name in IN_PROCESS_TEAMMATE_ALLOWED_TOOLS
        }
    else:
        filtered = {t.name: t for t in parent_registry.list_tools()}
        filtered.pop("TeamCreate", None)
        filtered.pop("TeamDelete", None)

    # Apply agent definition restrictions
    if definition is not None:
        if definition.disallowed_tools:
            for name in definition.disallowed_tools:
                filtered.pop(name, None)
        if definition.tools:
            allowed_set = set(definition.tools) | TEAMMATE_COORDINATION_TOOLS
            filtered = {
                name: tool
                for name, tool in filtered.items()
                if name in allowed_set
            }

    coordination_tools = [
        TaskCreateTool(team_manager, team_name, agent_name),
        TaskGetTool(team_manager, team_name),
        TaskListTool(team_manager, team_name),
        TaskUpdateTool(team_manager, team_name),
        SendMessageTool(team_manager, team_name, agent_id, agent_name),
    ]

    registry = ToolRegistry()
    for tool in filtered.values():
        registry.register(tool)
    for tool in coordination_tools:
        registry.register(tool)

    return registry


def apply_coordinator_filter(registry: ToolRegistry) -> ToolRegistry:
    all_tools = {t.name: t for t in registry.list_tools()}
    filtered = ToolRegistry()
    for name, tool in all_tools.items():
        if name in COORDINATOR_MODE_ALLOWED_TOOLS:
            filtered.register(tool)
    return filtered
