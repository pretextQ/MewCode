"""内部日志平台（Loki）只读查询 MCP server。

环境变量：
- ``MEWCODE_LOKI_URL``   日志平台基址（必填；未配置时工具报"未配置"而不是静默空结果）
- ``MEWCODE_LOKI_TOKEN`` 可选 Bearer token
- ``MEWCODE_LOKI_ORG``   可选多租户 ID（X-Scope-OrgID）

为什么是 Loki：它的查询能力（LogQL）足以覆盖"这个服务最近报了什么错"，
接口是纯 GET，天然适合只读封装。换 Elasticsearch 只需替换本文件的取数层。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ._http import ToolBackendError, get_json, truncate

mcp = FastMCP("mewcode-logs")

#: 单次查询返回给模型的最大日志行数（再多就是噪音，且会吃掉上下文）
MAX_LINES = 200

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


def _base_url() -> str:
    url = os.environ.get("MEWCODE_LOKI_URL", "").strip().rstrip("/")
    if not url:
        raise ToolBackendError(
            "the log platform is not configured in this environment "
            "(MEWCODE_LOKI_URL is unset) — logs cannot be queried"
        )
    return url


def _headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    token = os.environ.get("MEWCODE_LOKI_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    org = os.environ.get("MEWCODE_LOKI_ORG", "").strip()
    if org:
        headers["X-Scope-OrgID"] = org
    return headers


def _format_lines(stream: dict, values: list) -> list[str]:
    labels = ",".join(f"{k}={v}" for k, v in sorted(stream.items())) or "no-labels"
    lines: list[str] = []
    for entry in values:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        ts_ns, text = entry
        try:
            ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=UTC).isoformat(timespec="milliseconds")
        except (TypeError, ValueError):
            ts = str(ts_ns)
        lines.append(f"{ts} [{labels}] {text}")
    return lines


@mcp.tool(
    title="Query application logs",
    annotations=_READ_ONLY,
)
async def query_logs(query: str, minutes: int = 30, limit: int = 50) -> str:
    """Query application logs from the internal log platform (Loki, LogQL syntax).

    Use this when an alert does not carry enough log evidence on its own —
    e.g. to see what else a service logged around the failure, or to find the
    full stack trace behind a truncated alert annotation.

    Args:
        query: LogQL stream selector, e.g. ``{app="checkout"}``. Optional
            filters can be appended, e.g. ``{app="checkout"} |= "ERROR"``.
        minutes: how far back to look, in minutes (default 30).
        limit: maximum number of log lines to return (default 50, max 200).

    Returns log lines as ``timestamp [labels] message``, oldest first.
    """
    base = _base_url()
    window = max(1, min(int(minutes), 24 * 60))
    max_lines = max(1, min(int(limit), MAX_LINES))
    end = datetime.now(tz=UTC)
    start = end - timedelta(minutes=window)

    payload = await get_json(
        f"{base}/loki/api/v1/query_range",
        params={
            "query": query,
            "start": str(int(start.timestamp() * 1e9)),
            "end": str(int(end.timestamp() * 1e9)),
            "limit": str(max_lines),
            "direction": "backward",
        },
        headers=_headers(),
    )
    data = payload.get("data") or {}
    results = data.get("result") or []
    if not results:
        return f"No log lines matched {query!r} in the last {window} minute(s)."

    lines: list[str] = []
    for stream in results:
        if isinstance(stream, dict):
            lines.extend(_format_lines(stream.get("stream") or {}, stream.get("values") or []))
    lines.sort()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    header = f"{len(lines)} line(s) for {query!r} in the last {window} minute(s):"
    return truncate(header + "\n" + "\n".join(lines))


@mcp.tool(
    title="List log labels",
    annotations=_READ_ONLY,
)
async def list_labels(label: str = "") -> str:
    """Discover which labels (services, jobs, environments) exist in the log platform.

    Call this before ``query_logs`` when you do not know the exact label values
    to select a stream with.

    Args:
        label: a label name to list the values of (e.g. ``app``). Leave empty
            to list all available label names.
    """
    base = _base_url()
    if label.strip():
        payload = await get_json(
            f"{base}/loki/api/v1/label/{label.strip()}/values", headers=_headers()
        )
        values = payload.get("data") or []
        return truncate(f"values of label {label!r}: " + ", ".join(str(v) for v in values))
    payload = await get_json(f"{base}/loki/api/v1/labels", headers=_headers())
    names = payload.get("data") or []
    return truncate("available labels: " + ", ".join(str(n) for n in names))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
