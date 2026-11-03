"""Pure filter-name validation shared by terminal previews and controlled Shell queries."""
from __future__ import annotations

import re

FILTER_QUERY = ['config', '--null', '--name-only', '--get-regexp', r'^filter\..*\.(clean|process|required)$']


def filter_overrides(data: bytes, code: int, truncated: bool = False) -> list[str]:
    if truncated or len(data) > 16000 or code not in (0, 1):
        raise OSError('Unable to safely inspect Git filter configuration')
    try:
        keys = data.decode('utf-8').split('\0')
    except UnicodeError as exc:
        raise OSError('Unsupported Git filter configuration encoding') from exc
    names = set()
    for key in keys:
        if not key:
            continue
        match = re.fullmatch(r'filter\.([A-Za-z0-9_.-]+)\.(clean|process|required)', key)
        if match is None:
            raise OSError('Unsupported Git filter name')
        names.add(match[1])
    overrides = []
    for name in sorted(names):
        overrides.extend(['-c', f'filter.{name}.clean=', '-c', f'filter.{name}.process=',
                          '-c', f'filter.{name}.required=false'])
    if len(names) > 32 or sum(map(len, overrides)) > 12000:
        raise OSError('Git filter configuration exceeds the query budget')
    return overrides
