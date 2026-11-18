"""Best-effort diagnostics from already recognized pytest failure blocks."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_CASES = 3
_MAX_SOURCE_BYTES = 65_536
_CAPTURE = re.compile(r"^-{3,} Captured [^\n]+ -{3,}$", re.MULTILINE)
_LOCATION = re.compile(
    r'^(?:(?P<path>(?![E>]\s)\S[^\r\n]*?\.py):(?P<line>[1-9][0-9]{0,6}):[^\r\n]*'
    r'|\s*File "(?P<file>[^"\r\n]+\.py)", line (?P<number>[1-9][0-9]{0,6})(?:,[^\r\n]*)?)$',
    re.MULTILINE,
)
_SOURCE_ASSERTION = re.compile(r"^>[ \t]+(assert[ \t]+[^\r\n]+)$", re.MULTILINE)
_EVALUATED_ASSERTION = re.compile(r"^E[ \t]+(assert[ \t]+[^\r\n]+)$", re.MULTILINE)


@dataclass(frozen=True)
class Location:
    path: str
    line: int


@dataclass(frozen=True)
class Diagnosis:
    node_id: str
    locations: tuple[Location, ...] = ()
    assertion: str = ""
    actual: str = ""
    expected: str = ""
    inferred_paths: tuple[str, ...] = ()


def _excerpt(value: str, limit: int) -> str:
    value = " ".join(value.splitlines())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _relative(value: str, work_dir: str) -> str:
    value = value.replace("\\", "/")
    prefix = work_dir.replace("\\", "/").rstrip("/") + "/"
    if value.startswith(prefix) and prefix != "/":
        value = value[len(prefix):]
    path = PurePosixPath(value)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", value) or ".." in path.parts:
        return ""
    if any(part in {".venv", "site-packages", "dist-packages", "_pytest"} for part in path.parts):
        return ""
    return path.as_posix()


def _locations(block: str, work_dir: str) -> tuple[Location, ...]:
    locations: dict[Location, None] = {}
    for match in _LOCATION.finditer(block):
        path = _relative(match["path"] or match["file"], work_dir)
        if path:
            loc = Location(path, int(match["line"] or match["number"]))
            locations[loc] = None
    return tuple(locations)


def _inside(root: Path, path: str) -> Path | None:
    try:
        resolved = (root / path).resolve()
        return resolved if resolved.is_relative_to(root) and resolved.is_file() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _read_test(root: Path, path: str) -> ast.Module | None:
    source = _inside(root, path)
    if source is None or source.suffix != ".py":
        return None
    try:
        with source.open("rb") as file:
            data = file.read(_MAX_SOURCE_BYTES + 1)
        return ast.parse(data) if len(data) <= _MAX_SOURCE_BYTES else None
    except (OSError, SyntaxError, ValueError, RecursionError):
        return None


def _assertion_in(block: str, pattern: re.Pattern[str]) -> str:
    # Source and evaluated assertions are read from their own traceback lines.
    matches = pattern.findall(block)
    return matches[0].strip() if len(matches) == 1 else ""


def _parse_assertion(text: str) -> ast.Assert | None:
    if not text or len(text) > 2000:
        return None
    try:
        nodes = ast.parse(text).body
        return nodes[0] if len(nodes) == 1 and isinstance(nodes[0], ast.Assert) else None
    except (SyntaxError, ValueError, RecursionError):
        return None


def _operands(test: ast.Assert | None) -> tuple[ast.expr, ast.expr] | None:
    if test is not None and isinstance(test.test, ast.Compare):
        comparison = test.test
        if len(comparison.ops) == 1 and isinstance(comparison.ops[0], ast.Eq):
            return comparison.left, comparison.comparators[0]
    return None


def _literal(node: ast.expr) -> bool:
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError, RecursionError):
        return False


def _values(expression: str, assertion: ast.Assert | None) -> tuple[str, str]:
    source = _operands(assertion)
    evaluated = _operands(_parse_assertion(expression))
    if source is None or evaluated is None or not all(_literal(node) for node in evaluated):
        return "", ""
    lhs, rhs = source
    # Label the sides only when the source supplies an orientation signal.
    left_expected = (isinstance(lhs, ast.Name) and lhs.id.lower() == "expected") or _literal(lhs)
    right_expected = (isinstance(rhs, ast.Name) and rhs.id.lower() == "expected") or _literal(rhs)
    if left_expected == right_expected:
        return "", ""
    actual, expected = (evaluated[1], evaluated[0]) if left_expected else evaluated
    return (
        _excerpt(ast.get_source_segment(expression, actual) or "", 120),
        _excerpt(ast.get_source_segment(expression, expected) or "", 120),
    )


def _import_paths(root: Path, test: ast.Module, assertion: ast.Assert, test_path: str) -> tuple[str, ...]:
    used = {node.id for node in ast.walk(assertion.test) if isinstance(node, ast.Name)}
    modules: list[str] = []
    for statement in test.body:
        if isinstance(statement, ast.Import):
            modules.extend(
                alias.name for alias in statement.names if (alias.asname or alias.name.split(".")[0]) in used
            )
        if isinstance(statement, ast.ImportFrom) and statement.module and statement.level == 0:
            for alias in statement.names:
                if (alias.asname or alias.name) in used:
                    modules.extend([f"{statement.module}.{alias.name}", statement.module])
    found = []
    for module in list(dict.fromkeys(modules))[:16]:
        stem = module.replace(".", "/")
        for prefix in ("", "src/"):
            for path in (f"{prefix}{stem}.py", f"{prefix}{stem}/__init__.py"):
                if len(path) <= 180 and path != test_path and _inside(root, path) is not None:
                    found.append(path)
                    if len(found) == 2:
                        return tuple(found)
    return tuple(found)


def _title(node_id: str) -> str:
    name, bracket, params = node_id.partition("::")[2].partition("[")
    return name.replace("::", ".") + bracket + params


def diagnose(nodes: tuple[str, ...], blocks: tuple[str, ...], work_dir: str) -> list[Diagnosis]:
    titles = {_title(node) for node in nodes[:MAX_CASES]}
    indexed: dict[str, list[tuple[str, tuple[Location, ...]]]] = {}
    for block in blocks:
        title, _, details = block.partition("\n")
        if title.strip() in titles:
            body = _CAPTURE.split(details, maxsplit=1)[0]
            indexed.setdefault(title.strip(), []).append((body, _locations(body, work_dir)))

    diagnoses = []
    root = Path(work_dir).resolve()
    for node in nodes[:MAX_CASES]:
        path = _relative(node.partition("::")[0], work_dir)
        matches = indexed.get(_title(node), [])
        if len(matches) > 1:
            matches = [(body, locs) for body, locs in matches if any(loc.path == path for loc in locs)]
        # An ambiguous block still has a confirmed node ID from the summary.
        if len(matches) != 1:
            diagnoses.append(Diagnosis(_excerpt(node, 200)))
            continue
        body, locations = matches[0]
        test = _read_test(root, path)
        source = _parse_assertion(_assertion_in(body, _SOURCE_ASSERTION))
        if source is None and test is not None:
            lines = {loc.line for loc in locations if loc.path == path}
            assertions = [item for item in ast.walk(test) if isinstance(item, ast.Assert) and item.lineno in lines]
            source = assertions[0] if len(assertions) == 1 else None
        expression = _assertion_in(body, _EVALUATED_ASSERTION)
        actual, expected = _values(expression, source)
        candidates = _import_paths(root, test, source, path) if test is not None and source is not None else ()
        diagnoses.append(Diagnosis(
            _excerpt(node, 200), tuple(Location(_excerpt(loc.path, 180), loc.line) for loc in locations[-2:]),
            _excerpt(expression, 240), actual, expected, candidates,
        ))
    return diagnoses


def render_diagnoses(diagnoses: list[Diagnosis], failure_count: int) -> str:
    lines = ["Observed pytest failures:"]
    for case in diagnoses:
        lines.append(f"- {case.node_id}")
        if case.locations:
            lines.append("  Observed traceback: " + ", ".join(f"{loc.path}:{loc.line}" for loc in case.locations))
        if case.actual and case.expected:
            lines.append(f"  Actual: {case.actual}; expected: {case.expected}")
        elif case.assertion:
            lines.append(f"  Evaluated assertion: {case.assertion}")
        if case.inferred_paths:
            lines.append("  Inferred candidate source (imports used in assertion): " + ", ".join(case.inferred_paths))
    if failure_count > len(diagnoses):
        lines.append(f"… {failure_count - len(diagnoses)} more failures in the original output")
    rendered = "\n".join(lines)
    return rendered if len(rendered) <= 1500 else rendered[:1470].rsplit("\n", 1)[0] + "\n… diagnostic output shortened"
