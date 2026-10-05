"""代码托管集成（W4）：建分支、提交、推送、开 PR、轮询 CI。

选型说明：计划文档首选 `gh` CLI，但**本机没有 gh**，因此按文档预案走
"VCS 抽象接口 + GitHub REST API"。本地动作（branch/commit/push）仍走 git
二进制（跨平台且免依赖），远端动作（PR、check-runs）走 REST。

安全红线（计划 W4.4）：
- PR 的 base 分支永远取自配置，不接受调用方传入；
- 本模块**没有** merge 能力——合并是人审环节，代码里根本不存在这条路径；
- token 不进 argv（GIT_ASKPASS 注入）、不进日志/事件（输出统一 redact）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mewcode.config import VCSConfig

log = logging.getLogger(__name__)

GIT_TIMEOUT = 120
_SLUG_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


class VCSError(Exception):
    """VCS 层的基类异常。"""


class VCSAuthError(VCSError):
    """拿不到凭证，或凭证被服务端拒绝。"""


@dataclass
class PRInfo:
    number: int
    url: str
    head_branch: str
    base_branch: str


@dataclass
class CheckStatus:
    """CI 检查的聚合状态。``none`` = 该提交没有任何检查（仓库未配 CI）。"""

    state: str  # success | failure | pending | none
    details: str = ""

    @property
    def is_failure(self) -> bool:
        return self.state == "failure"


class VCSProvider(Protocol):
    """远端动作的抽象接口（GitLab 等实现可平行加入）。

    注意这里**没有**合并相关方法：合并永远是人做的事（W4 安全红线）。
    """

    async def resolve_repo_slug(self, work_dir: str) -> str: ...

    async def ensure_branch(self, work_dir: str, branch: str) -> None: ...

    async def commit_all(self, work_dir: str, message: str, exclude: tuple[str, ...] = ()) -> bool: ...

    async def push(self, work_dir: str, branch: str) -> None: ...

    async def head_sha(self, work_dir: str) -> str: ...

    async def find_open_pr(self, repo_slug: str, head_branch: str) -> PRInfo | None: ...

    async def create_pr(
        self, repo_slug: str, head_branch: str, title: str, body: str, base: str = ""
    ) -> PRInfo: ...

    async def update_pr_body(self, repo_slug: str, number: int, body: str) -> None: ...

    async def poll_checks(self, repo_slug: str, ref: str) -> CheckStatus: ...


def redact(text: str, secrets: list[str]) -> str:
    """把 token 从任意输出里抹掉（错误信息会被写进 job 审计，必须过这一层）。"""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
    return out


class GitHubVCS:
    def __init__(self, config: VCSConfig, *, transport: Any = None) -> None:
        self.config = config
        self._transport = transport  # httpx.AsyncBaseTransport，测试注入
        self._token_cache: str | None = None

    # -- 凭证 -------------------------------------------------------------

    async def resolve_token(self, work_dir: str | None = None) -> str:
        """显式配置优先；否则从 git credential helper 取（不落盘、不进 git）。"""
        if self._token_cache is not None:
            return self._token_cache
        token = (self.config.token or "").strip()
        if token:
            from mewcode.config import resolve_env_vars

            resolved = resolve_env_vars(token)
            if resolved and "${" not in resolved:
                self._token_cache = resolved
                return resolved

        protocol, host = await self._remote_transport(work_dir)
        if not host:
            raise VCSAuthError(
                "no VCS token: set service.vcs.token or configure a git credential helper"
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "credential",
                "fill",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # 无人值守下绝不能出现交互：GCM_INTERACTIVE=never 让它宁可失败
                # 也不弹窗（实测：多账号时弹"Select an account"会把 headless
                # 进程挂死，直到 30s 超时）。GCM_PROVIDER=generic 让 GCM 直接
                # 读存储而不走它的账号选择流程（只影响 GCM，其他 helper 忽略）。
                env={
                    **os.environ,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GCM_INTERACTIVE": "never",
                    "GCM_PROVIDER": "generic",
                },
            )
            payload = f"protocol={protocol}\nhost={host}\n\n".encode()
            stdout, _ = await asyncio.wait_for(proc.communicate(payload), timeout=30)
        except (TimeoutError, OSError) as e:
            raise VCSAuthError(f"git credential helper unavailable: {e}") from e

        password = ""
        for line in (stdout or b"").decode("utf-8", errors="replace").splitlines():
            if line.startswith("password="):
                password = line[len("password="):].strip()
        if not password:
            raise VCSAuthError(
                f"no stored credential for {host}: set service.vcs.token or run `git push` once"
            )
        self._token_cache = password
        return password

    async def _remote_transport(self, work_dir: str | None) -> tuple[str, str]:
        """从 remote URL 推出 (protocol, host)，供 credential helper 查询。"""
        url = self.config.remote or "origin"
        if work_dir:
            code, out = await self._git(work_dir, ["remote", "get-url", self.config.remote or "origin"])
            if code == 0 and out.strip():
                url = out.strip().splitlines()[0]
        m = re.match(r"^(?P<proto>https?)://(?P<host>[^/]+)/", url)
        if m:
            return m.group("proto"), m.group("host")
        m = re.match(r"^git@(?P<host>[^:]+):", url)
        if m:
            return "ssh", m.group("host")
        # remote 只是个名字（如 "origin"）时退回 API 主机：api.github.com -> github.com
        api_host = re.sub(r"^https?://", "", self.config.api_base).split("/")[0]
        return "https", re.sub(r"^api\.", "", api_host)

    # -- 本地 git ---------------------------------------------------------

    async def _git(self, cwd: str, args: list[str], *, askpass_token: str = "") -> tuple[int, str]:
        # 注意：不要设 GIT_CONFIG_NOSYSTEM —— 它会关掉 Windows 系统级
        # core.autocrlf，使每个 CRLF 文件都显示为已修改，PR 里会混进
        # 整份文件的换行符改动。这里只关交互式提问。
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
        }
        if askpass_token:
            # 关键：清空 credential.helper。push 的 URL 里带 x-access-token 用户名，
            # 交给 Git Credential Manager 会触发交互式认证（实测卡在
            # git-credential-manager.exe 上直到 job 超时）——headless 服务里
            # 没有任何人能回答它。凭证只由 GIT_ASKPASS 提供。
            args = ["-c", "credential.helper=", *args]
        askpass_file = ""
        if askpass_token:
            # token 不进 argv：写一个临时 askpass 脚本，从环境变量里取。
            # Windows 上 git 通过 cmd 执行 .cmd；POSIX 上是可执行 sh 脚本。
            if sys.platform == "win32":
                suffix, content = ".cmd", "@echo off\r\necho %MCODE_GIT_TOKEN%\r\n"
            else:
                suffix, content = ".sh", '#!/bin/sh\nprintf \'%s\' "$MCODE_GIT_TOKEN"\n'
            fd, askpass_file = tempfile.mkstemp(prefix="mewcode-askpass-", suffix=suffix)
            os.close(fd)
            Path(askpass_file).write_text(content, encoding="utf-8")
            if sys.platform != "win32":
                os.chmod(askpass_file, 0o700)
            env["GIT_ASKPASS"] = askpass_file
            env["MCODE_GIT_TOKEN"] = askpass_token

        def _run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                stdin=subprocess.DEVNULL,
                env=env,
            )

        try:
            result = await asyncio.to_thread(_run)
        except (subprocess.SubprocessError, OSError) as e:
            return 1, f"(git failed: {e})"
        finally:
            if askpass_file:
                try:
                    os.unlink(askpass_file)
                except OSError:  # pragma: no cover
                    pass
        return result.returncode, result.stdout + result.stderr

    async def head_sha(self, work_dir: str) -> str:
        code, out = await self._git(work_dir, ["rev-parse", "HEAD"])
        if code != 0:
            raise VCSError(f"cannot read HEAD in {work_dir}: {out.strip()}")
        return out.strip().splitlines()[0]

    async def resolve_repo_slug(self, work_dir: str) -> str:
        code, out = await self._git(work_dir, ["remote", "get-url", self.config.remote or "origin"])
        url = out.strip().splitlines()[0] if (code == 0 and out.strip()) else ""
        m = _SLUG_RE.search(url)
        if not m:
            raise VCSError(f"cannot derive owner/repo from remote url: {url!r}")
        return f"{m.group('owner')}/{m.group('repo')}"

    async def ensure_branch(self, work_dir: str, branch: str) -> None:
        """把 worktree 的当前分支重命名为目标发布分支。"""
        if not branch or ".." in branch or not re.match(r"^[A-Za-z0-9._/-]+$", branch):
            raise VCSError(f"invalid branch name: {branch!r}")
        code, out = await self._git(work_dir, ["branch", "-m", branch])
        if code != 0:
            raise VCSError(f"cannot rename branch to {branch}: {out.strip()}")

    async def commit_all(self, work_dir: str, message: str, exclude: tuple[str, ...] = ()) -> bool:
        """提交 worktree 内全部改动（排除服务自身的目录）。返回是否产生了提交。"""
        add_args = ["add", "-A", "--", "."]
        for pattern in exclude:
            add_args.append(pattern if pattern.startswith(":(") else f":(exclude){pattern}")
        code, out = await self._git(work_dir, add_args)
        if code != 0:
            raise VCSError(f"git add failed: {out.strip()}")

        code, out = await self._git(work_dir, ["status", "--porcelain"])
        if code != 0:
            raise VCSError(f"git status failed: {out.strip()}")
        staged = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("??")]
        if not staged:
            return False

        code, out = await self._git(work_dir, ["commit", "-m", message])
        if code != 0:
            raise VCSError(f"git commit failed: {out.strip()}")
        return True

    async def remote_url(self, work_dir: str) -> str:
        """推送到远端用的 URL：只含用户名，密码交给 GIT_ASKPASS（token 不进 argv）。"""
        protocol, host = await self._remote_transport(work_dir)
        slug = await self.resolve_repo_slug(work_dir)
        user = "x-access-token" if "github" in host else "git"
        return f"{protocol}://{user}@{host}/{slug}.git"

    async def push(self, work_dir: str, branch: str) -> None:
        token = await self.resolve_token(work_dir)
        url = await self.remote_url(work_dir)
        code, out = await self._git(
            work_dir,
            ["push", url, f"HEAD:refs/heads/{branch}"],
            askpass_token=token,
        )
        if code != 0:
            message = redact(out.strip(), [token])
            if "non-fast-forward" in message or "fetch first" in message:
                # 分支是服务独占的：远端被别的东西改过说明有人干预过，交给人处理
                raise VCSError(
                    f"refusing to overwrite remote branch {branch} (non-fast-forward); "
                    f"a human may have pushed to it: {message}"
                )
            raise VCSError(f"git push failed: {message}")

    # -- GitHub REST ------------------------------------------------------

    async def _api(self, method: str, path: str, **kwargs: Any) -> Any:
        import httpx

        token = await self.resolve_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{self.config.api_base.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=30) as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as e:
            raise VCSError(redact(f"github api request failed: {e}", [token])) from e
        if response.status_code in (401, 403):
            raise VCSAuthError(redact(f"github api auth failed ({response.status_code})", [token]))
        if response.status_code >= 400:
            raise VCSError(
                redact(f"github api {method} {path} -> {response.status_code}: {response.text[:300]}", [token])
            )
        return response.json()

    async def find_open_pr(self, repo_slug: str, head_branch: str) -> PRInfo | None:
        owner = repo_slug.split("/")[0]
        data = await self._api(
            "GET", f"/repos/{repo_slug}/pulls",
            params={"head": f"{owner}:{head_branch}", "state": "open"},
        )
        if not data:
            return None
        pr = data[0]
        return PRInfo(
            number=pr["number"], url=pr["html_url"],
            head_branch=head_branch, base_branch=(pr.get("base") or {}).get("ref", ""),
        )

    async def create_pr(
        self, repo_slug: str, head_branch: str, title: str, body: str, base: str = ""
    ) -> PRInfo:
        """开 PR。

        ``base`` 允许调用方显式指定目标分支（M3 W2：仓库策略的 target_branch，
        经 publisher 传入）；缺省仍取服务配置。webhook payload 永远到不了这里
        ——M1 的安全红线（base 不可被告警内容改写）不变：策略文件是仓库方写的
        配置，不是告警数据。
        """
        base = base or self.config.base_branch
        if not base:
            raise VCSError("service.vcs.base_branch is not configured")
        data = await self._api(
            "POST", f"/repos/{repo_slug}/pulls",
            json={"title": title, "head": head_branch, "base": base, "body": body},
        )
        return PRInfo(
            number=data["number"], url=data["html_url"], head_branch=head_branch, base_branch=base
        )

    async def update_pr_body(self, repo_slug: str, number: int, body: str) -> None:
        await self._api("PATCH", f"/repos/{repo_slug}/pulls/{number}", json={"body": body})

    async def poll_checks(self, repo_slug: str, ref: str) -> CheckStatus:
        """轮询 check-runs 直到有结论/超时。返回聚合状态。"""
        interval = max(1, self.config.ci_poll_interval_seconds)
        deadline = asyncio.get_running_loop().time() + max(1, self.config.ci_timeout_seconds)
        grace = max(0, self.config.ci_none_grace_seconds)
        start = asyncio.get_running_loop().time()

        while True:
            status = await self._collect_checks(repo_slug, ref)
            if status.state == "success" or status.is_failure:
                return status
            now = asyncio.get_running_loop().time()
            if status.state == "none" and now - start >= grace:
                return status
            if now >= deadline:
                return CheckStatus(
                    state="pending",
                    details=f"CI did not conclude within {self.config.ci_timeout_seconds}s",
                )
            await asyncio.sleep(interval)

    async def _collect_checks(self, repo_slug: str, ref: str) -> CheckStatus:
        data = await self._api("GET", f"/repos/{repo_slug}/commits/{ref}/check-runs")
        runs = data.get("check_runs") or []
        if not runs:
            return CheckStatus(state="none", details="no check runs reported for this commit")

        failing = [
            r for r in runs
            if r.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required", "startup_failure")
        ]
        if failing:
            names = ", ".join(r.get("name", "?") for r in failing[:5])
            return CheckStatus(state="failure", details=f"failing checks: {names}")

        unfinished = [r for r in runs if r.get("status") != "completed"]
        if unfinished:
            return CheckStatus(
                state="pending", details=f"{len(unfinished)} of {len(runs)} checks still running"
            )

        skipped = [r for r in runs if r.get("conclusion") in ("neutral", "skipped")]
        details = f"all {len(runs)} checks passed"
        if skipped:
            details += f" ({len(skipped)} skipped/neutral)"
        return CheckStatus(state="success", details=details)
