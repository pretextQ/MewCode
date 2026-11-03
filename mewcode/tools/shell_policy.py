"""Conservative lexical query policy, independent of permission auto-approval."""
from __future__ import annotations

import os
import re
from typing import Any

STATUS_OPTIONS = frozenset({'--short', '--porcelain', '--porcelain=v1', '--porcelain=v2', '--branch',
                            '--untracked-files=no', '--untracked-files=normal', '--untracked-files=all',
                            '--no-renames'})


def simple_words(command: object) -> list[str] | None:
    # One grammar for both shells: no quoting, expansion, escapes, globbing,
    # environment assignments, redirection or command composition.
    if not isinstance(command, str) or len(command) > 4096 or not re.fullmatch(r'[A-Za-z0-9_./:= -]+', command):
        return None
    return command.split() or None


def plain_path(value: str) -> bool:
    if not re.fullmatch(r'[A-Za-z0-9_./-]+', value) or value.startswith(('-', '/')):
        return False
    if any(part in {'', '..'} for part in value.split('/')) or value in {'.', './'}:
        return False
    return not any(re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', part.split('.')[0], re.I)
                   for part in value.split('/'))


def git_status_words(command: object) -> list[str] | None:
    words = simple_words(command)
    if (words is not None and words[:2] == ['git', 'status'] and len(words) <= 10
            and len(words[2:]) == len(set(words[2:])) and all(w in STATUS_OPTIONS for w in words[2:])):
        return words
    return None


def concurrent_shell(arguments: dict[str, Any], *, windows: bool | None = None) -> bool:
    if arguments.keys() - {'command', 'timeout'}:
        return False
    timeout = arguments.get('timeout', 120)
    if type(timeout) is not int or not 0 < timeout <= 600:
        return False
    words = simple_words(arguments.get('command'))
    if words is None:
        return False
    if git_status_words(arguments.get('command')) is not None:
        return True  # Execution MUST use the guarded Git argv path.
    windows = os.name == 'nt' if windows is None else windows
    if words == (['cd'] if windows else ['pwd']):
        return True
    return len(words) == 2 and words[0] == ('type' if windows else 'cat') and plain_path(words[1])


def semantic_nonerror(command: str, code: int) -> bool:
    """Only simple, recognized no-match/difference results have a nonzero exception."""
    if code != 1:
        return False
    words = simple_words(command)
    if not words:
        return False
    name = words[0].removesuffix('.exe')
    args = words[1:]
    options = {'-n', '-i', '-F', '--fixed-strings', '--line-number', '--ignore-case'}
    if name in {'rg', 'grep'}:
        while args and args[0] in options:
            args = args[1:]
        return (len(args) == 2 and not args[0].startswith('-') and '=' not in args[0]
                and plain_path(args[1]))
    if name == 'diff':
        if args and args[0] in {'-u', '-q', '--unified', '--brief'}:
            args = args[1:]
        return len(args) == 2 and all(plain_path(a) for a in args)
    return False
