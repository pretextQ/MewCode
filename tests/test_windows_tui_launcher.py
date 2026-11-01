"""A failed child must still leave the outer Windows launcher resetting terminal modes."""
from __future__ import annotations

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
