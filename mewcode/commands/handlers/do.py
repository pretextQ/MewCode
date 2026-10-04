from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType


async def handle_do(ctx: CommandContext) -> None:
    ctx.ui.set_plan_mode(False)
    ctx.ui.add_system_message("已切换到执行模式 — 写入和命令需要确认")


DO_COMMAND = Command(
    name="do",
    description="切换到执行模式",
    usage="/do",
    type=CommandType.LOCAL_UI,
    handler=handle_do,
)

