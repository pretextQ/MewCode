
from __future__ import annotations

import os
import re
import shlex

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/\S*"), "递归强制删除绝对路径"),
    (re.compile(r"mkfs\."), "格式化磁盘"),
    (re.compile(r"dd\s+if=.*of=/dev/"), "直接写磁盘设备"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "递归修改根目录权限"),
    (re.compile(r":\(\)\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"curl\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r"wget\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r">\s*/dev/sd"), "覆盖磁盘设备"),
]

# win32 专属模式（大小写不敏感）。BYPASS/DONT_ASK 模式下没有 ask 兜底，
# 黑名单是这些命令的唯一屏障
_WINDOWS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rd(?:ir)?\s+/s\b", re.IGNORECASE), "递归强制删除目录 (rd /s)"),
    (re.compile(r"del\s+/f\s+/s\b", re.IGNORECASE), "递归强制删除文件 (del /f /s)"),
    (re.compile(r"format\s+[a-z]:", re.IGNORECASE), "格式化磁盘"),
    (re.compile(r"reg\s+add\s+[^\n]*\\currentversion\\run\b", re.IGNORECASE), "写入注册表自启动项"),
    (re.compile(r"certutil\s+[^\n]*-urlcache\b", re.IGNORECASE), "certutil 远程下载"),
    (re.compile(
        r"powershell(\.\w+)?\s+[^\n]*-\w*enc(?:oded(?:command)?)?\b", re.IGNORECASE
    ), "PowerShell 编码命令执行"),
    (re.compile(
        r"remove-item\b(?=[^\n]*-recurse\b)(?=[^\n]*-force\b)"
        r"(?=[^\n]*\s[a-z]:\\(?:\s|-|/|$))",
        re.IGNORECASE,
    ), "PowerShell 递归强制删除盘根"),
]


_SAFE_COMMANDS = frozenset({
    "ls", "dir", "pwd", "echo", "cat", "head", "tail", "wc",
    "which", "whereis", "whoami", "hostname", "uname",
    "date", "cal", "uptime", "df", "du", "free", "env", "printenv",
    "file", "stat", "readlink", "realpath", "basename", "dirname",
    "sort", "uniq", "tr", "cut", "grep",
    "diff", "comm", "true", "false", "test",
    "git status", "git log", "git diff", "git show", "git branch",
    "git tag", "git remote", "git rev-parse", "git ls-files",
    "git blame", "git stash list", "go version", "go env",
    "node -v", "npm -v", "python --version", "pip list",
    "cargo --version", "rustc --version", "java -version", "java --version",
})

# 白名单命令借绝对路径读沙箱外文件（cat C:/Users/<u>/.ssh/id_rsa、cat /etc/passwd），
# 命中即不自动放行，回退到正常 ask 流程
_UNSAFE_PATH_RE = re.compile(
    r"(?:"
    r"[A-Za-z]:[\\/]"
    r"|(?<![\w/])~"
    r"|(?<![\w/])/(?:etc|home|root|users|var|usr|opt|tmp|proc|sys|dev|bin|sbin|lib|boot|mnt|srv)\b"
    r")",
    re.IGNORECASE,
)


def is_safe_command(command: str) -> bool:
    trimmed = command.strip()
    if not trimmed:
        return False
    for ch in ("|", ";", "&", ">", "<", "$", "%", "!", "^", "`", "\n", "\r", "//", "\\\\"):
        if ch in trimmed:
            return False
    if _UNSAFE_PATH_RE.search(trimmed):
        return False
    try:
        words = shlex.split(trimmed)
    except ValueError:
        return False
    if words[:2] in (["git", "branch"], ["git", "tag"], ["git", "remote"]):
        args = words[2:]
        if not args:
            return True
        # These commands mix listing and mutation. Never infer read-only
        # behavior from their prefix, or allow unknown options/positionals.
        if words[1] == "branch":
            return all(arg in {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
                               "--list", "-l", "--show-current", "--no-color"} for arg in args)
        if words[1] == "tag":
            return all(arg in {"-l", "--list", "-n", "--no-column"} for arg in args)
        return args in (["-v"], ["--verbose"])
    return any(
        trimmed == safe or trimmed.startswith(safe + " ")
        for safe in _SAFE_COMMANDS
    )


class DangerousCommandDetector:


    def __init__(self, extra_patterns: list[tuple[str, str]] | None = None) -> None:
        self._patterns = list(_DANGEROUS_PATTERNS)
        if os.name == "nt":
            self._patterns += _WINDOWS_PATTERNS
        if extra_patterns:
            for regex_str, reason in extra_patterns:
                self._patterns.append((re.compile(regex_str), reason))


    def detect(self, command: str) -> tuple[bool, str]:
        # Parse option sets rather than relying on one spelling/order of flags.
        for segment in re.split(r"[;&|\n\r]+", command):
            try:
                words = shlex.split(segment, posix=False)
            except ValueError:
                continue
            if not words:
                continue
            executable = words[0].strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
            args = [word.strip("\"'") for word in words[1:]]
            if executable in {"rm", "rm.exe"}:
                options = args[:args.index("--")] if "--" in args else args
                short_flags = "".join(a[1:] for a in options if a.startswith("-") and not a.startswith("--"))
                recursive = "r" in short_flags.lower() or "--recursive" in options
                force = "f" in short_flags or "--force" in options
                absolute = any(a.startswith(("/", "~", "$", "%", "\\"))
                               or re.match(r"^[a-zA-Z]:", a) for a in args)
                if recursive and force and absolute:
                    return True, "递归强制删除绝对路径"
            if os.name == "nt":
                flags = {a.lower() for a in args}
                if executable in {"rd", "rmdir", "rd.exe", "rmdir.exe", "del", "erase"} and "/s" in flags:
                    return True, "Windows 递归删除"
        for pattern, reason in self._patterns:
            if pattern.search(command):
                return True, reason
        return False, ""
