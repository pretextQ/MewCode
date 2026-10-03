
from __future__ import annotations

import time

from mewcode.commands.registry import Command, CommandType


async def _handle_rewind(ctx) -> None:
    fh = getattr(ctx.agent, "file_history", None)
    if fh is None or not fh.has_snapshots():
        ctx.ui.add_system_message("No checkpoints to rewind to.")
        return

    snapshots = fh.get_snapshots()

    lines = ["⟲ Rewind — select a checkpoint:\n"]
    for i, snap in enumerate(snapshots):
        ago = int(time.time() - snap.timestamp)
        label = snap.user_text[:50] + "…" if len(snap.user_text) > 50 else snap.user_text
        lines.append(f"  [{i + 1}] {label} ({ago}s ago, {len(snap.backups)} file(s))")
    lines.append("\nOptions after selecting:")
    lines.append("  1) Restore code and conversation")
    lines.append("  2) Restore conversation only")
    lines.append("  3) Restore code only")
    lines.append(f"\nUsage: /rewind <checkpoint> [option]  (e.g. /rewind {len(snapshots)} 1)")
    ctx.ui.add_system_message("\n".join(lines))

    args = ctx.args.strip()
    if not args:
        return

    parts = args.split()
    try:
        idx = int(parts[0]) - 1
    except (ValueError, IndexError):
        ctx.ui.add_system_message("Invalid checkpoint number.")
        return

    if idx < 0 or idx >= len(snapshots):
        ctx.ui.add_system_message(f"Checkpoint {idx + 1} not found. Valid: 1-{len(snapshots)}")
        return

    option = 1
    if len(parts) > 1:
        try:
            option = int(parts[1])
        except ValueError:
            pass

    snap = snapshots[idx]

    def _invalidate_cache(changed_files: list[str]) -> None:
        """rewind 恢复了磁盘内容：文件缓存不失效的话，agent 会继续
        基于回滚前的旧内容编辑（FileCache 无 TTL / mtime 校验）。"""
        cache = getattr(ctx.agent, "file_cache", None)
        if cache is None:
            return
        if len(changed_files) > 20:
            cache.clear()
            return
        for path in changed_files:
            try:
                from pathlib import Path as _P
                cache.invalidate(str(_P(path).resolve()))
            except Exception:
                cache.clear()
                return

    if option == 1:
        changed = fh.rewind(idx)
        _invalidate_cache(changed)
        ctx.conversation.replace_history(ctx.conversation.history[: snap.message_index])
        ctx.ui.add_system_message(
            f"⟲ Rewound to checkpoint {idx + 1}. Restored {len(changed)} file(s) and conversation."
        )
    elif option == 2:
        ctx.conversation.replace_history(ctx.conversation.history[: snap.message_index])
        ctx.ui.add_system_message(
            f"⟲ Rewound conversation to checkpoint {idx + 1}. Files unchanged."
        )
    elif option == 3:
        changed = fh.rewind(idx)
        _invalidate_cache(changed)
        ctx.ui.add_system_message(
            f"⟲ Restored {len(changed)} file(s) to checkpoint {idx + 1}. Conversation unchanged."
        )
    else:
        ctx.ui.add_system_message("Invalid option. Use 1 (both), 2 (conversation), or 3 (code).")


REWIND_COMMAND = Command(
    name="rewind",
    description="Rewind to a previous checkpoint",
    type=CommandType.LOCAL,
    handler=_handle_rewind,
    usage="/rewind [checkpoint_number] [option]",
)
