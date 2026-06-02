from __future__ import annotations

from mewcode.commands.registry import Command, CommandContext, CommandType


async def handle_compact(ctx: CommandContext) -> None:
    if ctx.agent is None:
        ctx.ui.add_system_message("Agent 未初始化")
        return


    input_tokens, _ = ctx.ui.get_token_count()
    if input_tokens < 5000:
        ctx.ui.add_system_message(f"当前 token 数 {input_tokens:,}，无需压缩")
        return

    from mewcode.agent import CompactNotification, ErrorEvent


    result = await ctx.agent.manual_compact(ctx.conversation)
    if isinstance(result, CompactNotification):
        ctx.ui.add_system_message(result.message)
    elif isinstance(result, ErrorEvent):
        ctx.ui.add_system_message(f"压缩失败: {result.message}")


COMPACT_COMMAND = Command(
    name="compact",
    aliases=["c"],
    description="压缩上下文",
    usage="/compact [保留重点]",
    type=CommandType.LOCAL,
    handler=handle_compact,
)

