"""Command metadata matches the native shell; quoted paths work on both platforms."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mewcode import shell
from mewcode.prompts import environment_section
from mewcode.tools.bash import Bash, Params


@pytest.mark.parametrize("platform_name,expected", [("nt", "cmd.exe"), ("posix", "/bin/sh")])
def test_environment_identifies_native_shell(monkeypatch, platform_name, expected):
    monkeypatch.setattr(shell, "os", SimpleNamespace(name=platform_name))
    assert expected in shell.shell_description()
    assert shell.shell_description() in environment_section("/project").content


def test_bash_schema_identifies_actual_shell():
    schema = Bash().get_schema()
    assert schema["name"] == "Bash"
    assert shell.shell_description() in schema["description"]


@pytest.mark.asyncio
async def test_quoted_interpreter_and_working_directory_with_spaces(tmp_path):
    env = tmp_path / "python env with spaces"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(env)], check=True, timeout=30)
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    work = tmp_path / "work with spaces"
    work.mkdir()
    result = await Bash().bind(str(work)).execute(Params(
        command=f'"{python}" -c "import os; print(os.getcwd())"',
    ))
    assert not result.is_error
    assert str(work) in result.output
