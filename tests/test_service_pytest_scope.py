from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from mewcode.service.execution import SandboxTestRunner
from mewcode.service.pytest_scope import supports_pytest


@pytest.mark.parametrize(("command", "supported"), [
    ("pytest -q", True),
    ('"C:\\Program Files\\Python\\python.exe" -m pytest tests', True),
    ("uv run --locked python -m pytest", True),
    ("uv run pytest -q", True),
    ("make pytest", False),
    ("pytest -q && pytest -m integration", False),
    ("PYTEST_ADDOPTS=-q pytest", False),
])
def test_pytest_command_recognition(command: str, supported: bool):
    assert supports_pytest(command) is supported


@pytest.mark.asyncio
async def test_sandbox_collects_probe_receipt_without_exposing_it_as_test_output(tmp_path: Path):
    class Sandbox:
        async def available(self):
            return True

        async def run_command(self, job_id, work_dir, command, *, repo_name, timeout):
            assert work_dir == str(tmp_path) and command.endswith("pytest -q")
            module = re.search(r"-p (mew_pytest_\w+)", command)
            assert module is not None
            assert len(list(tmp_path.glob(f".mew-pytest-*/{module[1]}.py"))) == 1
            nodeid = "test_demo.py::test_demo"
            data = {"exit_code": 0, "items": {nodeid: "test_demo.py"}, "reported": [nodeid]}
            return SimpleNamespace(exit_code=0, stdout=f"1 passed\n{module[1]} {json.dumps(data)}\n", timed_out=False)

    result = await SandboxTestRunner(Sandbox()).run(str(tmp_path), "pytest -q", 10)
    assert result.passed and result.command == "pytest -q" and result.output == "1 passed\n"
    assert result.pytest_scope is not None and result.pytest_scope.reported == ["test_demo.py::test_demo"]
    assert not list(tmp_path.glob(".mew-pytest-*"))
