"""Write-only protection for application policy and executable skill definitions."""
from __future__ import annotations

import os
from pathlib import Path

_FILES = ("config.yaml", "permissions.yaml", "permissions.local.yaml")


def _lexical(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if os.name == "nt":
        # Win32 strips trailing dots/spaces and supports alternate data streams.
        path = Path(path.anchor, *(p.split(":", 1)[0].rstrip(" .") for p in path.parts[1:]))
    return path


def check_protected_write(path: str, project_root: Path) -> tuple[bool, str]:
    try:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = project_root / target
        candidates = (_lexical(target), _lexical(target.resolve()))
        for root in (project_root / ".mewcode", Path.home() / ".mewcode"):
            for name in (*_FILES, "skills"):
                protected = root / name
                for identity in (_lexical(protected), _lexical(protected.resolve())):
                    if any(p == identity or identity in p.parents for p in candidates):
                        return False, "protected configuration or skill definition"
    except (OSError, RuntimeError, ValueError):
        return False, "cannot safely resolve protected write path"
    return True, ""
