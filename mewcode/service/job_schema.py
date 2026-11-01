"""SQLite schema and additive migrations for service persistence."""
from __future__ import annotations

import sqlite3

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
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
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

def initialize(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for column in ("tokens_in", "tokens_out"):
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
    events = {r[1] for r in conn.execute("PRAGMA table_info(job_events)")}
    if "execution_id" not in events:
        conn.execute("ALTER TABLE job_events ADD COLUMN execution_id TEXT NOT NULL DEFAULT ''")
    conn.executescript(_JOURNAL_SCHEMA)
    conn.commit()


_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_policies (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_executions (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
    origin TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT NOT NULL DEFAULT '',
    start_seq INTEGER NOT NULL, start_state TEXT NOT NULL, end_state TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT 'running',
    tokens_in_start INTEGER NOT NULL, tokens_out_start INTEGER NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_execution ON job_executions(job_id) WHERE ended_at = '';
CREATE TABLE IF NOT EXISTS notification_outbox (
    event_seq INTEGER PRIMARY KEY REFERENCES job_events(seq),
    job_id TEXT NOT NULL REFERENCES jobs(id), phase TEXT NOT NULL,
    snapshot TEXT NOT NULL, detail TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, sent_at TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notification_due ON notification_outbox(status, due, event_seq);
CREATE INDEX IF NOT EXISTS idx_notification_job ON notification_outbox(job_id, event_seq);
"""
