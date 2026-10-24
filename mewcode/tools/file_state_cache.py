
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path


class FileStateCache:
    """Tracks which files have been read, enforcing read-before-edit.

    Stores bounded { absolute_path: mtime_ns } after each ReadFile call.
    EditFile and WriteFile check the cache before proceeding:
      - Gate 1: file must have been read (present in cache).
      - Gate 2: file must not have been modified since the read (mtime_ns matches).
    """

    def __init__(self, max_entries: int = 1024) -> None:
        self._cache: OrderedDict[str, int] = OrderedDict()
        self.max_entries = max(1, max_entries)

    def record(self, path: str, content: str, mtime_ns: int) -> None:
        """Record a file's content and mtime after a successful read."""
        self._cache[path] = mtime_ns
        self._cache.move_to_end(path)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)

    def check(self, path: str) -> tuple[bool, str]:
        """Check whether the file is safe to edit/write.

        Returns (ok, error_message). If ok is True, error_message is empty.
        """
        entry = self._cache.get(path)
        if entry is None:
            return False, "Error: file has not been read yet. Read it first before editing."

        cached_mtime_ns = entry
        self._cache.move_to_end(path)
        try:
            current_mtime_ns = Path(path).stat().st_mtime_ns
        except OSError:
            # File may have been deleted; allow the write to proceed
            # (WriteFile will create it, EditFile will fail on its own).
            return True, ""

        if current_mtime_ns != cached_mtime_ns:
            return False, "Error: file has been modified since last read. Read it again before editing."

        return True, ""

    def update(self, path: str) -> None:
        """Update the cache entry after a successful edit/write."""
        try:
            p = Path(path)
            self.record(path, "", p.stat().st_mtime_ns)
        except OSError:
            # If we can't read it back, just remove the stale entry.
            self._cache.pop(path, None)
