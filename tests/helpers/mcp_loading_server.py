"""Local stdio fixture: no configuration, memory, network backend or model."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

root = Path(sys.argv[1]).resolve()
os.chdir(root)
(root / 'server.pid').write_text(str(os.getpid()))
mcp = FastMCP('offline-loading')


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def query(text: str, delay: float = 0, fail: bool = False) -> str:
    """Query the temporary fixture."""
    with (root / 'calls.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'text': text, 'cwd': str(Path.cwd())}) + '\n')
    await asyncio.sleep(delay)
    if fail:
        raise ValueError('offline failure')
    return text


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
def write(text: str) -> str:
    """A deliberately filtered capability."""
    (root / 'forbidden-write').write_text(text)
    return 'written'


if __name__ == '__main__':
    mcp.run()
