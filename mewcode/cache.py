from __future__ import annotations

import threading
from collections import OrderedDict


class FileCache:
    def __init__(self, max_entries: int = 256, max_chars: int = 4_194_304) -> None:
        self._store: OrderedDict[str, str] = OrderedDict()
        self.max_entries = max(1, max_entries)
        self.max_chars = max(0, max_chars)
        self._chars = 0
        self._lock = threading.Lock()

    def get(self, path: str) -> str | None:
        with self._lock:
            value = self._store.get(path)
            if value is not None:
                self._store.move_to_end(path)
            return value


    def put(self, path: str, content: str) -> None:
        with self._lock:
            old = self._store.pop(path, "")
            self._chars -= len(old)
            if len(content) <= self.max_chars:
                self._store[path] = content
                self._chars += len(content)
            while len(self._store) > self.max_entries or self._chars > self.max_chars:
                _, removed = self._store.popitem(last=False)
                self._chars -= len(removed)


    def invalidate(self, path: str) -> None:
        with self._lock:
            self._chars -= len(self._store.pop(path, ""))


    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._chars = 0


    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
