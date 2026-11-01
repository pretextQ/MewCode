"""Durable notifications, effective policy and executions sharing JobStore transactions.

No network or configuration-file access belongs here. Notifications describe an
event, not the job's later state. One service process dispatches a database.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .jobs import Job, JobStore

NOTIFICATION_STATES = {
    "received": "received", "pr_opened": "pr_opened",
    "human_review": "human_review", "escalate": "escalated",
}


def active_execution(conn: sqlite3.Connection, job_id: str) -> str:
    row = conn.execute(
        "SELECT id FROM job_executions WHERE job_id=? AND ended_at=''", (job_id,),
    ).fetchone()
    return str(row[0]) if row else ""


def save_policy(conn: sqlite3.Connection, job_id: str, snapshot: dict | None) -> None:
    if snapshot is not None:
        conn.execute(
            "INSERT OR IGNORE INTO job_policies VALUES (?, ?)",
            (job_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True)),
        )


def enqueue_notification(conn: sqlite3.Connection, job: Job, seq: int, detail: str = "") -> None:
    if job.status in {"merged", "changes_requested", "escalate"}:
        conn.execute(
            "UPDATE notification_outbox SET status='superseded',error='review is no longer waiting' "
            "WHERE job_id=? AND phase='human_review' AND status IN ('pending','retry','blocked')", (job.id,),
        )
    phase = NOTIFICATION_STATES.get(job.status)
    if phase is None:
        return
    snapshot = asdict(job)
    # Preserve only the payload field used for the title; do not copy raw alerts.
    snapshot["payload"] = {"alertname": job.payload.get("alertname", "")}
    snapshot["result"] = ""
    policy = conn.execute("SELECT snapshot FROM job_policies WHERE job_id=?", (job.id,)).fetchone()
    route = json.loads(policy[0]).get("notify_route", "") if policy else ""
    # Capture an escalation digest at the event's position, before any retry.
    if phase == "escalated":
        rows = conn.execute(
            "SELECT kind, detail FROM job_events WHERE job_id=? AND seq<=? AND kind IN "
            "('transition','agent_finished','baseline_tests','verify_tests','test_delta','ci_status',"
            "'verification_retry','escalated') ORDER BY seq DESC LIMIT 6", (job.id, seq),
        ).fetchall()
        detail += "\n已尝试的分析：\n" + "\n".join(f"- [{r[0]}] {r[1][:180]}" for r in reversed(rows))
    conn.execute(
        "INSERT INTO notification_outbox (event_seq,job_id,phase,snapshot,detail,route,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (seq, job.id, phase, json.dumps(snapshot, ensure_ascii=False), detail[:6000], route, job.updated_at),
    )


class ServiceJournal:
    def __init__(self, store: JobStore) -> None:
        self.store = store

    async def policy(self, job_id: str) -> dict:
        def read(conn: sqlite3.Connection) -> dict:
            row = conn.execute("SELECT snapshot FROM job_policies WHERE job_id=?", (job_id,)).fetchone()
            return json.loads(row[0]) if row else {}
        return await self.store.transact(read)

    async def apply_policy(self, job: Job, snapshot: dict) -> dict:
        """Freeze branch choices and original budget; retain current checks in audit."""
        from .jobs import utc_now
        from .policy import PolicyError

        def apply(conn: sqlite3.Connection) -> dict:
            row = conn.execute("SELECT snapshot FROM job_policies WHERE job_id=?", (job.id,)).fetchone()
            original = json.loads(row[0]) if row else snapshot
            for key in ("repo_path", "worktree_base", "pr_base"):
                if original[key] != snapshot[key]:
                    raise PolicyError(f"{key} changed for existing job; manual handling required")
            save_policy(conn, job.id, snapshot)
            conn.execute(
                "INSERT INTO job_events (job_id,ts,kind,detail,execution_id) VALUES (?,?,?,?,?)",
                (job.id, utc_now(), "policy_revalidated" if row else "policy_snapshot",
                 json.dumps(snapshot, ensure_ascii=False, sort_keys=True), active_execution(conn, job.id)),
            )
            return original
        return await self.store.transact(apply)

    async def start_execution(self, job: Job) -> str:
        from .jobs import utc_now

        def begin(conn: sqlite3.Connection) -> str:
            prior = conn.execute(
                "SELECT start_seq FROM job_executions WHERE job_id=? ORDER BY rowid DESC LIMIT 1", (job.id,),
            ).fetchone()
            recovered = conn.execute(
                "SELECT detail FROM job_events WHERE job_id=? AND kind='recovered' AND seq>? "
                "ORDER BY seq DESC LIMIT 1", (job.id, prior[0] if prior else 0),
            ).fetchone()
            origin = "manual_rework" if job.status == "changes_requested" else "initial"
            if recovered:
                origin = "handler_retry" if "handler-level" in recovered[0] else "recovery"
            seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM job_events").fetchone()[0]
            execution_id = f"exec-{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO job_executions "
                "(id,job_id,origin,started_at,start_seq,start_state,tokens_in_start,tokens_out_start) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (execution_id, job.id, origin, utc_now(), seq, job.status, job.tokens_in, job.tokens_out),
            )
            return execution_id
        return await self.store.transact(begin)

    async def finish_execution(self, execution_id: str, outcome: str) -> None:
        from .jobs import utc_now

        def finish(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE job_executions SET ended_at=?,outcome=?,"
                "end_state=(SELECT status FROM jobs WHERE id=job_id),"
                "tokens_in=MAX(0,(SELECT tokens_in FROM jobs WHERE id=job_id)-tokens_in_start),"
                "tokens_out=MAX(0,(SELECT tokens_out FROM jobs WHERE id=job_id)-tokens_out_start) "
                "WHERE id=? AND ended_at=''", (utc_now(), outcome, execution_id),
            )
        await self.store.transact(finish)

    async def recover_execution(self, job_id: str) -> None:
        execution_id = await self.store.transact(lambda conn: active_execution(conn, job_id))
        if execution_id:
            await self.finish_execution(execution_id, "interrupted")

    async def recover_executions(self) -> None:
        """Also close abandoned records of jobs already waiting for a person."""
        ids = await self.store.transact(lambda conn: [r[0] for r in conn.execute(
            "SELECT id FROM job_executions WHERE ended_at=''",
        )])
        for execution_id in ids:
            await self.finish_execution(execution_id, "interrupted")

    async def executions(self, job_id: str) -> list[dict]:
        return await self.store.transact(lambda conn: [dict(row) for row in conn.execute(
            "SELECT * FROM job_executions WHERE job_id=? ORDER BY rowid", (job_id,),
        )])

    async def notifications(self, job_id: str) -> list[dict]:
        # Snapshots and route fingerprints remain internal; expose operational facts.
        return await self.store.transact(lambda conn: [dict(row) for row in conn.execute(
            "SELECT event_seq,phase,status,attempts,due,error,created_at,sent_at "
            "FROM notification_outbox WHERE job_id=? ORDER BY event_seq", (job_id,),
        )])

    async def notification_stats(self) -> dict[str, Any]:
        def read(conn: sqlite3.Connection) -> dict[str, Any]:
            counts = {r[0]: r[1] for r in conn.execute(
                "SELECT status,COUNT(*) FROM notification_outbox GROUP BY status",
            )}
            oldest = conn.execute(
                "SELECT MIN(created_at) FROM notification_outbox WHERE status IN ('pending','retry','blocked')",
            ).fetchone()[0]
            return {"counts": counts, "oldest_pending_at": oldest}
        return await self.store.transact(read)

    async def ready_notifications(self, now: float | None = None) -> list[dict]:
        """Per-job order, with a blocked destination never stalling other jobs."""
        return await self.store.transact(lambda conn: [dict(row) for row in conn.execute(
            "SELECT n.* FROM notification_outbox n WHERE n.status IN ('pending','retry','blocked') "
            "AND n.due<=? AND NOT EXISTS (SELECT 1 FROM notification_outbox older "
            "WHERE older.job_id=n.job_id AND older.event_seq<n.event_seq "
            "AND older.status IN ('pending','retry','blocked')) ORDER BY n.event_seq LIMIT 32",
            (time.time() if now is None else now,),
        )])

    async def pin_route(self, row: dict, route: str) -> str | None:
        def pin(conn: sqlite3.Connection) -> str | None:
            current = conn.execute(
                "SELECT route,status,generation FROM notification_outbox WHERE event_seq=?", (row["event_seq"],),
            ).fetchone()
            if (current is None or current["generation"] != row["generation"]
                    or current["status"] not in {"pending", "retry", "blocked"}):
                return None
            conn.execute(
                "UPDATE notification_outbox SET route=? WHERE event_seq=? AND route='' AND generation=?",
                (route, row["event_seq"], row["generation"]),
            )
            return str(current["route"] or route)
        return await self.store.transact(pin)

    async def settle_notification(
        self, row: dict, status: str, *, error: str = "", delay: float = 0,
    ) -> None:
        from .jobs import utc_now

        await self.store.transact(lambda conn: conn.execute(
            "UPDATE notification_outbox SET status=?,attempts=attempts+1,error=?,due=?,sent_at=? "
            "WHERE event_seq=? AND generation=?",
            (status, error[:500], time.time() + delay, utc_now() if status == "sent" else "",
             row["event_seq"], row["generation"]),
        ).rowcount)

    async def retry_notification(self, job_id: str, seq: int, accept_route_change: bool = False) -> bool:
        from .jobs import utc_now

        def retry(conn: sqlite3.Connection) -> bool:
            changed = bool(conn.execute(
                "UPDATE notification_outbox SET status='pending',due=0,error='',generation=generation+1,"
                "route=CASE WHEN ? THEN '' ELSE route END WHERE job_id=? AND event_seq=? "
                "AND status IN ('retry','blocked')", (accept_route_change, job_id, seq),
            ).rowcount)
            if changed:
                conn.execute(
                    "INSERT INTO job_events (job_id,ts,kind,detail) VALUES (?,?,?,?)",
                    (job_id, utc_now(), "notification_retry_requested",
                     f"event_seq={seq} accept_route_change={accept_route_change}"),
                )
            return changed
        return await self.store.transact(retry)
