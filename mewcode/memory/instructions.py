"""Load global policy, root-to-work-directory policy, then local overrides."""
from __future__ import annotations

from pathlib import Path

from mewcode.memory.instruction_includes import (
    INCLUDE_PREFIX,
    MAX_FILE_BYTES,
    MAX_INCLUDE_DEPTH,
    InstructionReader,
)

MAX_DIRECTORY_DEPTH = 64
__all__ = ["INCLUDE_PREFIX", "MAX_INCLUDE_DEPTH", "agent_instructions", "load_instructions", "process_includes"]


def process_includes(content: str, base_dir: Path, project_root: Path, depth: int = 0) -> str:
    reader = InstructionReader()
    data = content.encode('utf-8')
    if len(data) > MAX_FILE_BYTES:
        return '<!-- instructions budget exhausted: file bytes -->'
    reader.remaining -= len(data)
    return reader.expand(content, base_dir, project_root, depth)


def _directory_chain(work: Path) -> tuple[list[Path], str]:
    chain = [work]
    for _ in range(MAX_DIRECTORY_DEPTH):
        current = chain[-1]
        if (current / '.git').exists():  # Includes the .git file used by worktrees.
            return list(reversed(chain)), ''
        if current.parent == current:
            return [work], ''
        chain.append(current.parent)
    return [work], '<!-- instructions directory budget exhausted -->'


def load_instructions(project_root: str) -> str:
    try:
        work = Path(project_root).resolve()
        chain, diagnostic = _directory_chain(work)
        reader = InstructionReader()
        global_root = Path.home() / '.mewcode'
        sources = [(global_root / name, global_root) for name in ('MEWCODE.md', 'AGENTS.md')]
        root = chain[0]
        for directory in chain:
            sources.extend((directory / name, root) for name in ('MEWCODE.md', 'AGENTS.md', '.mewcode/MEWCODE.md'))
        sources.append((work / 'MEWCODE.local.md', root))
        sections = [reader.read(path, scope, optional=True) for path, scope in sources]
        return '\n---\n'.join(s for s in [*sections, diagnostic] if s)
    except (OSError, RuntimeError, ValueError):
        return '<!-- instructions skipped: invalid working directory -->'


def agent_instructions(work_dir: str, role: str = '') -> str:
    """Independent agents load their own scope; fork contexts inherit existing policy."""
    return '\n---\n'.join(section for section in (load_instructions(work_dir), role) if section)
