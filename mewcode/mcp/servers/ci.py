"""CI 状态只读查询 MCP server（GitHub Actions / Checks API）。

环境变量：
- ``GITHUB_TOKEN``      只读 token（未配置时公开仓库仍可用，私有仓库会 404/403）
- ``MEWCODE_GITHUB_API`` 可选 API 基址（GitHub Enterprise 自建实例）

服务层的 CI 门禁（publisher.GitHubCIGate）自己会等 checks；这个 server 的
价值在于让 agent 在**动手之前**能看历史：这个分支/提交最近是不是一直红着，
失败的是同一类 job 还是新引入的。只读 GET，不含任何触发/取消工作流的能力。
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ._http import ToolBackendError, get_json, truncate

mcp = FastMCP("mewcode-ci")

MAX_RUNS = 20
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


def _api_base() -> str:
    return os.environ.get("MEWCODE_GITHUB_API", "https://api.github.com").strip().rstrip("/")


def _headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _slug_check(repo: str) -> str:
    parts = repo.strip().split("/")
    if len(parts) != 2 or not all(parts):
        raise ToolBackendError(f"invalid repository {repo!r}; expected 'owner/name'")
    return repo.strip()


@mcp.tool(
    title="List recent workflow runs",
    annotations=_READ_ONLY,
)
async def list_workflow_runs(repo: str, branch: str = "", limit: int = 10) -> str:
    """List recent CI workflow runs for a repository (GitHub Actions).

    Use this to see whether a branch has been failing repeatedly before
    touching it, or to check whether the failure predates the current alert.

    Args:
        repo: repository slug, ``owner/name``.
        branch: restrict to one branch (e.g. ``main``); empty = all branches.
        limit: maximum number of runs (default 10, max 20).
    """
    slug = _slug_check(repo)
    max_runs = max(1, min(int(limit), MAX_RUNS))
    params: dict[str, str] = {"per_page": str(max_runs)}
    if branch.strip():
        params["branch"] = branch.strip()
    payload = await get_json(f"{_api_base()}/repos/{slug}/actions/runs", params=params, headers=_headers())
    runs = payload.get("workflow_runs") or []
    if not runs:
        return f"No workflow runs found for {slug}" + (f" on branch {branch!r}." if branch else ".")
    lines = [
        f"{len(runs)} recent workflow run(s) for {slug}:",
    ]
    for run in runs:
        lines.append(
            "- #{number} {name} | {branch} | {event} | status={status} conclusion={conclusion} | {created} | {url}".format(
                number=run.get("run_number", "?"),
                name=run.get("name", "?"),
                branch=run.get("head_branch", "?"),
                event=run.get("event", "?"),
                status=run.get("status", "?"),
                conclusion=run.get("conclusion") or "-",
                created=(run.get("created_at") or "")[:19],
                url=run.get("html_url", ""),
            )
        )
    return truncate("\n".join(lines))


@mcp.tool(
    title="Get check runs for a commit",
    annotations=_READ_ONLY,
)
async def get_check_runs(repo: str, ref: str) -> str:
    """Get the CI check runs (status + conclusion per job) for one commit or branch head.

    Use this after pushing nothing — i.e. to inspect the *current* state of a
    branch head or a specific commit referenced by the alert.

    Args:
        repo: repository slug, ``owner/name``.
        ref: commit SHA, branch name or tag.
    """
    slug = _slug_check(repo)
    payload = await get_json(
        f"{_api_base()}/repos/{slug}/commits/{ref.strip()}/check-runs",
        params={"per_page": str(MAX_RUNS)},
        headers=_headers(),
    )
    checks = payload.get("check_runs") or []
    if not checks:
        return f"No check runs reported for {slug}@{ref}."
    lines = [f"{len(checks)} check run(s) for {slug}@{ref}:"]
    for check in checks:
        lines.append(
            "- {name} | status={status} conclusion={conclusion} | {url}".format(
                name=check.get("name", "?"),
                status=check.get("status", "?"),
                conclusion=check.get("conclusion") or "-",
                url=check.get("html_url", ""),
            )
        )
    return truncate("\n".join(lines))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
