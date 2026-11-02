"""Retain bounded process log families without removing another live process."""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path
from typing import BinaryIO

KEEP_SESSIONS = 20
MAX_FAULT_BYTES = 1024 * 1024


def lock_lease(stream: BinaryIO) -> None:
    stream.seek(0)
    if sys.platform == 'win32':
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def prune(root: Path) -> None:
    try:
        real_root = root.resolve()
        directories = sorted((p for p in root.iterdir() if re.fullmatch(r'\d+-[0-9a-f]{32}', p.name)),
                             key=lambda p: p.stat().st_mtime, reverse=True)
        for path in directories[KEEP_SESSIONS:]:
            try:
                # A junction/symlink must never redirect recursive removal.
                if path.resolve() != real_root / path.name or not path.is_dir():
                    continue
                with (path / '.lease').open('r+b') as lease:
                    lock_lease(lease)
                shutil.rmtree(path)
            except OSError:
                continue  # Live lease or unavailable storage: retain rather than disrupt.
    except OSError:
        pass


def trim_fault(path: Path) -> None:
    try:
        with path.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size <= MAX_FAULT_BYTES:
                return
            stream.seek(-MAX_FAULT_BYTES, os.SEEK_END)
            tail = stream.read(MAX_FAULT_BYTES)
        path.write_bytes(b'[earlier trace truncated]\n' + tail)
    except OSError:
        pass
