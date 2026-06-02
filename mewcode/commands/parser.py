from __future__ import annotations

from mewcode.commands.registry import CommandRegistry


def parse_command(text: str) -> tuple[str, str, bool]:
    text = text.strip()
    if not text.startswith("/"):
        return "", "", False
    text = text[1:]
    if not text:
        return "", "", True
    parts = text.split(None, 1)
    name = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args, True


def complete(registry: CommandRegistry, prefix: str) -> list[str]:
    prefix = prefix.lstrip("/")
    matches: list[str] = []
    for cmd in registry.list_commands():
        if cmd.name.startswith(prefix):
            matches.append("/" + cmd.name)
        for alias in cmd.aliases:
            if alias.startswith(prefix):
                matches.append("/" + alias)
    matches.sort()
    return matches

