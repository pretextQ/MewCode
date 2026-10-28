"""Small, dependency-free source guards; also callable from pytest."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def imports(source: str, filename: str) -> list[tuple[int, str]]:
    """Resolve static imports, including package __init__ and relative forms."""
    path = PurePosixPath(filename.replace("\\", "/"))
    package = list(path.parent.parts)
    result = []
    for node in ast.walk(ast.parse(source, filename=path.as_posix())):
        if isinstance(node, ast.Import):
            result.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[:len(package) - node.level + 1] if node.level else []
            if node.module:
                base.extend(node.module.split("."))
            module = ".".join(base)
            result.append((node.lineno, module))
            result.extend((node.lineno, f"{module}.{alias.name}") for alias in node.names)
    return result


def architecture_errors(root: Path) -> list[str]:
    errors = []
    for path in sorted((root / "mewcode").rglob("*.py")):
        name = path.relative_to(root).as_posix()
        forbidden: tuple[str, ...] = ()
        if name.startswith("mewcode/service/"):
            forbidden = (
                "textual", "mewcode.app", "mewcode.driver", "mewcode.askuser_dialog",
                "mewcode.permission_dialog", "mewcode.plan_dialog", "mewcode.teammate_tree",
                "mewcode.commands.completion",
            )
        elif name.startswith("mewcode/permissions/"):
            forbidden = ("mewcode.service",)
        elif name.startswith("mewcode/tools/impl/"):
            forbidden = ("mewcode.agent",)
        if not forbidden:
            continue
        try:
            dependencies = imports(path.read_text(encoding="utf-8"), name)
        except SyntaxError as exc:
            errors.append(f"{name}:{exc.lineno}: {exc.msg}")
            continue
        for line, dependency in dependencies:
            if any(dependency == prefix or dependency.startswith(prefix + ".") for prefix in forbidden):
                errors.append(f"{name}:{line}: forbidden dependency {dependency}")
    return errors


def source_budgets(root: Path) -> tuple[list[str], list[str]]:
    manifest = json.loads((root / "scripts/source_budgets.json").read_text(encoding="utf-8"))
    ceilings = manifest["ceilings"]
    errors = []
    usage = []
    found = set()
    for path in sorted((root / "mewcode").rglob("*.py")):
        name = path.relative_to(root).as_posix()
        found.add(name)
        lines = len(path.read_text(encoding="utf-8").splitlines())
        limit = ceilings.get(name, manifest["default_ceiling"])
        kind = "named" if name in ceilings else "default"
        usage.append(f"{name}: {lines}/{limit} ({kind}, remaining {limit - lines})")
        if lines > limit:
            errors.append(f"{name}: {lines} lines exceeds {kind} ceiling {limit}")
    for name in sorted(ceilings.keys() - found):
        errors.append(f"{name}: stale or non-POSIX budget entry")
    return errors, usage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show all source budget usage")
    args = parser.parse_args()
    errors, usage = source_budgets(ROOT)
    errors.extend(architecture_errors(ROOT))
    if args.list:
        print("\n".join(usage))
    print("\n".join(errors) if errors else "Engineering guards passed")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
