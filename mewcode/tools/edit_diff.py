"""Bounded presentation after a successful edit; never part of the write transaction."""
from __future__ import annotations

from collections.abc import Iterator
from difflib import SequenceMatcher

MAX_DIFF_LINES = 200
MAX_DIFF_CHARS = 12000
MAX_INPUT_CHARS = 1_000_000
MAX_INPUT_LINES = 20_000
MAX_LINE_PAIRS = 2_000_000


def edit_summary(path: str, before: str, after: str) -> str:
    safe_path = path[:500]
    if any(ord(c) < 32 for c in safe_path):
        safe_path = safe_path.encode('unicode_escape').decode('ascii')
    heading = f"Successfully edited {safe_path}"
    if len(before) + len(after) > MAX_INPUT_CHARS:
        return heading + "\nDiff statistics omitted: input exceeds comparison budget."
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    if (len(old) + len(new) > MAX_INPUT_LINES or len(old) * len(new) > MAX_LINE_PAIRS):
        return heading + "\nDiff statistics omitted: input exceeds comparison budget."
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    opcodes = matcher.get_opcodes()
    additions = sum(j2 - j1 for tag, _, _, j1, j2 in opcodes if tag in {"insert", "replace"})
    removals = sum(i2 - i1 for tag, i1, i2, _, _ in opcodes if tag in {"delete", "replace"})
    lines = [heading, f"+{additions} additions, -{removals} removals"]
    chars = sum(len(line) + 1 for line in lines)

    def append(line: str) -> bool:
        nonlocal chars
        # Reserve space for the truncation marker, even for a single huge line.
        remaining = MAX_DIFF_CHARS - chars - 40
        if len(lines) >= MAX_DIFF_LINES - 1 or remaining <= 0:
            return False
        lines.append(line[:remaining])
        chars += len(lines[-1]) + 1
        return len(line) <= remaining

    def preview() -> Iterator[str]:
        for group in matcher.get_grouped_opcodes(3):
            old_count, new_count = group[-1][2] - group[0][1], group[-1][4] - group[0][3]
            yield (f"@@ -{group[0][1] + bool(old_count)},{old_count} "
                   f"+{group[0][3] + bool(new_count)},{new_count} @@")
            for tag, i1, i2, j1, j2 in group:
                chunks = ((" ", old[i1:i2]),) if tag == "equal" else (
                    ("-", old[i1:i2] if tag in {"delete", "replace"} else []),
                    ("+", new[j1:j2] if tag in {"insert", "replace"} else []),
                )
                for prefix, chunk in chunks:
                    for line in chunk:
                        yield prefix + line.rstrip("\r\n")
                        if not line.endswith(("\n", "\r")):
                            yield "\\ No newline at end of file"

    for line in preview():
        if not append(line):
            lines.append("… Diff preview truncated.")
            break
    return "\n".join(lines)
