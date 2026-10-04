"""SQLite-backed job store with a validated lifecycle state machine.

The store is the single source of truth for job lifecycle. Every transition
is validated against :data:`TRANSITIONS` and appended to the ``job_events``
table, so the same table doubles as the audit log (M3 reuses it) and as the
"what was already tried" evidence attached to escalations.

Design references: docs/evolution/02-architecture.md section 4 (state machine,
retry ceiling, idempotency) and 03-m1-alert-driven.md W1/W2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

#: 正常推进路径：received → triaging → reproducing → fixing → verifying →
#: pr_opened → ci_gate → human_review → merged。
#: 任一执行阶段失败可转 escalate（升级人工，附已尝试分析）；
#: fix/verify/CI 失败可有限重试后回到 fixing。
TRANSITIONS: dict[str, frozenset[str]] = {
    "received": frozenset({"triaging", "invalid", "escalate"}),
    "triaging": frozenset({"reproducing", "invalid", "escalate"}),
    "reproducing": frozenset({"fixing", "cant_repro", "escalate"}),
    "fixing": frozenset({"verifying", "fix_failed", "escalate"}),
    "verifying": frozenset({"pr_opened", "verify_failed", "escalate"}),
    "pr_opened": frozenset({"ci_gate", "escalate"}),
    "ci_gate": frozenset({"human_review", "ci_failed", "escalate"}),
    "human_review": frozenset({"merged", "changes_requested", "escalate"}),
    # 重试入口：回到 fixing 重新走修复链
    "fix_failed": frozenset({"fixing", "escalate"}),
    "verify_failed": frozenset({"fixing", "escalate"}),
    "ci_failed": frozenset({"fixing", "escalate"}),
    "changes_requested": frozenset({"fixing", "escalate"}),
    # 终态
    "merged": frozenset(),
    "invalid": frozenset(),
    "cant_repro": frozenset(),
    "escalate": frozenset(),
}

#: 从这些状态回到 fixing 视为一次"重试"，受 attempts 上限约束；
#: changes_requested 是人工评审驱动的返工，不计入自动重试预算。
RETRY_STATES = frozenset({"fix_failed", "verify_failed", "ci_failed"})

TERMINAL_STATES = frozenset({"merged", "invalid", "cant_repro", "escalate"})

#: fix 阶段总尝试次数上限（首次 + 2 次重试 = 3），对应架构文档
#: "重试上限 N=2"：超限必须 escalate，不允许死循环烧 token。
DEFAULT_MAX_FIX_ATTEMPTS = 3


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class JobStoreError(Exception):
    """JobStore 层的基类异常。"""


class InvalidTransition(JobStoreError):
    """请求的状态转移不在状态机允许的边集合内。"""


class JobNotFound(JobStoreError):
    """指定的 job id 不存在。"""


@dataclass
class JobEvent:
    seq: int
    job_id: str
    ts: str
    kind: str
    detail: str


@dataclass
class Job:
    id: str
    fingerprint: str
    repo: str
    severity: str
    payload: dict
    status: str
    attempts: int = 0
    title: str = ""
    branch: str = ""
    pr_url: str = ""
    ci_status: str = ""
    last_error: str = ""
    result: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def payload_text(self) -> str:
        """payload 的规范化 JSON 文本，供提示词拼装与通知使用。"""
        return json.dumps(self.payload, ensure_ascii=False, sort_keys=True, indent=2)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    repo        TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'warning',
    title       TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    branch      TEXT NOT NULL DEFAULT '',
    pr_url      TEXT NOT NULL DEFAULT '',
    ci_status   TEXT NOT NULL DEFAULT '',
    last_error  TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(repo, fingerprint, status);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, updated_at);
CREATE TABLE IF NOT EXISTS job_events (
    seq    INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts     TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_job_events_job ON job_events(job_id, seq);
"""


def _row_to_job(row: sqlite3.Row) -> Job:
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {"raw": payload}
    return Job(
        id=row["id"],
        fingerprint=row["fingerprint"],
        repo=row["repo"],
        severity=row["severity"],
        payload=payload,
        status=row["status"],
        attempts=row["attempts"],
        title=row["title"],
        branch=row["branch"],
        pr_url=row["pr_url"],
        ci_status=row["ci_status"],
        last_error=row["last_error"],
        result=row["result"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class JobStore:
    """SQLite job 持久化 + 状态机流转。

    单连接 + ``check_same_thread=False``，所有访问经 ``asyncio.Lock`` 串行化，
    并在线程池中执行（不阻塞事件循环）。job 速率低（告警数量级），
    这一实现足够且避免了连接池的复杂度。
    """

    def __init__(self, db_path: str | Path, max_fix_attempts: int = DEFAULT_MAX_FIX_ATTEMPTS) -> None:
        self.db_path = Path(db_path)
        self.max_fix_attempts = max_fix_attempts
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # -- 生命周期 ---------------------------------------------------------

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_open)
        log.info("JobStore ready at %s", self.db_path)

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise JobStoreError("JobStore.connect() must be awaited before use")
        return self._conn

    # -- 写入 -------------------------------------------------------------

    async def create_job(
        self,
        fingerprint: str,
        repo: str,
        severity: str = "warning",
        title: str = "",
        payload: dict | None = None,
        status: str = "received",
    ) -> Job:
        if status not in TRANSITIONS:
            raise JobStoreError(f"unknown status: {status}")
        job = Job(
            id=f"job-{uuid.uuid4().hex[:12]}",
            fingerprint=fingerprint,
            repo=repo,
            severity=severity,
            payload=payload or {},
            status=status,
            attempts=1 if status == "fixing" else 0,
            title=title,
            created_at=utc_now(),
            updated_at=utc_now(),
        )

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO jobs (id, fingerprint, repo, severity, title, payload, status,"
                " attempts, branch, pr_url, ci_status, last_error, result, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.id, job.fingerprint, job.repo, job.severity, job.title,
                    json.dumps(job.payload, ensure_ascii=False), job.status, job.attempts,
                    job.branch, job.pr_url, job.ci_status, job.last_error, job.result,
                    job.created_at, job.updated_at,
                ),
            )
            conn.execute(
                "INSERT INTO job_events (job_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                (job.id, job.created_at, "created", f"status={job.status} repo={job.repo}"),
            )
            conn.commit()

        async with self._lock:
            await asyncio.to_thread(_insert, self._require_conn())
        log.info("job created id=%s repo=%s status=%s", job.id, repo, status)
        return job

    async def transition(
        self,
        job_id: str,
        to_state: str,
        reason: str = "",
        *,
        pr_url: str | None = None,
        branch: str | None = None,
        ci_status: str | None = None,
        last_error: str | None = None,
        result: str | None = None,
    ) -> Job:
        """校验并落库一次状态转移；非法转移抛 :class:`InvalidTransition`。

        ``RETRY_STATES → fixing`` 视为一次重试：attempts 递增，超过
        ``max_fix_attempts`` 时拒绝并抛 :class:`InvalidTransition`——
        调用方应改为 escalate。
        """
        if to_state not in TRANSITIONS:
            raise JobStoreError(f"unknown state: {to_state}")

        async with self._lock:
            conn = self._require_conn()

            def _do() -> Job:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise JobNotFound(job_id)
                current = _row_to_job(row)
                allowed = TRANSITIONS.get(current.status, frozenset())
                if to_state not in allowed:
                    raise InvalidTransition(
                        f"{current.status} -> {to_state} is not allowed for {job_id}"
                    )

                attempts = current.attempts
                if to_state == "fixing":
                    if current.status in RETRY_STATES:
                        if attempts >= self.max_fix_attempts:
                            raise InvalidTransition(
                                f"retry ceiling reached for {job_id}: attempts={attempts} "
                                f">= max_fix_attempts={self.max_fix_attempts}; escalate instead"
                            )
                        attempts += 1
                    else:
                        attempts = max(attempts, 1)

                now = utc_now()
                updated = Job(
                    id=current.id,
                    fingerprint=current.fingerprint,
                    repo=current.repo,
                    severity=current.severity,
                    payload=current.payload,
                    status=to_state,
                    attempts=attempts,
                    title=current.title,
                    branch=current.branch if branch is None else branch,
                    pr_url=current.pr_url if pr_url is None else pr_url,
                    ci_status=current.ci_status if ci_status is None else ci_status,
                    last_error=current.last_error if last_error is None else last_error,
                    result=current.result if result is None else result,
                    created_at=current.created_at,
                    updated_at=now,
                )
                conn.execute(
                    "UPDATE jobs SET status=?, attempts=?, branch=?, pr_url=?, ci_status=?,"
                    " last_error=?, result=?, updated_at=? WHERE id=?",
                    (
                        updated.status, updated.attempts, updated.branch, updated.pr_url,
                        updated.ci_status, updated.last_error, updated.result,
                        updated.updated_at, updated.id,
                    ),
                )
                detail = f"{current.status} -> {to_state}"
                if reason:
                    detail += f": {reason}"
                conn.execute(
                    "INSERT INTO job_events (job_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                    (job_id, now, "transition", detail),
                )
                conn.commit()
                return updated

            job = await asyncio.to_thread(_do)

        log.info("job %s: %s", job_id, reason or f"{job.status}")
        return job

    async def update(self, job_id: str, **fields: object) -> Job:
        """更新非状态字段（branch / pr_url / ci_status / last_error / result）。

        状态字段必须走 :meth:`transition`，这里显式拒绝 ``status``。
        """
        if "status" in fields:
            raise JobStoreError("use transition() to change status")
        allowed = {"branch", "pr_url", "ci_status", "last_error", "result", "title"}
        unknown = set(fields) - allowed
        if unknown:
            raise JobStoreError(f"unknown fields: {', '.join(sorted(unknown))}")
        if not fields:
            return await self.get_or_raise(job_id)

        async with self._lock:
            conn = self._require_conn()

            def _do() -> Job:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise JobNotFound(job_id)
                sets = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE jobs SET {sets}, updated_at=? WHERE id=?",
                    (*fields.values(), utc_now(), job_id),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return _row_to_job(row)

            return await asyncio.to_thread(_do)

    async def add_event(self, job_id: str, kind: str, detail: str = "") -> None:
        """追加一条审计事件（不改变状态）。"""
        async with self._lock:
            conn = self._require_conn()

            def _do() -> None:
                conn.execute(
                    "INSERT INTO job_events (job_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                    (job_id, utc_now(), kind, detail),
                )
                conn.commit()

            await asyncio.to_thread(_do)

    async def reset_for_recovery(self, job_id: str, reason: str = "") -> Job:
        """把中断在途的 job 回退到 ``received``（服务重启恢复专用）。

        正常状态机没有通向 ``received`` 的边，重启恢复是一次显式的运维操作，
        因此单独开一条通道：只允许从非终态回退，且落一条审计事件记录
        原状态，保证"每个状态变化都有据可查"。
        """
        async with self._lock:
            conn = self._require_conn()

            def _do() -> Job:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise JobNotFound(job_id)
                current = _row_to_job(row)
                if current.is_terminal:
                    raise InvalidTransition(
                        f"cannot recover {job_id}: status {current.status} is terminal"
                    )
                if current.status == "received":
                    return current
                now = utc_now()
                conn.execute(
                    "UPDATE jobs SET status='received', updated_at=? WHERE id=?", (now, job_id)
                )
                conn.execute(
                    "INSERT INTO job_events (job_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                    (
                        job_id, now, "recovery_reset",
                        f"{current.status} -> received" + (f": {reason}" if reason else ""),
                    ),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return _row_to_job(row)

            return await asyncio.to_thread(_do)

    # -- 读取 -------------------------------------------------------------

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            conn = self._require_conn()

            def _do() -> Job | None:
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                return _row_to_job(row) if row else None

            return await asyncio.to_thread(_do)

    async def get_or_raise(self, job_id: str) -> Job:
        job = await self.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    async def events(self, job_id: str) -> list[JobEvent]:
        async with self._lock:
            conn = self._require_conn()

            def _do() -> list[JobEvent]:
                rows = conn.execute(
                    "SELECT * FROM job_events WHERE job_id = ? ORDER BY seq", (job_id,)
                ).fetchall()
                return [
                    JobEvent(seq=r["seq"], job_id=r["job_id"], ts=r["ts"], kind=r["kind"], detail=r["detail"])
                    for r in rows
                ]

            return await asyncio.to_thread(_do)

    async def list_jobs(self, status: str | None = None, limit: int = 50) -> list[Job]:
        async with self._lock:
            conn = self._require_conn()

            def _do() -> list[Job]:
                if status is None:
                    rows = conn.execute(
                        "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                        (status, limit),
                    ).fetchall()
                return [_row_to_job(r) for r in rows]

            return await asyncio.to_thread(_do)

    async def count_by_status(self) -> dict[str, int]:
        """各状态的 job 数量（healthz 的观测口径）。"""
        async with self._lock:
            conn = self._require_conn()

            def _do() -> dict[str, int]:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
                ).fetchall()
                return {r["status"]: r["n"] for r in rows}

            return await asyncio.to_thread(_do)

    async def list_unfinished(self) -> list[Job]:
        """所有非终态 job（服务重启时的恢复输入）。"""
        async with self._lock:
            conn = self._require_conn()

            def _do() -> list[Job]:
                placeholders = ", ".join("?" for _ in TERMINAL_STATES)
                rows = conn.execute(
                    f"SELECT * FROM jobs WHERE status NOT IN ({placeholders})"
                    " ORDER BY created_at ASC",
                    tuple(sorted(TERMINAL_STATES)),
                ).fetchall()
                return [_row_to_job(r) for r in rows]

            return await asyncio.to_thread(_do)

    async def find_open_by_fingerprint(
        self, repo: str, fingerprint: str, window_seconds: int
    ) -> Job | None:
        """幂等去重：同一仓库 + 指纹、且仍在去重窗口内的未完结 job。

        用于"同一告警重复触发只更新已有 job"（架构文档第四节）。
        窗口按 ``updated_at`` 计算——已完结 job 不再拦截新告警。
        """
        if not fingerprint:
            return None
        cutoff = datetime.now(UTC).timestamp() - window_seconds

        async with self._lock:
            conn = self._require_conn()

            def _do() -> Job | None:
                placeholders = ", ".join("?" for _ in TERMINAL_STATES)
                rows = conn.execute(
                    f"SELECT * FROM jobs WHERE repo = ? AND fingerprint = ?"
                    f" AND status NOT IN ({placeholders}) ORDER BY created_at DESC",
                    (repo, fingerprint, *sorted(TERMINAL_STATES)),
                ).fetchall()
                for row in rows:
                    job = _row_to_job(row)
                    try:
                        ts = datetime.strptime(job.updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                            tzinfo=UTC
                        ).timestamp()
                    except ValueError:
                        return job
                    if ts >= cutoff:
                        return job
                return None

            return await asyncio.to_thread(_do)
