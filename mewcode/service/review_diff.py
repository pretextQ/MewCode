"""Pinned Git evidence and byte-budgeted patch selection, with no worktree reads."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

from .review_models import Coverage

GIT_TIMEOUT = 60
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def git(work_dir: str, args: list[str], output: Path | None = None) -> bytes:
    """No shell, external diff, textconv, prompts or lossy filename quoting."""
    command = ["git", "-c", "core.quotePath=false", *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
    if output is None:
        result = subprocess.run(
            command, cwd=work_dir, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=GIT_TIMEOUT, check=True,
        )
        return result.stdout
    with output.open("wb") as stream:
        subprocess.run(
            command, cwd=work_dir, env=env, stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.PIPE, timeout=GIT_TIMEOUT, check=True,
        )
    return b""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def changed_files(raw: bytes) -> list[Coverage]:
    parts = iter(raw.decode("utf-8", errors="strict").rstrip("\0").split("\0"))
    files = []
    for change in parts:
        if not change:
            continue
        first = next(parts)
        old, path = (first, next(parts)) if change.startswith(("R", "C")) else ("", first)
        files.append(Coverage(path=path, old_path=old, change=change))
    return files


def snapshot(work_dir: str, base_ref: str, head: str, directory: Path) -> tuple[str, list[Coverage]]:
    resolved = git(work_dir, ["rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}"])
    base = git(work_dir, ["merge-base", resolved.decode().strip(), head]).decode().strip()
    git(work_dir, [
        "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", "--find-renames",
        base, head, "--",
    ], directory / "full.diff")
    names = git(work_dir, ["diff", "--name-status", "-z", "--find-renames", base, head, "--"])
    (directory / "files.z").write_bytes(names)
    return base, changed_files(names)


def select_patches(
    work_dir: str, base: str, head: str, files: list[Coverage], directory: Path, budget: int,
) -> list[dict[str, str]]:
    selected = []
    for file in files:
        if budget <= 0:
            file.reason = "input_budget"
            continue
        scratch = directory / "selection.tmp"
        paths = list(dict.fromkeys([file.old_path, file.path]))
        git(work_dir, [
            "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--find-renames",
            "--unified=3", base, head, "--", *[path for path in paths if path],
        ], scratch)
        file.diff_bytes = scratch.stat().st_size
        with scratch.open("rb") as stream:
            raw = stream.read(budget + 1)
        if any(line.startswith((b"Binary files ", b"GIT binary patch")) for line in raw.splitlines()):
            file.reason = "binary"
            continue
        if raw.count(b"diff --git ") > 1:
            file.reason = "complex_rename"
            continue
        partial = len(raw) > budget
        raw = raw[:budget]
        if partial:
            raw = raw.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in raw else b""
        try:
            patch = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            file.reason = "non_utf8"
            continue
        if not patch or (partial and not any(HUNK.match(line) for line in patch.splitlines())):
            file.reason = "input_budget"
            continue
        file.status = "partial" if partial else "full"
        file.reason = "input_budget" if partial else "sent"
        file.sent_bytes = len(raw)
        budget -= len(raw)
        selected.append({"path": file.path, "old_path": file.old_path, "patch": patch})
    (directory / "selection.tmp").unlink(missing_ok=True)
    return selected


def included_lines(patches: list[dict[str, str]]) -> set[tuple[str, str, int]]:
    """Only changed lines actually included in the request can anchor findings."""
    locations: set[tuple[str, str, int]] = set()
    for patch in patches:
        old = new = 0
        in_hunk = False
        for line in patch["patch"].splitlines():
            match = HUNK.match(line)
            if match:
                old, new = int(match[1]), int(match[3])
                in_hunk = True
            elif in_hunk and line.startswith("+"):
                locations.add((patch["path"], "new", new))
                new += 1
            elif in_hunk and line.startswith("-"):
                locations.add((patch["old_path"] or patch["path"], "old", old))
                old += 1
            elif in_hunk and line.startswith(" "):
                old += 1
                new += 1
    return locations
