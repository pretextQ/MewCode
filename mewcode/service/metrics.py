"""M3 W1: 指标暴露与复盘报告。

数据源只有 JobStore（jobs 表 + job_events 审计表）——不建第二套统计管线：

- token 用量：jobs 表的累计列（执行链每次 agent 运行后经 ``add_usage`` 累加），
  每次 attempt 的明细仍在 ``agent_finished`` 审计事件里；
- 时长（alert→PR、总时长）：由 transition 审计事件的时间戳推导，
  不新增任何计时设施；
- 状态分布：jobs 表的当前状态（终态才计入时长/成败类指标）。

诚实口径（docs/evolution/README.md 第五节）：token 只报告原始数量，
不换算金额（没有定价数据，不编造汇率）；时长只统计有确定终点的事件
（alert→PR 需要 job 真的开过 PR，总时长需要 job 到达终态）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .jobs import TERMINAL_STATES, Job, JobEvent, JobStore

#: /metrics 单次抓取扫描的 job 上限。告警量级的服务远达不到；
#: 达到上限时指标只反映最近的 job（诚实降级好过无限内存）。
METRICS_JOB_LIMIT = 10_000

#: 时长直方图的桶边界（秒）。上界覆盖典型 job（分钟级）与 job 超时上限（30 分钟）。
DURATION_BUCKETS = (30, 60, 120, 300, 600, 900, 1800, 3600)

_RE_INT = {
    name: re.compile(rf"{name}=(\d+)")
    for name in ("attempt", "tool_calls", "tokens_in", "tokens_out")
}

#: 报告里纳入"验证证据"小节的审计事件（顺序即展示顺序）
VERIFICATION_KINDS = ("baseline_tests", "verify_tests", "test_delta")
INTEGRATION_KINDS = (
    "integration_up",
    "integration_tests",
    "integration_down",
    "integration_skipped",
    "integration_error",
)
CI_KINDS = ("ci_status", "ci_retry")

_TOKEN_NOTE = "raw provider-reported token counts; not converted to currency (no pricing data)"


def _esc(value: str) -> str:
    """Prometheus 文本格式的 label 值转义。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _parse_ts(value: str) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    """两个 ISO 时间戳的秒差；任一端无法解析返回 None；时钟回拨收敛到 0。"""
    start_ts = _parse_ts(start or "")
    end_ts = _parse_ts(end or "")
    if start_ts is None or end_ts is None:
        return None
    return max(0.0, end_ts - start_ts)


def _int_after(name: str, text: str) -> int:
    match = _RE_INT[name].search(text)
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# /metrics：聚合 + Prometheus 文本渲染
# ---------------------------------------------------------------------------


@dataclass
class MetricsSnapshot:
    """一次指标抓取的聚合结果（纯数据，便于测试与后续复用）。"""

    jobs_by_status_repo: Counter = field(default_factory=Counter)
    #: 已到终态的 job：告警受理 → 终态转移
    terminal_durations: list[float] = field(default_factory=list)
    #: 开过 PR 的 job：告警受理 → pr_opened 转移（MTTR 核心）
    pr_durations: list[float] = field(default_factory=list)
    merged_total: int = 0
    escalate_total: int = 0
    #: repo -> (tokens_in, tokens_out)
    tokens_by_repo: dict[str, tuple[int, int]] = field(default_factory=dict)


async def collect_metrics(store: JobStore) -> MetricsSnapshot:
    """从 JobStore 聚合指标快照。"""
    jobs = await store.list_jobs(limit=METRICS_JOB_LIMIT)
    history = await store.transition_history()

    pr_at: dict[str, str] = {}
    terminal_at: dict[str, str] = {}
    for rec in history:
        # 只记第一次进入该状态的时间：重试链里回到 fixing 再来一遍，
        # MTTR 口径以"第一次开出 PR"为准
        if rec.to_state == "pr_opened":
            pr_at.setdefault(rec.job_id, rec.ts)
        if rec.to_state in TERMINAL_STATES:
            terminal_at.setdefault(rec.job_id, rec.ts)

    snapshot = MetricsSnapshot()
    tokens: dict[str, list[int]] = {}
    for job in jobs:
        snapshot.jobs_by_status_repo[(job.status, job.repo)] += 1
        if job.status == "merged":
            snapshot.merged_total += 1
        if job.status == "escalate":
            snapshot.escalate_total += 1
        if job.status in TERMINAL_STATES:
            end = terminal_at.get(job.id) or job.updated_at
            duration = _duration_seconds(job.created_at, end)
            if duration is not None:
                snapshot.terminal_durations.append(duration)
        if job.id in pr_at:
            duration = _duration_seconds(job.created_at, pr_at[job.id])
            if duration is not None:
                snapshot.pr_durations.append(duration)
        in_out = tokens.setdefault(job.repo, [0, 0])
        in_out[0] += job.tokens_in
        in_out[1] += job.tokens_out
    snapshot.tokens_by_repo = {repo: (in_out[0], in_out[1]) for repo, in_out in tokens.items()}
    return snapshot


def _render_histogram(name: str, help_text: str, durations: list[float]) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
    for bucket in DURATION_BUCKETS:
        count = sum(1 for d in durations if d <= bucket)
        lines.append(f'{name}_bucket{{le="{bucket}"}} {count}')
    lines.append(f'{name}_bucket{{le="+Inf"}} {len(durations)}')
    lines.append(f"{name}_sum {sum(durations):.3f}")
    lines.append(f"{name}_count {len(durations)}")
    return lines


def render_prometheus(snapshot: MetricsSnapshot, *, model: str = "") -> str:
    """渲染 Prometheus 文本格式（version 0.0.4）。"""
    model_label = model or "unknown"
    lines = [
        "# HELP mewcode_jobs_total Jobs by current lifecycle status and repo.",
        "# TYPE mewcode_jobs_total counter",
    ]
    for (status, repo), count in sorted(snapshot.jobs_by_status_repo.items()):
        lines.append(f'mewcode_jobs_total{{status="{_esc(status)}",repo="{_esc(repo)}"}} {count}')
    lines += _render_histogram(
        "mewcode_job_duration_seconds",
        "Seconds from alert intake to terminal state (terminal jobs only).",
        snapshot.terminal_durations,
    )
    lines += _render_histogram(
        "mewcode_alert_to_pr_seconds",
        "Seconds from alert intake to PR opened (MTTR core; jobs that opened a PR).",
        snapshot.pr_durations,
    )
    lines += [
        "# HELP mewcode_fix_merged_total Jobs whose fix PR reached merged.",
        "# TYPE mewcode_fix_merged_total counter",
        f"mewcode_fix_merged_total {snapshot.merged_total}",
        "# HELP mewcode_escalate_total Jobs escalated to humans.",
        "# TYPE mewcode_escalate_total counter",
        f"mewcode_escalate_total {snapshot.escalate_total}",
        "# HELP mewcode_token_cost_total Tokens consumed (input+output), by model and repo.",
        "# TYPE mewcode_token_cost_total counter",
    ]
    for repo, (tokens_in, tokens_out) in sorted(snapshot.tokens_by_repo.items()):
        lines.append(
            f'mewcode_token_cost_total{{model="{_esc(model_label)}",repo="{_esc(repo)}"}}'
            f" {tokens_in + tokens_out}"
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# /jobs/{id}/report：单 job 复盘报告
# ---------------------------------------------------------------------------


def _parse_agent_finished(event: JobEvent) -> dict:
    return {
        "ts": event.ts,
        "attempt": _int_after("attempt", event.detail),
        "tool_calls": _int_after("tool_calls", event.detail),
        "tokens_in": _int_after("tokens_in", event.detail),
        "tokens_out": _int_after("tokens_out", event.detail),
        "detail": event.detail,
    }


def _events_by_kind(events: list[JobEvent], kinds: tuple[str, ...]) -> list[dict]:
    return [
        {"ts": e.ts, "kind": e.kind, "detail": e.detail} for e in events if e.kind in kinds
    ]


def build_job_report(job: Job, events: list[JobEvent]) -> dict:
    """把一个 job 的审计轨迹投影成结构化复盘报告。

    纯函数（store 读取在调用方）：输入 job 行 + 全部审计事件，
    输出状态轨迹 / 工具使用 / token / 验证证据 / MCP 调用五个板块。
    """
    trajectory: list[dict] = []
    attempts: list[dict] = []
    tools: Counter = Counter()
    mcp: dict[str, list[dict]] = {"ready": [], "used": [], "errors": [], "teardown_cancellation": []}
    prompts = 0
    escalations: list[dict] = []
    for event in events:
        if event.kind in ("transition", "recovery_reset"):
            arrow = event.detail.find(" -> ")
            if arrow >= 0:
                rest = event.detail[arrow + 4:]
                to_state, _, reason = rest.partition(": ")
                trajectory.append({
                    "ts": event.ts,
                    "kind": event.kind,
                    "from": event.detail[:arrow].strip(),
                    "to": to_state.strip(),
                    "reason": reason,
                })
        elif event.kind == "agent_finished":
            attempts.append(_parse_agent_finished(event))
        elif event.kind == "agent_tool_use":
            # detail 形如 "toolName: hint"（见 execution._make_on_event）
            tools[event.detail.split(":", 1)[0].strip()] += 1
        elif event.kind == "agent_prompt":
            prompts += 1
        elif event.kind == "agent_mcp_ready":
            mcp["ready"].append({"ts": event.ts, "detail": event.detail})
        elif event.kind == "agent_mcp_used":
            mcp["used"].append({"ts": event.ts, "detail": event.detail})
        elif event.kind == "agent_mcp_error":
            mcp["errors"].append({"ts": event.ts, "detail": event.detail})
        elif event.kind == "agent_teardown_cancellation":
            mcp["teardown_cancellation"].append({"ts": event.ts, "detail": event.detail})
        elif event.kind == "escalated":
            escalations.append({"ts": event.ts, "reason": event.detail})

    pr_at = next((t["ts"] for t in trajectory if t["to"] == "pr_opened"), None)
    terminal_at = None
    if job.is_terminal:
        terminal_at = next((t["ts"] for t in reversed(trajectory) if t["to"] == job.status), None)
    tool_total = sum(tools.values())
    return {
        "job": {
            "id": job.id,
            "repo": job.repo,
            "severity": job.severity,
            "title": job.title,
            "status": job.status,
            "attempts": job.attempts,
            "branch": job.branch,
            "pr_url": job.pr_url,
            "ci_status": job.ci_status,
            "last_error": job.last_error,
            "result": job.result,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "tokens": {"input": job.tokens_in, "output": job.tokens_out, "total": job.tokens_in + job.tokens_out},
        },
        "timing": {
            "alert_to_pr_seconds": _duration_seconds(job.created_at, pr_at),
            "total_duration_seconds": (
                _duration_seconds(job.created_at, terminal_at or job.updated_at)
                if job.is_terminal
                else None
            ),
            "pr_opened_at": pr_at,
        },
        "trajectory": trajectory,
        "agent": {
            "attempts": attempts,
            "tools": {**dict(tools.most_common()), "_total": tool_total},
            "prompt_count": prompts,
            "mcp": mcp,
        },
        "verification": {
            "tests": _events_by_kind(events, VERIFICATION_KINDS),
            "integration": _events_by_kind(events, INTEGRATION_KINDS),
            "ci": _events_by_kind(events, CI_KINDS),
        },
        "escalations": escalations,
        "notes": [_TOKEN_NOTE],
    }


async def job_report(store: JobStore, job_id: str) -> dict:
    """按 job 生成复盘报告；job 不存在抛 :class:`JobNotFound`。"""
    job = await store.get_or_raise(job_id)
    events = await store.events(job_id)
    return build_job_report(job, events)


# ---------------------------------------------------------------------------
# /costs：按仓库聚合的成本报告
# ---------------------------------------------------------------------------


async def cost_report(store: JobStore, *, model: str = "", repo: str | None = None) -> dict:
    """按仓库（× 当前服务唯一模型）聚合 token 成本。

    服务当前对所有 job 使用同一个 provider/model（``config.providers[0]``），
    因此 model 是报告级字段而不是聚合维度；等出现按 job 选模型的路由时，
    它才需要变成 GROUP BY 的一维。
    """
    jobs = await store.list_jobs(limit=METRICS_JOB_LIMIT)
    rows: dict[str, dict[str, int]] = {}
    for job in jobs:
        if repo is not None and job.repo != repo:
            continue
        row = rows.setdefault(job.repo, {"jobs": 0, "tokens_in": 0, "tokens_out": 0})
        row["jobs"] += 1
        row["tokens_in"] += job.tokens_in
        row["tokens_out"] += job.tokens_out

    def _totals(row: dict[str, int]) -> dict[str, int]:
        return {**row, "tokens_total": row["tokens_in"] + row["tokens_out"]}

    repos = [
        {"repo": name, **_totals(row)}
        for name, row in sorted(rows.items())
    ]
    total = _totals({
        "jobs": sum(r["jobs"] for r in repos),
        "tokens_in": sum(r["tokens_in"] for r in repos),
        "tokens_out": sum(r["tokens_out"] for r in repos),
    })
    return {
        "unit": "tokens",
        "model": model or "unknown",
        "repos": repos,
        "total": total,
        "notes": [_TOKEN_NOTE],
    }
