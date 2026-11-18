"""Collect and compare pytest scope from the actual service test invocations."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shlex
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from .jobs import JobEvent, JobStore
from .verification import SourceRevision, VerificationSourceError, git_bytes

BASELINE_EVENT = "pytest_scope_baseline"
SCOPE_EVENT = "pytest_scope"


def supports_pytest(command: str) -> bool:
    # Shell composition and environment prefixes can change the meaning of the
    # injected options. Run those commands as configured without a probe.
    if re.search(r"[|&;<>\r\n`$%]", command):
        return False
    try:
        words = shlex.split(command, posix=False)
    except ValueError:
        return False
    if not words:
        return False
    executable = PureWindowsPath(words[0].strip("\"'")).name.lower().removesuffix(".exe")
    if executable == "uv" and words[1:2] == ["run"]:
        index = 3 if words[2:3] == ["--locked"] else 2
        words = words[index:]
        if not words:
            return False
        executable = PureWindowsPath(words[0].strip("\"'")).name.lower().removesuffix(".exe")
    return executable == "pytest" or (
        re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable) is not None and words[1:3] == ["-m", "pytest"]
    )


@dataclass
class PytestScope:
    reason: str = ""
    exit_code: int = -1
    items: dict[str, str] = field(default_factory=dict)
    reported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    config: str = ""
    settings: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ScopeProbe:
    directory: Path
    module: str

    def host_env(self, env: dict[str, str]) -> dict[str, str]:
        pythonpath = env.get("PYTHONPATH")
        addopts = env.get("PYTEST_ADDOPTS", "")
        return {
            **env,
            "PYTHONPATH": str(self.directory) + (os.pathsep + pythonpath if pythonpath else ""),
            "PYTEST_ADDOPTS": f"{addopts} -p {self.module}",
        }

    def shell_command(self, command: str) -> str:
        return (
            f'PYTHONPATH="./{self.directory.name}${{PYTHONPATH:+:$PYTHONPATH}}" '
            f'PYTEST_ADDOPTS="${{PYTEST_ADDOPTS:+$PYTEST_ADDOPTS }}-p {self.module}" {command}'
        )

    def read(self, output: str, exit_code: int) -> tuple[str, PytestScope]:
        prefix = self.module + " "
        lines = output.splitlines(keepends=True)
        records = [line[len(prefix):] for line in lines if line.startswith(prefix)]
        if not records:
            return output, PytestScope(reason="probe_output_missing")
        output = "".join(line for line in lines if not line.startswith(prefix))
        if len(records) != 1 or len(records[0]) > 610_000:
            return output, PytestScope(reason="invalid_probe_output")
        try:
            scope = PytestScope(**json.loads(records[0]))
            if (
                not isinstance(scope.reason, str) or (not scope.reason and scope.exit_code != exit_code)
                or not isinstance(scope.items, dict) or any(not isinstance(path, str) for path in scope.items.values())
                or not isinstance(scope.config, str) or not isinstance(scope.settings, dict)
                or not isinstance(scope.reported, list) or not isinstance(scope.skipped, list)
                or not set(scope.skipped) <= set(scope.reported) <= scope.items.keys()
            ):
                raise ValueError("invalid pytest receipt")
            return output, scope
        except (TypeError, ValueError):
            return output, PytestScope(reason="invalid_probe_output")


@contextmanager
def pytest_probe(work_dir: str, command: str) -> Iterator[ScopeProbe | None]:
    if not supports_pytest(command):
        yield None
        return
    temporary = None
    try:
        temporary = tempfile.TemporaryDirectory(prefix=".mew-pytest-", dir=work_dir)
        directory = Path(temporary.name).resolve()
        directory.chmod(0o755)
        module = "mew_pytest_" + uuid4().hex
        (directory / f"{module}.py").write_bytes(Path(__file__).with_name("_pytest_scope_probe.py").read_bytes())
    except OSError:
        if temporary is not None:
            temporary.cleanup()
        yield None
        return
    with temporary:
        yield ScopeProbe(directory, module)


@dataclass
class ScopeBaseline:
    command: str
    tree: str
    scope: PytestScope
    recorded: bool = False

    @classmethod
    async def load(cls, store: JobStore, job_id: str, command: str) -> ScopeBaseline | None:
        for event in await store.events(job_id):
            if event.kind == BASELINE_EVENT:
                data = json.loads(event.detail)
                return cls(data["command"], data["tree"], PytestScope(**data["scope"]), recorded=True)
        if not re.search(r"\bpytest(?:\.exe)?\b", command, re.IGNORECASE):
            return None
        return cls(command, "", PytestScope(reason="baseline_unavailable"))

    async def record(self, store: JobStore, job_id: str, scope: PytestScope | None, tree: str) -> None:
        self.tree = tree
        self.scope = scope or PytestScope(reason="unsupported_command_or_runner")
        self.recorded = True
        await store.add_event(job_id, BASELINE_EVENT, json.dumps(asdict(self)))

    async def compare(
        self, work_dir: str, source: SourceRevision, command: str, after: PytestScope | None,
    ) -> ScopeComparison:
        return await asyncio.to_thread(compare_scope, self, work_dir, source, command, after)


@dataclass
class ScopeComparison:
    status: str
    baseline_tree: str
    tree: str
    before_count: int
    after_count: int
    reasons: list[str] = field(default_factory=list)
    changes: dict[str, dict] = field(default_factory=dict)

    def detail(self, attempt: int) -> str:
        return json.dumps({**asdict(self), "attempt": attempt}, ensure_ascii=False)

    def add(self, key: str, items: set[str]) -> None:
        if items:
            self.changes[key] = {"count": len(items), "sample": [item[:300] for item in sorted(items)[:30]]}


def _normalized_tree(source: bytes, additions: set[tuple[str, ...]]) -> str:
    module = ast.parse(source)
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    module.body = [
        stmt for stmt in module.body
        if not (isinstance(stmt, (*functions, ast.ClassDef)) and (stmt.name,) in additions)
    ]
    for stmt in module.body:
        if isinstance(stmt, ast.ClassDef):
            stmt.body = [
                member for member in stmt.body
                if not (isinstance(member, functions) and (stmt.name, member.name) in additions)
            ]
    return ast.dump(module)


def _node_path(nodeid: str) -> tuple[str, ...]:
    return tuple(nodeid.partition("::")[2].partition("[")[0].split("::"))


def _test_edits(
    work_dir: str, before_tree: str, after_tree: str, paths: set[str], before: PytestScope, after: PytestScope,
) -> tuple[set[str], set[str]]:
    edits: set[str] = set()
    unreadable: set[str] = set()
    old_defs: dict[str, set[tuple[str, ...]]] = {}
    new_defs: dict[str, set[tuple[str, ...]]] = {}
    for scope, definitions in ((before, old_defs), (after, new_defs)):
        for nodeid, path in scope.items.items():
            if path in paths:
                definitions.setdefault(path, set()).add(_node_path(nodeid))
    for path, existing in old_defs.items():
        try:
            old = git_bytes(work_dir, "cat-file", "blob", f"{before_tree}:{path}")
            new = git_bytes(work_dir, "cat-file", "blob", f"{after_tree}:{path}")
        except VerificationSourceError:
            unreadable.add(path)
            continue
        if not path.endswith(".py"):
            if old != new:
                edits.add(path)
            continue
        additions = new_defs.get(path, set()) - existing
        old_roots = {node[0] for node in existing}
        additions |= {(node[0],) for node in additions if node[0] not in old_roots}
        try:
            if _normalized_tree(old, set()) != _normalized_tree(new, additions):
                edits.add(path)
        except (SyntaxError, ValueError):
            if old != new:
                edits.add(path)
    return edits, unreadable


def compare_scope(
    baseline: ScopeBaseline, work_dir: str, source: SourceRevision, command: str, after: PytestScope | None,
) -> ScopeComparison:
    before = baseline.scope
    after = after or PytestScope(reason="unsupported_command_or_runner")
    result = ScopeComparison("unavailable", baseline.tree, source.tree, len(before.items), len(after.items))
    if before.reason:
        result.reasons.append(f"baseline:{before.reason}")
    if after.reason:
        result.reasons.append(f"candidate:{after.reason}")
    if command != baseline.command:
        result.reasons.append("command_changed_since_baseline")
    if result.reasons:
        return result

    old_ids, new_ids = set(before.items), set(after.items)
    result.add("removed", old_ids - new_ids)
    result.add("added", new_ids - old_ids)
    result.add("newly_skipped", (set(after.skipped) - set(before.skipped)) & old_ids)
    if after.exit_code == 0:
        result.add("unexecuted", (old_ids & new_ids) - set(after.reported))
    result.add("config", {
        f"{key}: {before.settings.get(key)!r} → {after.settings.get(key)!r}"
        for key in before.settings.keys() | after.settings.keys() if before.settings.get(key) != after.settings.get(key)
    } | ({f"config file: {before.config or '(none)'} → {after.config or '(none)'}"}
         if before.config != after.config else set()))
    try:
        raw = git_bytes(work_dir, "diff", "--name-status", "--no-renames", "-z", baseline.tree, source.tree, "--")
        records = raw.decode("utf-8", errors="replace").split("\0")
        statuses = dict(zip(records[1::2], records[::2], strict=False))
        paths = set(statuses)
        result.add("collection_hooks", {path for path in paths if path.rsplit("/", 1)[-1] == "conftest.py"})
        existing_paths = set(before.items.values())
        removed_paths = {path for path in paths & existing_paths if statuses[path] == "D"}
        result.add("deleted_test_files", removed_paths)
        edits, unreadable = _test_edits(work_dir, baseline.tree, source.tree, paths - removed_paths, before, after)
        result.add("edited_tests", edits)
        result.add("unreadable_test_files", unreadable | ({"outside worktree"} if "" in existing_paths else set()))
    except VerificationSourceError:
        result.reasons.append("git_scope_diff_unavailable")
    attention = result.changes.keys() - {"added", "unreadable_test_files"}
    if attention:
        result.status = "review_required"
    elif not result.reasons and "unreadable_test_files" not in result.changes:
        result.status = "preserved"
    return result


def scope_reports(events: list[JobEvent]) -> list[dict]:
    return [
        {**json.loads(event.detail), "ts": event.ts, "event_seq": event.seq, "execution_id": event.execution_id}
        for event in events if event.kind == SCOPE_EVENT
    ]
