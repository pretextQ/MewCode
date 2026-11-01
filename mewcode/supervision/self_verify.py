"""Run-local Python verification signals, never a completion or coverage gate."""
from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from mewcode.tools.bash import Bash
from mewcode.tools.edit_file import EditFile
from mewcode.tools.write_file import WriteFile

if TYPE_CHECKING:
    from .bus import AdvisoryBus

KEY = "builtin:self-verify"


def verification_kind(command: str) -> str | None:
    """Recognize a small argv grammar; shell composition is deliberately unknown."""
    if len(command) > 8192 or re.search(r"[;&|<>`$%!^#()\r\n]", command):
        return None
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    if not args:
        return None
    if args[:2] == ["uv", "run"]:
        args = args[2:]
    if not args:
        return None
    executable = args.pop(0).replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe")
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable):
        if len(args) < 2 or args[0] != "-m":
            return None
        executable, args = args[1], args[2:]
    informational = {
        "-h", "--help", "-V", "--version", "--collect-only", "--collectonly", "--co",
        "--fixtures", "--funcargs", "--fixtures-per-test", "--markers", "--show-files", "--show-settings",
        "--fix", "--fix-only",
    }
    if any(arg.split("=", 1)[0] in informational for arg in args):
        return None
    if executable in {"pytest", "mypy"}:
        return executable
    if executable == "ruff" and args[:1] == ["check"]:
        return "ruff"
    return None


class SelfVerify:
    def __init__(self, bus: AdvisoryBus) -> None:
        self.bus = bus
        self.reset()

    def reset(self) -> None:
        self.revision = self.attempted_revision = self.successful_revision = 0
        self.generation = 0
        self.suspended = False

    def steer(self) -> None:
        self.generation += 1
        self.suspended = True

    def start(self, tool: object, arguments: dict) -> tuple[int, int, str | None]:
        """Called only after permission/hooks/parameter validation, before execute."""
        kind = verification_kind(arguments.get("command", "")) if type(tool) is Bash else None
        revision = self.revision
        if kind:
            self.attempted_revision = revision
            self.bus._event("verification_attempt", verification_kind=kind, revision=revision,
                            coverage="unknown")
            if revision and not self.suspended:
                self.bus.observe("verification_attempt", "Bash")
        return self.generation, revision, kind

    def finish(self, tool: object, arguments: dict, token: tuple[int, int, str | None],
               success: bool) -> None:
        generation, revision, kind = token
        if generation != self.generation:
            return  # An in-flight pre-steer result cannot restore the old obligation.
        if type(tool) in (EditFile, WriteFile) and success:
            path = arguments.get("file_path", "")
            if path.lower().endswith((".py", ".pyi")):
                self.revision += 1
                self.suspended = False
                self.bus._event("python_edit_observed", revision=self.revision)
        if kind:
            if success:
                self.successful_revision = max(self.successful_revision, revision)
            self.bus._event("verification_result", verification_kind=kind, revision=revision,
                            tool_success=success, coverage="unknown")

    def suggest(self, available: bool) -> None:
        from .bus import Advisory, Expectation

        outstanding = self.revision > self.attempted_revision and not self.suspended
        entry = self.bus.entries.get(KEY)
        if entry and entry.state == "pending" and (not outstanding or not available):
            entry.state = "superseded"
            self.bus._event("self_verify_withdrawn", key=KEY)
        if not available or not outstanding:
            return
        if self.bus.publish(Advisory(
            KEY, category="self_verify", priority=60, ttl=2,
            expect=Expectation("verification_attempt", "Bash", within_turns=2),
            text=(
                "A successful Python file edit was observed without a later recognized "
                "verification attempt for those edits. Within the task's authorized scope, "
                "consider an applicable targeted test, type check or lint check before concluding. "
                "For work with no applicable runnable check, explain that limitation and the "
                "evidence used; do not run unrelated tests. This advice grants no permissions "
                "and adds no requests. Report attempted checks, failures and unverified scope "
                "accurately: command success does not establish sufficient coverage."
            ),
        )):
            self.bus._event("self_verify_suggested", key=KEY, revision=self.revision)

    def report(self) -> dict:
        return {"revision": self.revision, "attempted_revision": self.attempted_revision,
                "successful_revision": self.successful_revision, "suspended": self.suspended,
                "coverage": "unknown"}
