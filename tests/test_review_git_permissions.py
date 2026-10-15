"""Mixed Git subcommands must not bypass interactive permissions."""
import pytest

from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.permissions.dangerous import is_safe_command
from mewcode.tools.bash import Bash


@pytest.mark.parametrize("command", [
    "git branch -D important-branch", "git branch -d important-branch",
    "git branch new-branch", "git branch -m new-name", "git branch --force existing HEAD",
    "git tag -d release-v1", "git tag release-v2", "git tag -f release-v2",
    "git remote remove origin", "git remote rename origin other",
    "git remote set-url origin relative-repo", "git remote add other relative-repo",
])
def test_mutating_git_commands_require_confirmation(tmp_path, command):
    checker = PermissionChecker(
        DangerousCommandDetector(), PathSandbox(str(tmp_path)), RuleEngine(), PermissionMode.DEFAULT
    )
    assert not is_safe_command(command)
    assert checker.check(Bash(), {"command": command}).effect == "ask"


@pytest.mark.parametrize("command", [
    "git branch", "git branch --list", "git branch -a -vv", "git branch --show-current",
    "git tag", "git tag --list", "git remote", "git remote -v", "git status --short",
])
def test_listing_git_commands_remain_safe(command):
    assert is_safe_command(command)
