"""Describe the system shell used by asyncio subprocess commands."""
import os


def shell_description() -> str:
    if os.name == "nt":
        return (
            "Windows command shell (COMSPEC, normally cmd.exe). "
            "Use cmd syntax, not PowerShell or POSIX Bash syntax."
        )
    return "POSIX /bin/sh. Use POSIX shell syntax; Bash-specific extensions are not guaranteed."
