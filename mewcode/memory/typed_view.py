"""Deterministic, owned and rebuildable views of compatible flat memories."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any

from mewcode.atomic import atomic_write_text

OWNER = "mewcode-auto-memory-v1"
MANIFEST = "_mewcode-auto.json"
_LOCK = RLock()
_TYPES = {"用户偏好": "user", "纠正反馈": "feedback", "项目知识": "project", "参考资料": "reference"}
_NAME = re.compile(r"auto-(?:user|feedback|project|reference)-[a-f0-9]{64}(?:-\d+)?\.md")


def digest(text: str | bytes) -> str:
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def entries(content: str) -> dict[str, tuple[str, str]]:
    result = {}
    kind = ""
    for line in content.removeprefix("\ufeff").splitlines():
        if line.startswith("### "):
            kind = next((value for header, value in _TYPES.items() if header in line), "")
        elif kind and line.strip().startswith("- "):
            text = line.strip()[2:].strip()
            if text and text not in {"...", "…", "无", "暂无", "N/A"}:
                result[digest(kind + "\0" + text)] = (kind, text)
    return result


@contextmanager
def memory_transaction(sources: Any) -> Iterator[None]:
    """Thread + OS locks cover the complete read/merge/write/view transaction."""
    streams = []
    with _LOCK:
        try:
            locks = sorted({p.resolve().parent / ".memory-writer.lock" for p in sources})
            for path in locks:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.is_symlink():
                    raise ValueError("Memory writer lock cannot be a symlink")
                stream = path.open("a+b")
                if path.stat().st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                deadline = time.monotonic() + 5
                try:
                    while True:
                        try:
                            stream.seek(0)
                            if sys.platform == "win32":
                                import msvcrt
                                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except OSError as exc:
                            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                                raise
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Memory writer lock timed out") from exc
                            time.sleep(0.01)
                    streams.append(stream)
                except BaseException:
                    stream.close()
                    raise
            yield
        finally:
            for stream in reversed(streams):
                stream.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                stream.close()


def _load(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("Memory manifest cannot be a symlink")
    if not path.exists():
        return {"owner": OWNER, "entries": {}, "retired": {}, "index": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("owner") != OWNER or not isinstance(data.get("entries"), dict):
        raise ValueError("Refusing to overwrite an unowned memory manifest")
    if not isinstance(data.get("retired", {}), dict) or not isinstance(data.get("index", {}), dict):
        raise ValueError("Invalid automatic memory manifest metadata")
    return data


def _owned(path: Path, record: Any) -> bool:
    if not _NAME.fullmatch(path.name) or path.is_symlink() or not isinstance(record, dict):
        return False
    try:
        raw = path.read_bytes()
        return digest(raw) == record["digest"] and f"<!-- {OWNER}:{record['key']} -->".encode() in raw
    except (OSError, KeyError, TypeError):
        return False


def _write_changed(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError("Refusing to overwrite a memory symlink")
    if not path.exists() or path.read_bytes() != content.encode():
        atomic_write_text(path, content)


def _create_or_match(path: Path, content: str) -> None:
    """Publish a complete new inode without replacing a concurrent hand file."""
    if path.exists() or path.is_symlink():
        if not path.is_symlink() and path.read_bytes() == content.encode():
            return
        raise FileExistsError("Memory destination changed during publication; retry the view")
    fd, name = tempfile.mkstemp(prefix=".memory-entry-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic create-only publication on POSIX and NTFS.
    finally:
        for attempt in range(10):
            try:
                temporary.unlink(missing_ok=True)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02)


def _index(text: str, previous: dict[str, Any], records: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    marker = previous.get("marker", "mewcode:auto-memory:v1:0")
    if not isinstance(marker, str) or not re.fullmatch(r"mewcode:auto-memory:v1:\d+", marker):
        marker = "mewcode:auto-memory:v1:0"
    counter = int(marker.rsplit(":", 1)[1])
    while True:
        start, end = f"<!-- {marker}:start -->", f"<!-- {marker}:end -->"
        left, right = text.find(start), text.find(end)
        old_hash = ""
        if left < 0:
            break
        if right > left:
            old_hash = digest(text[left:right + len(end)])
            if marker == previous.get("marker") and old_hash in previous.get("hashes", []):
                break
        counter += 1
        marker = f"mewcode:auto-memory:v1:{counter}"
    pointers = [f"- [{name}]({name}) ({record['type']})" for name, record in sorted(records.items())]
    block = start + "\n## Automatic memory\n" + "\n".join(pointers) + "\n" + end
    updated = (text[:left] + block + text[right + len(end):]) if left >= 0 else (
        text + ("\n" if text and not text.endswith("\n") else "") + block + "\n")
    return updated, {"marker": marker, "hashes": list(dict.fromkeys([h for h in (old_hash, digest(block)) if h]))}


def sync_view(source: Path, allowed_types: set[str]) -> None:
    """Caller holds memory_transaction. Never modify the flat source here."""
    directory = source.parent / "memory"
    if directory.is_symlink():
        raise ValueError("Refusing to write through a memory directory symlink")
    manifest = directory / MANIFEST
    facts = entries(source.read_text(encoding="utf-8")) if source.exists() else {}
    facts = {key: value for key, value in facts.items() if value[0] in allowed_types}
    if not facts and not manifest.exists():
        return
    previous = _load(manifest)
    old_records = {**previous.get("retired", {}), **previous["entries"]}
    records, bodies = {}, {}
    for key, (kind, text) in sorted(facts.items()):
        body = (f"---\nname: {json.dumps('Automatic ' + kind + ' memory')}\n"
                f"description: {json.dumps(text, ensure_ascii=False)}\ntype: {kind}\n---\n"
                f"<!-- {OWNER}:{key} -->\n- {text}\n")
        for suffix in range(1000):
            name = f"auto-{kind}-{key}" + (f"-{suffix}" if suffix else "") + ".md"
            target = directory / name
            if (not target.exists() and not target.is_symlink()
                    or _owned(target, old_records.get(name)) and target.read_bytes() == body.encode()):
                break
        else:
            raise FileExistsError("Too many collisions for an automatic memory file")
        records[name] = {"key": key, "digest": digest(body), "type": kind}
        bodies[name] = body
    retired = {name: record for name, record in old_records.items() if name not in records and _NAME.fullmatch(name)}
    index_path = directory / "MEMORY.md"
    if index_path.is_symlink():
        raise ValueError("Refusing to update an unowned memory index symlink")
    old_index = index_path.read_bytes().decode() if index_path.exists() else ""
    index, index_meta = _index(old_index, previous.get("index", {}), records)
    intent: dict[str, Any] = dict(owner=OWNER, entries=records, retired=retired, index=index_meta)
    # Persist ownership intentions first. Interrupted writes can be rebuilt
    # without mistaking an existing hand-written file for an owned artifact.
    _write_changed(manifest, json.dumps(intent, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    for name, body in bodies.items():
        _create_or_match(directory / name, body)
    for name, record in retired.items():
        path = directory / name
        if _owned(path, record):
            path.unlink()
    if index_path.is_symlink() or (index_path.read_bytes().decode() if index_path.exists() else "") != old_index:
        raise FileExistsError("Memory index changed during publication; retry the view")
    _write_changed(index_path, index)
    intent["retired"] = {}
    intent["index"]["hashes"] = [index_meta["hashes"][-1]]
    _write_changed(manifest, json.dumps(intent, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


@lru_cache(maxsize=64)
def _cached_manifest(path: str, signature: tuple[int, int, int]) -> dict[str, Any]:
    return _load(Path(path))


def owned_key(path: Path) -> str:
    if not _NAME.fullmatch(path.name):
        return ""
    try:
        manifest = path.parent / MANIFEST
        stat = manifest.stat()
        data = _cached_manifest(str(manifest), (stat.st_mtime_ns, stat.st_size, stat.st_ino))
        record = data["entries"].get(path.name)
        retired = record is None
        record = record or data.get("retired", {}).get(path.name)
        return ("retired:" if retired else "") + str(record["key"]) if _owned(path, record) else ""
    except (OSError, ValueError, TypeError, KeyError):
        return ""
