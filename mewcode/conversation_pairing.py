"""Normalize tool protocol structure on a disposable request copy only.

Results bind FIFO within the nearest preceding call batch for their original ID. This
also handles duplicate IDs without inventing execution facts. Late results move
next to their call; unmatched/duplicate outputs remain ordinary user evidence.
"""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import replace

from mewcode.conversation import Message, ToolResultBlock, ToolUseBlock

INTERRUPTED_RESULT = (
    "Tool execution was interrupted or its result was not recorded. Execution status is unknown; "
    "side effects may already have occurred. Inspect the actual state before retrying."
)


def pair_request_messages(messages: list[Message]) -> list[Message]:
    """Preserve signed thinking and real outputs; never mutate durable history.

    Normal unique-ID histories keep their contents and result order.
    Malformed roles are derived from block kind. Empty shells are
    omitted; mixed text/results are split so serialization cannot hide text.
    """
    copied = deepcopy(messages)
    reserved = {u.tool_use_id for m in copied for u in m.tool_uses}
    used: set[str] = set()
    pending: dict[str, deque[str]] = defaultdict(deque)
    outputs: dict[str, ToolResultBlock] = {}
    output_order: dict[str, int] = {}
    uses_by_message: dict[int, list[ToolUseBlock]] = {}
    unpaired: dict[int, list[ToolResultBlock]] = defaultdict(list)
    sequence = 0
    for index, message in enumerate(copied):
        uses = []
        for original in {u.tool_use_id for u in message.tool_uses}:
            # Reuse in a later turn must not steal that turn's real result for
            # an earlier interrupted invocation. Duplicates in one batch use FIFO.
            pending[original].clear()
        for use in message.tool_uses:
            original = use.tool_use_id
            identifier = original
            if not identifier or identifier in used:
                suffix = len(used)
                identifier = f"request_tool_{suffix}"
                while identifier in reserved or identifier in used:
                    suffix += 1
                    identifier = f"request_tool_{suffix}"
            used.add(identifier)
            uses.append(replace(use, tool_use_id=identifier))
            pending[original].append(identifier)
        uses_by_message[index] = uses
        for result in message.tool_results:
            queue = pending[result.tool_use_id]
            if queue:
                identifier = queue.popleft()
                outputs[identifier] = replace(result, tool_use_id=identifier)
                output_order[identifier] = sequence
                sequence += 1
            else:
                unpaired[index].append(result)

    normalized = []
    for index, message in enumerate(copied):
        uses = uses_by_message[index]
        if uses:
            normalized.append(replace(message, role="assistant", tool_uses=uses, tool_results=[]))
            results = [outputs.get(u.tool_use_id, ToolResultBlock(u.tool_use_id, INTERRUPTED_RESULT, True))
                       for u in uses]
            results.sort(key=lambda r: output_order.get(r.tool_use_id, sequence))
            normalized.append(Message("user", "", tool_results=results))
        elif message.content or message.thinking_blocks:
            normalized.append(replace(message, tool_results=[]))
        for result in unpaired[index]:
            normalized.append(Message(
                "user", f"[Unpaired tool output {result.tool_use_id}; execution association unknown]\n{result.content}",
            ))
    return normalized
