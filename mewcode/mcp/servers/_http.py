"""两个内置 server 共用的 HTTP 取数小工具。

**只读是硬约束**：整条取数路径只有 GET，没有第二个动词。这不是"约定"，
而是这一层刻意收窄——agent 不可信，那就别给它能写外部系统的工具。
"""

from __future__ import annotations

import os

import httpx

DEFAULT_TIMEOUT = 20.0
#: 回给模型的文本上限：内部系统的响应可能很大，别让它淹掉上下文
MAX_OUTPUT_CHARS = 20_000


class ToolBackendError(Exception):
    """后端未配置或不可达——模型看到明确原因好过看到一个空结果。"""


def http_timeout() -> float:
    raw = os.environ.get("MEWCODE_MCP_HTTP_TIMEOUT", "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


async def get_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    """GET 一个 JSON 端点；网络/HTTP 错误转成可读的 ToolBackendError。"""
    try:
        async with httpx.AsyncClient(timeout=http_timeout(), follow_redirects=True) as client:
            response = await client.get(url, params=params or {}, headers=headers or {})
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as e:
        raise ToolBackendError(
            f"{url} returned HTTP {e.response.status_code}: {e.response.text[:300]}"
        ) from e
    except httpx.HTTPError as e:
        raise ToolBackendError(f"{url} is unreachable: {type(e).__name__}: {e}") from e
    except ValueError as e:  # 非 JSON 响应（网关错误页等）
        raise ToolBackendError(f"{url} returned a non-JSON response: {e}") from e
    if not isinstance(payload, dict):
        raise ToolBackendError(f"{url} returned {type(payload).__name__}, expected a JSON object")
    return payload


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated, {len(text)} chars total)"
