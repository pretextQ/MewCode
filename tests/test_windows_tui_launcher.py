"""A failed child must still leave the outer Windows launcher resetting terminal modes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell console launcher")
def test_failed_child_still_emits_terminal_cleanup(tmp_path):
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell unavailable")
    fake_uv = tmp_path / "uv.cmd"
    fake_uv.write_text("@echo off\necho OFFLINE_CHILD_FAILED\nexit /b 7\n", encoding="ascii")
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "run_tui.ps1"
    result = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-File", str(launcher)],
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        capture_output=True, timeout=30,
    )
    assert b"OFFLINE_CHILD_FAILED" in result.stdout, result.stderr
    after_child = result.stdout.split(b"OFFLINE_CHILD_FAILED", 1)[1]
    for mode in (1000, 1003, 1015, 1006, 1004, 2004):
        assert f"\x1b[?{mode}l".encode() in after_child
    assert b"\x1b[<u" in after_child and b"\x1b[?25h" in after_child


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell console launcher")
@pytest.mark.parametrize("shortcut", [False, True])
@pytest.mark.parametrize("explicit_config", [False, True])
def test_launcher_opens_target_directory_with_config(tmp_path, shortcut, explicit_config):
    powershell = shutil.which("powershell")
    if not powershell:
        pytest.skip("Windows PowerShell unavailable")
    project = Path(__file__).resolve().parents[1]
    target = tmp_path / "目标 folder [demo]"
    target.mkdir()
    config = tmp_path / "custom config.yaml"
    config.write_text("{}", encoding="utf-8")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['MEW_TEST_OUTPUT']).write_text("
        "json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:]}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.cmd").write_text(
        '@echo off\n"%MEW_TEST_PYTHON%" "%MEW_TEST_PROBE%" %*\n', encoding="ascii",
    )
    output = tmp_path / "result.json"
    launcher = project / "mew.cmd" if shortcut else project / "scripts" / "run_tui.ps1"
    command = "& $env:MEW_TEST_LAUNCHER $env:MEW_TEST_WORKDIR"
    if explicit_config:
        command += " -Config $env:MEW_TEST_CONFIG"
    result = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-Command", command],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "MEW_TEST_PYTHON": sys.executable,
            "MEW_TEST_PROBE": str(probe),
            "MEW_TEST_OUTPUT": str(output),
            "MEW_TEST_LAUNCHER": str(launcher),
            "MEW_TEST_WORKDIR": str(target),
            "MEW_TEST_CONFIG": config.name,
        },
        capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_text(encoding="utf-8"))
    assert Path(data["cwd"]) == target
    assert data["args"][:4] == ["run", "--project", str(project), "mewcode"]
    if explicit_config:
        assert data["args"][4:] == ["--config", str(config)]
    elif (project / ".mewcode" / "config.yaml").is_file():
        assert data["args"][4:] == ["--config", str(project / ".mewcode" / "config.yaml")]
    else:
        assert data["args"][4:] == []
