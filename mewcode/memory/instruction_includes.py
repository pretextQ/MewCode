"""Bounded instruction includes; no access outside the caller's scope."""
from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath

MAX_INCLUDE_DEPTH = 5
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
MAX_FILES = 128
INCLUDE_PREFIX = '@include '


class InstructionReader:
    def __init__(self) -> None:
        self.seen: set[Path] = set()
        self.remaining = MAX_TOTAL_BYTES

    def read(self, path: Path, scope: Path, depth: int = 0, *, optional: bool = False) -> str:
        try:
            real = path.resolve()
            if not real.is_relative_to(scope.resolve()):
                return '<!-- @include blocked: path outside project -->'
            if real in self.seen:
                return '<!-- @include skipped: already loaded (cycle/duplicate) -->'
            if optional and not real.exists():
                return ''
            if depth > MAX_INCLUDE_DEPTH or len(self.seen) >= MAX_FILES or self.remaining <= 0:
                return '<!-- instructions budget exhausted -->'
            self.seen.add(real)
            limit = min(MAX_FILE_BYTES, self.remaining)
            with real.open('rb') as stream:
                data = stream.read(limit + 1)
            self.remaining -= min(len(data), limit)
            if len(data) > limit:
                return '<!-- instructions budget exhausted: file/total bytes -->'
            content = data.decode('utf-8-sig')
            body = self.expand(content, real.parent, scope, depth)
            source = str(real).replace('\n', ' ').replace('\r', ' ')[:1024]
            return f'<!-- instructions source: {source} -->\n{body}'
        except FileNotFoundError:
            return '<!-- @include skipped: file not found -->'
        except (OSError, UnicodeError, ValueError, RuntimeError):
            return '<!-- instructions skipped: unreadable/invalid file -->'

    def expand(self, content: str, base: Path, scope: Path, depth: int = 0) -> str:
        output: list[str] = []
        fence = ''
        fence_size = 0
        for line in content.split('\n'):
            stripped = line.strip()
            marker = re.match(r'^(`{3,}|~{3,})(.*)$', stripped)
            if marker:
                token, tail = marker.groups()
                if not fence:
                    fence, fence_size = token[0], len(token)
                elif token[0] == fence and len(token) >= fence_size and not tail.strip():
                    fence = ''
                output.append(line)
                continue
            if fence or not stripped.startswith(INCLUDE_PREFIX):
                output.append(line)
                continue
            target = stripped[len(INCLUDE_PREFIX):].strip()
            if Path(target).is_absolute() or PureWindowsPath(target).drive or target.startswith('~'):
                output.append('<!-- @include blocked: absolute path -->')
            elif depth >= MAX_INCLUDE_DEPTH:
                output.append('<!-- instructions budget exhausted: include depth -->')
            else:
                output.append(self.read(base / target, scope, depth + 1))
        return '\n'.join(output)
