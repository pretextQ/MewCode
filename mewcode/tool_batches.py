"""Ordered query batches with ownership through cancellation and shared entry policies."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from mewcode.tools import ToolRegistry
from mewcode.tools.base import ToolCallComplete, ToolResult

T = TypeVar('T')


@dataclass
class ToolBatch:
    concurrent: bool
    calls: list[ToolCallComplete]


def partition_tool_calls(tool_calls: list[ToolCallComplete], registry: ToolRegistry) -> list[ToolBatch]:
    batches: list[ToolBatch] = []
    for call in tool_calls:
        tool = registry.get(call.tool_name)
        safe = (tool is not None and registry.is_enabled(call.tool_name)
                and tool.can_run_concurrently(call.arguments) is True)
        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(call)
        else:
            batches.append(ToolBatch(concurrent=safe, calls=[call]))
    return batches


async def gather_tool_calls(calls: Iterable[Coroutine[Any, Any, T]]) -> list[T | BaseException]:
    tasks = [asyncio.create_task(call) for call in calls]
    try:
        return await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        async def drain() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)
        cleanup = asyncio.create_task(drain())
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
        cleanup.result()
        raise


async def noninteractive_batches(
    calls: list[ToolCallComplete], registry: ToolRegistry,
    execute: Callable[[ToolCallComplete], Coroutine[Any, Any, ToolResult]],
    callback: Callable[[dict[str, Any]], None] | None = None,
) -> AsyncIterator[tuple[ToolCallComplete, ToolResult]]:
    for batch in partition_tool_calls(calls, registry):
        resolved = [registry.resolve_tool_call(call) for call in batch.calls]
        for call in resolved:
            if callback:
                callback({'type': 'tool_use', 'toolName': call.tool_name, 'args': call.arguments})
        if batch.concurrent and len(resolved) > 1:
            results = await gather_tool_calls(execute(call) for call in resolved)
            for call, result in zip(resolved, results, strict=True):
                if isinstance(result, BaseException):
                    result = ToolResult(output=f'Tool execution error: {result}', is_error=True)
                yield call, result
        else:
            for call in resolved:
                yield call, await execute(call)
