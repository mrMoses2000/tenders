from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1

TASK_TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {"awaiting_approval", "completed", "failed", "cancelled"},
    "awaiting_approval": {"running", "completed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StateStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def migrate(self) -> None:
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_until TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_ready_idx
                    ON jobs(status, available_at, lease_until);
                CREATE TABLE IF NOT EXISTS facts (
                    fact_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    line_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    checksum TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS snapshots_request_idx
                    ON snapshots(request_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS source_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    correlation_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    approved_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS side_effects (
                    key TEXT PRIMARY KEY,
                    purpose TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS performance_events (
                    event_id TEXT PRIMARY KEY,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )
            current = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if current is None:
                connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif current["version"] != SCHEMA_VERSION:
                raise RuntimeError(f"unsupported database version: {current['version']}")

    def create_task(
        self, task_id: str, request_id: str, correlation_id: str, payload: dict[str, Any]
    ) -> None:
        now = utc_now().isoformat()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, 'pending', ?, ?, ?, ?)",
                (task_id, request_id, correlation_id, canonical_json(payload), now, now),
            )

    def enqueue_job(
        self, job_id: str, task_id: str, kind: str, payload: dict[str, Any],
        max_attempts: int = 3, available_at: datetime | None = None,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO jobs(
                    job_id, task_id, kind, status, payload_json, attempts, max_attempts,
                    available_at, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, 0, ?, ?, ?, ?)""",
                (
                    job_id, task_id, kind, canonical_json(payload), max_attempts,
                    (available_at or now).isoformat(), now.isoformat(), now.isoformat(),
                ),
            )

    def transition_task(self, task_id: str, status: str) -> None:
        if status not in TASK_TRANSITIONS:
            raise ValueError(f"unknown task status: {status}")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = row["status"]
            if current == status:
                return
            if status not in TASK_TRANSITIONS[current]:
                raise ValueError(f"invalid task transition: {current} -> {status}")
            connection.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
                (status, utc_now().isoformat(), task_id),
            )

    def task_status(self, task_id: str) -> str:
        row = self.connection.execute(
            "SELECT status FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return str(row["status"])

    def task_has_unfinished_jobs(self, task_id: str) -> bool:
        row = self.connection.execute(
            """SELECT COUNT(*) AS count FROM jobs WHERE task_id=?
               AND status NOT IN ('completed', 'dead_letter')""",
            (task_id,),
        ).fetchone()
        return bool(row["count"])

    def acquire_job(self, worker_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        now = utc_now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT jobs.*, tasks.correlation_id, tasks.request_id FROM jobs
                   JOIN tasks ON tasks.task_id = jobs.task_id
                   WHERE (
                       (jobs.status IN ('queued', 'retry') AND jobs.available_at <= ?)
                       OR (jobs.status = 'running' AND jobs.lease_until < ?)
                   )
                   ORDER BY jobs.available_at, jobs.created_at LIMIT 1""",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """UPDATE jobs SET status='running', lease_owner=?, lease_until=?,
                   attempts=attempts+1, updated_at=?
                   WHERE job_id=? AND (
                       status IN ('queued', 'retry') OR (status='running' AND lease_until < ?)
                   )""",
                (
                    worker_id, lease_until.isoformat(), now.isoformat(), row["job_id"],
                    now.isoformat(),
                ),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                """SELECT jobs.*, tasks.correlation_id, tasks.request_id FROM jobs
                   JOIN tasks ON tasks.task_id = jobs.task_id WHERE jobs.job_id=?""",
                (row["job_id"],),
            ).fetchone()
        result = dict(claimed)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def complete_job(self, job_id: str, worker_id: str) -> None:
        now = utc_now().isoformat()
        with self.transaction() as connection:
            updated = connection.execute(
                """UPDATE jobs SET status='completed', lease_owner=NULL, lease_until=NULL,
                   updated_at=? WHERE job_id=? AND status='running' AND lease_owner=?""",
                (now, job_id, worker_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("job is not leased by this worker")

    def fail_job(self, job_id: str, worker_id: str, error: str, backoff_seconds: int = 0) -> str:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE job_id=? AND lease_owner=?",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("job is not leased by this worker")
            status = "dead_letter" if row["attempts"] >= row["max_attempts"] else "retry"
            connection.execute(
                """UPDATE jobs SET status=?, available_at=?, lease_owner=NULL, lease_until=NULL,
                   last_error=?, updated_at=? WHERE job_id=?""",
                (
                    status, (now + timedelta(seconds=backoff_seconds)).isoformat(),
                    error[:2000], now.isoformat(), job_id,
                ),
            )
        return status

    def append_fact(self, request_id: str, line_id: str, fact_id: str, payload: dict[str, Any]) -> bool:
        serialized = canonical_json(payload)
        digest = sha256(serialized.encode()).hexdigest()
        existing = self.connection.execute(
            "SELECT payload_hash FROM facts WHERE fact_id=?", (fact_id,)
        ).fetchone()
        if existing:
            if existing["payload_hash"] != digest:
                raise ValueError("fact_id already exists with different payload")
            return False
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?)",
                (fact_id, request_id, line_id, serialized, digest, utc_now().isoformat()),
            )
        return True

    def save_artifact(self, artifact_id: str, request_id: str, payload: dict[str, Any]) -> bool:
        serialized = canonical_json(payload)
        digest = sha256(serialized.encode()).hexdigest()
        existing = self.connection.execute(
            "SELECT payload_hash FROM source_artifacts WHERE artifact_id=?", (artifact_id,)
        ).fetchone()
        if existing:
            if existing["payload_hash"] != digest:
                raise ValueError("artifact_id already exists with different metadata")
            return False
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO source_artifacts VALUES (?, ?, ?, ?, ?)",
                (artifact_id, request_id, serialized, digest, utc_now().isoformat()),
            )
        return True

    def save_snapshot(self, request_id: str, checksum: str, payload: dict[str, Any]) -> bool:
        serialized = canonical_json(payload)
        with self.transaction() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?, ?)",
                (checksum, request_id, serialized, utc_now().isoformat()),
            )
        return result.rowcount == 1

    def latest_snapshot(self, request_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload_json FROM snapshots WHERE request_id=? ORDER BY created_at DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def audit(
        self, correlation_id: str, event_type: str, entity_id: str, details: dict[str, Any]
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_events(correlation_id,event_type,entity_id,details_json,created_at) VALUES (?,?,?,?,?)",
                (correlation_id, event_type, entity_id, canonical_json(details), utc_now().isoformat()),
            )

    def audit_events(self, correlation_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            "SELECT * FROM audit_events WHERE correlation_id=? ORDER BY event_id",
            (correlation_id,),
        )
        return tuple(
            {**dict(row), "details": json.loads(row["details_json"])} for row in rows
        )

    def record_performance_event(
        self, event_id: str, subject_type: str, subject_id: str,
        payload: dict[str, Any], occurred_at: datetime,
    ) -> bool:
        with self.transaction() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO performance_events VALUES (?, ?, ?, ?, ?)",
                (event_id, subject_type, subject_id, canonical_json(payload), occurred_at.isoformat()),
            )
        return result.rowcount == 1

    def performance_history(self, subject_type: str, subject_id: str) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """SELECT payload_json, occurred_at FROM performance_events
               WHERE subject_type=? AND subject_id=? ORDER BY occurred_at""",
            (subject_type, subject_id),
        )
        return tuple(
            {**json.loads(row["payload_json"]), "occurred_at": row["occurred_at"]}
            for row in rows
        )

    def approve(self, approval_id: str, purpose: str, payload_hash: str, approved_by: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, NULL)",
                (approval_id, purpose, payload_hash, approved_by, utc_now().isoformat()),
            )

    def consume_approval(self, approval_id: str, purpose: str, payload_hash: str) -> bool:
        now = utc_now().isoformat()
        with self.transaction() as connection:
            updated = connection.execute(
                """UPDATE approvals SET consumed_at=? WHERE approval_id=? AND purpose=?
                   AND payload_hash=? AND consumed_at IS NULL""",
                (now, approval_id, purpose, payload_hash),
            )
        return updated.rowcount == 1

    def idempotent_result(self, key: str, payload_hash: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload_hash,result_json FROM idempotency_keys WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise ValueError("idempotency key reused for different payload")
        return json.loads(row["result_json"])

    def save_idempotent_result(
        self, key: str, payload_hash: str, result: dict[str, Any]
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?)",
                (key, payload_hash, canonical_json(result), utc_now().isoformat()),
            )

    def begin_side_effect(self, key: str, purpose: str, payload_hash: str) -> str:
        row = self.connection.execute(
            "SELECT purpose,payload_hash,status FROM side_effects WHERE key=?", (key,)
        ).fetchone()
        if row:
            if row["purpose"] != purpose or row["payload_hash"] != payload_hash:
                raise ValueError("side-effect key reused for different payload")
            return str(row["status"])
        now = utc_now().isoformat()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO side_effects VALUES (?, ?, ?, 'pending', NULL, ?, ?)",
                (key, purpose, payload_hash, now, now),
            )
        return "created"

    def complete_side_effect(self, key: str, payload_hash: str, result: dict[str, Any]) -> None:
        with self.transaction() as connection:
            updated = connection.execute(
                """UPDATE side_effects SET status='completed', result_json=?, updated_at=?
                   WHERE key=? AND payload_hash=? AND status='pending'""",
                (canonical_json(result), utc_now().isoformat(), key, payload_hash),
            )
            if updated.rowcount != 1:
                raise RuntimeError("side effect is not pending for this payload")

    def side_effect_result(self, key: str, payload_hash: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload_hash,status,result_json FROM side_effects WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise ValueError("side-effect key reused for different payload")
        if row["status"] != "completed":
            raise RuntimeError("side-effect outcome is uncertain; reconcile it manually")
        return json.loads(row["result_json"])

    def job_counts(self) -> dict[str, int]:
        return {
            row["status"]: row["count"]
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"
            )
        }
