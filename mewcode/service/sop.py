"""服务层提示词：M1 内置的通用"告警排查 SOP"。

M2 会把这套 SOP 换成企业规范 Skill 包注入；M1 先内置一份最小可用的。
这里只负责"给 agent 什么指令"，不负责拼 PR body（PR body 由服务层从
job 记录结构化生成，见 W4 —— 不让 LLM 自由发挥）。
"""

from __future__ import annotations

import logging
from typing import Any

from .jobs import Job

log = logging.getLogger(__name__)

#: 单次注入的日志/描述上限，防止把整个告警风暴塞进首轮上下文
MAX_CONTEXT_CHARS = 6000

#: 服务默认启用的规范类 skill（可被 service.skills 覆盖；团队用自己的
#: .mewcode/skills/ 同名文件即可替换，加载优先级 仓库 > 用户 > 内置）
DEFAULT_SKILLS = ("incident-triage", "org-code-style")


def load_skill_bodies(work_dir: str, names: list[str] | tuple[str, ...]) -> dict[str, str]:
    """按加载优先级取出 skill 正文（仓库自带 > 用户级 > 内置）。

    服务层把正文**内联**进首轮提示词：这样直跑与容器两种执行模式行为一致，
    不依赖模型记得去调用 Skill 工具（无人值守下要的是确定性）。
    """
    from mewcode.skills.loader import SkillLoader

    try:
        available = SkillLoader(work_dir).load_all()
    except Exception as e:  # 加载失败退化为"没有 skill"，不能让作业停摆
        log.warning("skill loading failed for %s: %s", work_dir, e)
        return {}
    bodies: dict[str, str] = {}
    for name in names:
        skill = available.get(name)
        if skill is not None and skill.prompt_body.strip():
            bodies[name] = skill.prompt_body.strip()
    return bodies


def _truncate(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated, {len(text)} chars total)"


def render_alert_context(job: Job) -> str:
    """把 job.payload 渲染成人类可读的告警上下文（提示词与通知共用）。"""
    payload: dict[str, Any] = job.payload or {}
    source = payload.get("source", "unknown")
    lines = [
        f"- Alert source: {source}",
        f"- Repository: {job.repo}",
        f"- Severity: {job.severity}",
    ]
    if job.title:
        lines.append(f"- Title: {job.title}")

    if source == "alertmanager":
        labels = payload.get("labels") or {}
        annotations = payload.get("annotations") or {}
        common_annotations = payload.get("common_annotations") or {}
        if labels:
            lines.append("- Labels: " + ", ".join(f"{k}={v}" for k, v in sorted(labels.items())))
        for key in ("summary", "description", "runbook_url"):
            value = annotations.get(key) or common_annotations.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        if payload.get("generator_url"):
            lines.append(f"- Generator: {payload['generator_url']}")
        if payload.get("starts_at"):
            lines.append(f"- Started at: {payload['starts_at']}")
    else:
        summary = payload.get("summary") or ""
        if summary:
            lines.append(f"- Summary: {summary}")

    return "\n".join(lines)


def extract_logs(job: Job) -> str:
    """从 payload 里取出最有价值的"证据文本"（日志/堆栈/描述）。"""
    payload: dict[str, Any] = job.payload or {}
    parts: list[str] = []
    logs = payload.get("logs")
    if isinstance(logs, str) and logs.strip():
        parts.append(logs)
    if payload.get("source") == "alertmanager":
        annotations = payload.get("annotations") or {}
        common = payload.get("common_annotations") or {}
        for key in ("description", "summary"):
            value = annotations.get(key) or common.get(key)
            if isinstance(value, str) and value.strip() and value not in parts:
                parts.append(value)
    extra = payload.get("extra")
    if isinstance(extra, dict) and extra:
        import json

        parts.append(json.dumps(extra, ensure_ascii=False, indent=2, sort_keys=True))
    return _truncate("\n\n".join(parts))


def has_actionable_context(job: Job) -> tuple[bool, str]:
    """triaging 判据：告警信息不足以定位问题时不下手（M1 验收标准 2）。

    返回 (是否可继续, 不可继续时的原因)。宁可在 triaging 阶段 escalate，
    也不要让 agent 硬猜——猜出来的 PR 是垃圾 PR。
    """
    payload: dict[str, Any] = job.payload or {}
    if not payload:
        return False, "alert payload is empty"

    evidence = extract_logs(job)
    labels = payload.get("labels") or {}
    has_labels = bool(labels)
    has_title = bool(job.title.strip())

    if not evidence.strip() and not has_title and not has_labels:
        return False, (
            "insufficient alert context: no logs, no title and no labels to locate the problem"
        )
    return True, ""


def build_alert_prompt(
    job: Job,
    repo_path: str,
    test_command: str = "",
    baseline: str = "",
    feedback: str = "",
    skills: dict[str, str] | None = None,
    mcp_servers: list[tuple[str, str]] | None = None,
    integration_command: str = "",
) -> str:
    """组装交给 agent 的首轮指令（M1 通用 SOP + M2 规范注入 + 内部工具链）。

    ``feedback`` 用于验证失败后的重试：把上一轮的测试输出交给 agent，
    否则它会重复同样的修复思路（这正是有界重试存在的意义）。
    ``skills`` 是内联的规范正文（见 :func:`load_skill_bodies`）。
    ``mcp_servers`` 是 (名字, 说明) 列表：内部工具在工具表里可见，但没人
    告诉 agent "日志不在告警里、要去查"——它就可能直接猜。这一段就是那句
    提醒，只描述"能用什么"，不规定"必须用"。
    ``integration_command`` 是仓库的集成测试命令（M2 W4）：由服务层自起
    compose 依赖后执行，agent 自己跑不了（环境里没有 docker），所以只说
    "保持它能过"，避免它去折腾 docker。
    """
    context = render_alert_context(job)
    logs = extract_logs(job)
    sections = [
        "You are an on-call engineer agent working unattended on a production alert.",
        "",
        "## Alert",
        context,
    ]
    if logs:
        sections += ["", "## Evidence (logs / annotations)", "```", logs, "```"]
    if baseline:
        sections += [
            "",
            "## Baseline test result (before any change)",
            "```",
            _truncate(baseline, 2000),
            "```",
        ]
    if feedback:
        sections += [
            "",
            "## Previous attempt failed verification",
            "Your earlier fix did not pass the tests. Test output:",
            "```",
            feedback,
            "```",
            "Analyse why the previous attempt failed before changing anything again.",
        ]

    for name, body in (skills or {}).items():
        sections += ["", f"## Organization skill: {name}", body]

    if mcp_servers:
        sections += [
            "",
            "## Internal tools (read-only MCP servers)",
            "Besides your built-in tools you have read-only access to internal systems:",
        ]
        for name, description in mcp_servers:
            line = f"- `{name}`"
            if description:
                line += f" — {description}"
            sections.append(line)
        sections += [
            "Their tools appear in your tool list as `mcp_<server>_<tool>`",
            "(e.g. `mcp_logs_query_logs`). Use them when the alert evidence above is",
            "not enough to localise the problem — do not guess what the internal",
            "systems would show if you can simply look.",
        ]

    sections += [
        "",
        "## Your job",
        f"1. Reproduce or localise the root cause in the repository at `{repo_path}`.",
        "2. Fix it with the smallest correct change. Do not refactor unrelated code.",
        "3. Verify your fix: re-run the failing test or command and check it passes.",
        "",
        "## Rules",
        "- Work only inside the current working directory (an isolated git worktree).",
        "- Never run `git push`, `git merge`, `git rebase` against remote branches, or any",
        "  GitHub/PR command: publishing is done by the service, not by you. Local `git diff`,",
        "  `git status` and running tests are fine.",
        "- If you cannot determine the root cause from the evidence, stop and say so",
        "  explicitly instead of guessing. A wrong fix is worse than no fix.",
    ]
    if test_command:
        sections.append(f"- The repository's test command is: `{test_command}`")
    if integration_command:
        sections += [
            "- The service also runs the repository's integration tests against a",
            f"  docker-compose environment: `{integration_command}`. You cannot start it",
            "  yourself (no container runtime here) — just keep it passing.",
        ]
    sections += [
        "",
        "## Final message format",
        "End with a short report containing exactly these headings:",
        "- `ROOT CAUSE:` what actually broke and why",
        "- `FIX:` what you changed (files and the essence of each change)",
        "- `VERIFICATION:` the command(s) you ran and the observed result",
    ]
    if "org-code-style" in (skills or {}):
        sections.append(
            "- `SELF-CHECK:` the org-code-style checklist, one line per item "
            "(`- [x] ok` or `- [ ] not met: why`); this goes into the PR description"
        )
    sections.append(
        "If you did not fix anything, say why under `ROOT CAUSE:` and leave `FIX:` empty."
    )
    return "\n".join(sections)
