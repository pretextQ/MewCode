"""Bounded, on-demand Git reads for the terminal presentation only."""
from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

GIT_TIMEOUT = 3.0
GIT_OUTPUT_LIMIT = 48000
GIT_NOTICE = "可能包含原有未提交改动；不能归属为本任务生成。"


@dataclass(frozen=True)
class GitOutput:
    code: int
    data: bytes
    truncated: bool = False


async def read_git(
    work_dir: str, args: list[str], limit: int = GIT_OUTPUT_LIMIT, input_data: bytes | None = None,
) -> GitOutput:
    # Shield cleanup from repeated app/worker cancellation during terminal shutdown.
    task = asyncio.create_task(_read_git(work_dir, args, limit, input_data))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
        if not task.cancelled():
            task.exception()
        raise


async def _read_git(work_dir: str, args: list[str], limit: int, input_data: bytes | None) -> GitOutput:
    """No shell, pager, prompts, fsmonitor, external diff or textconv.

    Read only a bounded prefix, then kill and reap on truncation/timeout/cancellation.
    stderr is discarded: paths and errors shown by callers are controlled plain text.
    """
    overrides: list[str] = []
    if args[0] in {"status", "diff"}:
        # Working-tree reads can run clean/process filters even with --no-textconv.
        # Discover names only (never commands/secrets) and neutralize all drivers.
        filters = await _read_git(work_dir, [
            "config", "--null", "--name-only", "--get-regexp", r"^filter\..*\.(clean|process|required)$",
        ], 16000, None)
        if filters.truncated or filters.code not in (0, 1):
            raise OSError("Unable to safely inspect Git filter configuration")
        try:
            keys = filters.data.decode("utf-8").split("\0")
        except UnicodeError as exc:
            raise OSError("Unsupported Git filter configuration encoding") from exc
        names = set()
        for key in keys:
            if not key:
                continue
            match = re.fullmatch(r"filter\.(.+)\.(clean|process|required)", key)
            if match is None:
                raise OSError("Unsupported Git filter name")
            names.add(match[1])
        for name in sorted(names):
            overrides.extend([
                "-c", f"filter.{name}.clean=", "-c", f"filter.{name}.process=",
                "-c", f"filter.{name}.required=false",
            ])
        if len(names) > 32 or sum(map(len, overrides)) > 12000:
            raise OSError("Git filter configuration exceeds the preview budget")

    creation = asyncio.create_task(asyncio.create_subprocess_exec(
        "git", "--no-pager", *(["--literal-pathspecs"] if args[0] == "diff" else []),
        "-c", "core.quotePath=false",
        "-c", "core.fsmonitor=false", *overrides, *args, cwd=work_dir,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GIT_OPTIONAL_LOCKS": "0"},
        limit=4096, stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    ))
    try:
        process = await asyncio.shield(creation)
    except asyncio.CancelledError:
        process = await creation
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    assert process.stdout is not None
    if process.stdin is not None:
        process.stdin.write(input_data or b"")
        process.stdin.close()
    data = bytearray()
    try:
        async with asyncio.timeout(GIT_TIMEOUT):
            while len(data) <= limit:
                chunk = await process.stdout.read(min(4096, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            truncated = len(data) > limit
            if truncated:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
            code = await process.wait()
            return GitOutput(code, bytes(data[:limit]), truncated)
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


def display_text(value: str) -> str:
    """Keep control codes out of the terminal; preserve tabs/newlines in output."""
    return "".join(c if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf"} else "�" for c in value)


def safe_diff_path(path: str) -> bool:
    """Conservatively omit common credential/config locations from bulk preview."""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    for part in parts:
        name = part.lower()
        if (name.startswith(".env") or name in {".mewcode", ".ssh", ".aws", ".azure", ".git"}
                or any(word in name for word in ("secret", "credential", "token", "password"))
                or name.endswith((".pem", ".key", ".p12", ".pfx")) or name.startswith("id_rsa")):
            return False
    return True


async def branch_name(work_dir: str) -> str:
    try:
        result = await read_git(work_dir, ["symbolic-ref", "--quiet", "--short", "HEAD"], 240)
        if result.code == 0 and not result.truncated:
            return display_text(result.data.decode("utf-8", errors="replace").strip()) or "未知"
        head = await read_git(work_dir, ["rev-parse", "--short", "HEAD"], 80)
        if head.code == 0:
            return "detached " + head.data.decode("ascii", errors="replace").strip()
    except (OSError, TimeoutError):
        pass
    return "未知 / 非 Git"


async def workspace_changes(work_dir: str, include_diff: bool = False) -> str:
    try:
        status = await read_git(work_dir, [
            "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=normal",
            "--ignore-submodules=all",
        ])
        if status.code and not status.truncated:
            return "非 Git 工作区或 Git 状态不可用。"
        entries = status.data.split(b"\0")[:-1]  # never parse a truncated final filename
        lines = [GIT_NOTICE, "未跟踪项只列路径；默认仅列状态，diff 按需读取。", ""]
        paths = []
        for raw in entries[:200]:
            entry = raw.decode("utf-8", errors="replace")
            lines.append(display_text(entry.replace("\n", "\\n")))
            if raw[:2] != b"??" and safe_diff_path(entry[3:]):
                paths.append(entry[3:])
        if not entries and not status.truncated:
            lines.append("没有未提交改动。")
        if status.truncated or len(entries) > 200:
            lines.append("… 状态列表已截断")
        if include_diff:
            if paths:
                ignored = await read_git(
                    work_dir, ["check-ignore", "--no-index", "-z", "--stdin"],
                    input_data=b"\0".join(path.encode("utf-8") for path in paths) + b"\0",
                )
                excluded = set(ignored.data.decode("utf-8", errors="replace").split("\0"))
                paths = [path for path in paths if path not in excluded] if ignored.code in (0, 1) else []
                if ignored.truncated:
                    paths = []
            lines.append("\n已跟踪文件 diff（常见凭据路径省略；不包含未跟踪内容）：")
            if not paths:
                lines.append("没有可预览的已跟踪文件。")
            remaining = GIT_OUTPUT_LIMIT
            for label, flags in [("暂存区", ["--cached"]), ("工作区", [])]:
                if not paths or remaining <= 0:
                    break
                result = await read_git(work_dir, [
                    "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                    "--unified=3", "--ignore-submodules=all", *flags, "--", *paths,
                ], remaining)
                patch = display_text(result.data.decode("utf-8", errors="replace"))
                if patch:
                    lines.extend([f"\n{label}：", patch])
                remaining -= len(result.data)
                if result.truncated:
                    lines.append("… diff 已截断")
                elif result.code:
                    lines.append("diff 查询失败，内容可能不完整。")
        return "\n".join(lines)
    except TimeoutError:
        return "Git 查询超时；按刷新重试。"
    except OSError:
        return "Git 不可用，或无法安全读取该工作区。"
