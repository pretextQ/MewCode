"""Source identity and audit records for local verification."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jobs import JobEvent

IGNORED_DIRS = (".mewcode", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
COMMIT_EXCLUDES = tuple(
    # Excluding only contents lets git add still match and reject the ignored directory itself.
    spec for name in IGNORED_DIRS
    for spec in (f":(exclude,glob)**/{name}", f":(exclude,glob)**/{name}/**")
)
REVISION_EVENT = "verification_revision"
MAX_DIFF_CHARS = 60_000


class VerificationSourceError(Exception):
    """The publishable source could not be identified or has changed."""


def is_ignored_path(path: str) -> bool:
    return any(part in IGNORED_DIRS for part in path.replace("\\", "/").split("/"))


def git_bytes(work_dir: str, *args: str, index: Path | None = None) -> bytes:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        result = subprocess.run(
            ["git", *args], cwd=work_dir, capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationSourceError(f"git {args[0]} could not identify verification source") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise VerificationSourceError(f"git {args[0]} failed: {detail}")
    return result.stdout


def _git(work_dir: str, *args: str, index: Path | None = None) -> str:
    return git_bytes(work_dir, *args, index=index).decode("utf-8", errors="replace").rstrip("\n\r")


def collect_diff(work_dir: str) -> tuple[list[str], str]:
    status = _git(work_dir, "status", "--porcelain", "-z", "--no-renames", "--untracked-files=all")
    changed = [entry[3:] for entry in status.split("\0") if entry and not is_ignored_path(entry[3:])]
    if not changed:
        return [], ""
    # Include untracked files in the report without staging their contents.
    _git(work_dir, "add", "-N", "--", ".", *COMMIT_EXCLUDES)
    diff = _git(work_dir, "diff", "HEAD", "--no-color", "--", ".", *COMMIT_EXCLUDES)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n… (diff truncated)"
    return changed, diff


@dataclass(frozen=True)
class SourceRevision:
    base_sha: str
    tree: str

    @classmethod
    async def capture(cls, work_dir: str) -> SourceRevision:
        return await asyncio.to_thread(cls._capture, work_dir)

    @classmethod
    def _capture(cls, work_dir: str) -> SourceRevision:
        head = _git(work_dir, "rev-parse", "HEAD")
        # Start with the staged content (including force-added files) and overlay
        # current files using the same pathspecs as commit_all. Leave the real index alone.
        staged = _git(work_dir, "write-tree")
        with tempfile.TemporaryDirectory(prefix="mew-verify-") as directory:
            index = Path(directory) / "index"
            _git(work_dir, "read-tree", staged, index=index)
            _git(work_dir, "add", "-A", "--", ".", *COMMIT_EXCLUDES, index=index)
            tree = _git(work_dir, "write-tree", index=index)
        if _git(work_dir, "rev-parse", "HEAD") != head:
            raise VerificationSourceError("HEAD changed while capturing verification source")
        return cls(head, tree)

    async def check_committed(self, work_dir: str, sha: str) -> None:
        output = await asyncio.to_thread(_git, work_dir, "rev-parse", f"{sha}^", f"{sha}^{{tree}}")
        if output.splitlines() != [self.base_sha, self.tree]:
            raise VerificationSourceError("committed source differs from the locally verified source")


def revision_detail(
    source: SourceRevision | None, attempt: int, status: str, *, unit: str, integration: str,
    head_sha: str = "", reason: str = "", scope_status: str = "",
) -> str:
    return json.dumps({
        "attempt": attempt, "status": status, "base_sha": source.base_sha if source else "",
        "tree": source.tree if source else "", "head_sha": head_sha, "reason": reason,
        "unit": unit, "integration": integration, "scope_status": scope_status,
    })


def revision_reports(events: list[JobEvent]) -> list[dict]:
    return [
        {**json.loads(event.detail), "ts": event.ts, "event_seq": event.seq, "execution_id": event.execution_id}
        for event in events if event.kind == REVISION_EVENT
    ]
