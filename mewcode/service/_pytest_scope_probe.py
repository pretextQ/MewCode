"""Standalone plugin copied into the worktree for one pytest invocation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_MAX_ITEMS = 10_000
_INI_KEYS = (
    "addopts", "testpaths", "python_files", "python_classes", "python_functions", "norecursedirs",
    "markers", "xfail_strict", "strict_xfail", "required_plugins", "filterwarnings",
)
_items: dict[str, str] | None = None
_reported: set[str] = set()
_skipped: set[str] = set()
_collection_failed = False


def _relative(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return ""


def pytest_collection_finish(session) -> None:
    global _items
    if len(session.items) <= _MAX_ITEMS:
        _items = {item.nodeid: _relative(item.path) for item in session.items}


def pytest_collectreport(report) -> None:
    global _collection_failed
    _collection_failed |= report.failed


def pytest_runtest_logreport(report) -> None:
    if _items is not None and report.nodeid in _items:
        if report.when == "call" or report.skipped:
            _reported.add(report.nodeid)
        if report.skipped:
            _skipped.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    config = session.config
    reason = ""
    if getattr(config.option, "numprocesses", 0):
        reason = "distributed_collection"
    elif _collection_failed or _items is None or exitstatus not in (0, 1, 5):
        reason = "incomplete_collection"
    settings = {}
    for key in sorted(set(_INI_KEYS) | config.inicfg.keys()):
        try:
            settings[key] = config.getini(key)
        except ValueError:
            if key in config.inicfg:
                settings[key] = config.inicfg[key]
    payload = json.dumps({
        "reason": reason, "exit_code": int(exitstatus), "items": _items or {},
        "reported": sorted(_reported), "skipped": sorted(_skipped),
        "config": _relative(config.inipath) if config.inipath else "",
        "settings": settings,
    }, ensure_ascii=True)
    if len(payload) > 600_000:
        payload = '{"reason":"collection_limit"}'
    # The per-invocation module name identifies this record in mixed shell output.
    if sys.__stdout__ is not None:
        print(f"\n{__name__} {payload}", file=sys.__stdout__, flush=True)
