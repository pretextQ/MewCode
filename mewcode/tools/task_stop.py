from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from mewcode.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from mewcode.teams.manager import TeamManager


class TaskStopParams(BaseModel):
    agent_id: str = Field(min_length=1, description="Teammate name or agent ID")
    team_name: str = ""


class TaskStopTool(Tool[TaskStopParams]):
    name = "TaskStop"
    description = (
        "Terminate an owned team worker by name or agent ID and wait for runtime cleanup. "
        "Repeated stops are safe. Stopped workers cannot resume with SendMessage; spawn a new worker. "
        "This does not delete the team, discard edits or remove worktrees."
    )
    params_model = TaskStopParams
    category = "command"
    is_concurrency_safe = False

    def __init__(self, manager: TeamManager, owner_id: str, timeout: float = 5) -> None:
        self._manager, self._owner_id, self._timeout = manager, owner_id, timeout

    async def execute(self, params: TaskStopParams) -> ToolResult:
        teams = [t for t in self._manager._teams.values() if t.lead_agent_id == self._owner_id]
        matches = [(t, m) for t in teams if not params.team_name or t.name == params.team_name
                   for m in t.members if params.agent_id in (m.agent_id, m.name)]
        if len(matches) != 1:
            available = ", ".join(f"{t.name}/{m.name} ({m.agent_id})" for t in teams for m in t.members) or "none"
            return ToolResult(output=f"Unknown or ambiguous owned teammate '{params.agent_id}'. Available: {available}",
                              is_error=True)
        team, member = matches[0]
        try:
            if not await self._manager.stop_member(team.name, member.agent_id, timeout=self._timeout):
                return ToolResult(output=f"Could not confirm worker '{member.name}' stopped; runtime remains tracked.",
                                  is_error=True)
        except Exception as exc:
            return ToolResult(output=f"Worker stop failed ({type(exc).__name__}); cleanup is unconfirmed.",
                              is_error=True)
        return ToolResult(output=f"Worker '{member.name}' is stopped/offline. Worktree and edits retained.")
