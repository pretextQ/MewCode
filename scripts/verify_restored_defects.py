"""Prove selected existing regressions reject restored unsafe behavior.

Only temporary copies are edited. A pytest plugin records the exact assertion
and node ID; collection errors, skips, timeouts and unrelated failures are invalid.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Defect:
    name: str
    module: str
    anchor: str
    replacement: str
    test: str
    assertion: str


DEFECTS = (
    Defect(
        "N02 shell expansion", "mewcode.permissions.dangerous",
        '"$", "%", "!", "^",', '"$", "!", "^",',
        "tests/test_permission_security.py::test_shell_expansion_and_unc_require_confirmation",
        "assert not is_safe_command(command)",
    ),
    Defect(
        "N04 recursive delete option order", "mewcode.permissions.dangerous",
        "if recursive and force and absolute:", "if False:",
        "tests/test_permission_security.py::test_recursive_delete_option_order",
        "assert DangerousCommandDetector().detect(command)[0]",
    ),
    Defect(
        "N05 compound allow rule", "mewcode.permissions.rules",
        'if (self.effect == "allow" and tool_name == "Bash"',
        'if (False and self.effect == "allow" and tool_name == "Bash"',
        "tests/test_permissions.py::TestDangerousCommandDetector::test_legacy_allow_rule_cannot_authorize_compound_shell",
        'assert not Rule("Bash", "npm test*", "allow").matches("Bash", "npm test" + suffix)',
    ),
    Defect(
        "container credential serialization", "mewcode.service.sandbox",
        '"api_key": "",', '"api_key": provider_config.resolve_api_key(),',
        "tests/test_service_sandbox.py::TestRunAgent::test_container_config_has_no_secret",
        'assert "sk-super-secret" not in text',
    ),
)

# Runs in a fresh interpreter, imports the actual temporary package, and invokes
# the existing regression unmodified. Assertion rewriting remains pytest's job.
RUNNER = '''
import importlib
import json
import sys
from pathlib import Path
import pytest

root = Path.cwd().resolve()
sys.path.insert(0, str(root))
module = importlib.import_module(sys.argv[1])
assert Path(module.__file__).resolve() == root.joinpath(*sys.argv[1].split(".")).with_suffix(".py")

class Evidence:
    def __init__(self):
        self.events = []

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = (yield).get_result()
        event = {"node": item.nodeid, "when": report.when, "outcome": report.outcome}
        if call.excinfo is not None:
            frame = call.excinfo.traceback[-1]
            event.update(exception=call.excinfo.typename,
                         file=Path(str(frame.path)).resolve().relative_to(root).as_posix(),
                         assertion=str(frame.statement).strip())
        self.events.append(event)

evidence = Evidence()
code = pytest.main([
    sys.argv[2], "-q", "--timeout=30", "--import-mode=importlib",
    "-p", "pytest_asyncio.plugin", "-p", "pytest_timeout",
], plugins=[evidence])
(root / "evidence.json").write_text(json.dumps({"code": int(code), "events": evidence.events}), encoding="utf-8")
sys.exit(code)
'''


def valid_evidence(data: dict, defect: Defect, *, restored: bool) -> bool:
    events = data["events"]
    calls = [event for event in events if event["when"] == "call"]
    failures = [event for event in events if event["outcome"] != "passed"]
    if not calls or any(
        event["node"] != defect.test and not event["node"].startswith(defect.test + "[") for event in events
    ):
        return False
    if not restored:
        return data["code"] == 0 and not failures
    return data["code"] == 1 and bool(failures) and all(
        event["when"] == "call" and event["outcome"] == "failed"
        and event.get("exception") == "AssertionError"
        and event.get("file") == defect.test.split("::")[0]
        and event.get("assertion") == defect.assertion
        for event in failures
    )


def verify(defect: Defect) -> None:
    with tempfile.TemporaryDirectory(prefix="mewcode-restored-") as directory:
        root = Path(directory)
        shutil.copytree(ROOT / "mewcode", root / "mewcode", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (root / "tests").mkdir()
        for filename in ("conftest.py", Path(defect.test.split("::")[0]).name):
            shutil.copy2(ROOT / "tests" / filename, root / "tests" / filename)
        shutil.copy2(ROOT / "pyproject.toml", root / "pyproject.toml")
        (root / "runner.py").write_text(RUNNER, encoding="utf-8")
        target = root.joinpath(*defect.module.split(".")).with_suffix(".py")
        original = target.read_text(encoding="utf-8")
        if original.count(defect.anchor) != 1:
            raise RuntimeError(f"{defect.name}: expected exactly one mutation anchor")
        mutated = original.replace(defect.anchor, defect.replacement, 1)
        compile(mutated, str(target), "exec")
        # Isolate home/config and disable bytecode caching across green/red runs.
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        home = root / "home"
        home.mkdir()
        env.update(HOME=str(home), USERPROFILE=str(home))
        for restored in (False, True):
            target.write_text(mutated if restored else original, encoding="utf-8")
            evidence = root / "evidence.json"
            evidence.unlink(missing_ok=True)
            result = subprocess.run(
                [sys.executable, "-B", "runner.py", defect.module, defect.test],
                cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            if not evidence.exists():
                raise RuntimeError(f"{defect.name}: missing evidence\n{result.stdout}\n{result.stderr}")
            data = json.loads(evidence.read_text(encoding="utf-8"))
            if result.returncode != data["code"] or not valid_evidence(data, defect, restored=restored):
                raise RuntimeError(f"{defect.name}: invalid {'RED' if restored else 'GREEN'}\n{data}\n{result.stdout}")
            count = sum(event["when"] == "call" and event["outcome"] == "failed" for event in data["events"])
            print(f"{defect.name}: {'RED' if restored else 'GREEN'} (assertion failures: {count})", flush=True)


def main() -> None:
    for defect in DEFECTS:
        verify(defect)


if __name__ == "__main__":
    main()
