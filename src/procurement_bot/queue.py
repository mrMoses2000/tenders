from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    idempotency_key: str
    locked_by: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class ClaimedOutbox:
    id: UUID
    event_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    idempotency_key: str
    locked_by: str
    lease_expires_at: datetime
    approval_id: UUID | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True)
class IncomingAttachment:
    external_file_id: str
    kind: str
    external_file_unique_id: str = ""
    original_filename: str = ""
    mime_type: str = ""
    declared_size: int | None = None
    declared_duration_seconds: int | None = None


@dataclass(frozen=True)
class AcceptedIngress:
    user_id: UUID
    message_id: UUID
    attachment_id: UUID | None
    download_job_id: UUID | None
    acknowledgement_event_id: UUID | None


def stable_key(namespace: str, *parts: object) -> str:
    """Return a compact, reproducible idempotency key without leaking PII."""
    if not namespace or ":" in namespace:
        raise ValueError("namespace must be non-empty and may not contain ':'")
    canonical = json.dumps(
        [namespace, *parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


def bounded_backoff_seconds(
    attempt: int,
    *,
    base_seconds: float = 2.0,
    cap_seconds: float = 900.0,
    jitter_ratio: float = 0.2,
    random_value: float | None = None,
) -> float:
    """Exponential backoff with bounded positive jitter.

    ``random_value`` is injectable to keep tests deterministic.  A job's
    ``attempts`` value starts at one after its first claim.
    """
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    if base_seconds <= 0 or cap_seconds <= 0 or not 0 <= jitter_ratio <= 1:
        raise ValueError("invalid backoff parameters")
    sample = random.random() if random_value is None else random_value  # noqa: S311
    if not 0 <= sample <= 1:
        raise ValueError("random_value must be between 0 and 1")
    raw = base_seconds * (2 ** min(attempt - 1, 30))
    return min(cap_seconds, raw * (1 + jitter_ratio * sample))


async def accept_update(
    connection: asyncpg.Connection,
    update_id: int,
    raw_payload: dict[str, Any] | None = None,
) -> bool:
    return (
        await connection.fetchval(
            """
            INSERT INTO processed_updates(update_id,raw_payload)
            VALUES ($1,$2::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING update_id
            """,
            update_id,
            raw_payload or {},
        )
        is not None
    )


async def enqueue_job(
    connection: asyncpg.Connection,
    *,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str,
    max_attempts: int = 5,
) -> UUID | None:
    return await connection.fetchval(
        """
        INSERT INTO jobs(id,kind,payload,idempotency_key,max_attempts)
        VALUES ($1,$2,$3::jsonb,$4,$5)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        uuid4(),
        kind,
        payload,
        idempotency_key,
        max_attempts,
    )


async def enqueue_outbox(
    connection: asyncpg.Connection,
    *,
    event_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    max_attempts: int = 8,
    approval_id: UUID | None = None,
) -> UUID | None:
    return await connection.fetchval(
        """
        INSERT INTO outbox_events(
            id,event_type,payload,idempotency_key,max_attempts,approval_id
        )
        VALUES ($1,$2,$3::jsonb,$4,$5,$6)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        uuid4(),
        event_type,
        payload,
        idempotency_key,
        max_attempts,
        approval_id,
    )


async def claim_outbox(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    event_types: tuple[str, ...] | None = None,
) -> ClaimedOutbox | None:
    if not worker_id:
        raise ValueError("worker_id must be non-empty")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    if event_types is not None and not event_types:
        raise ValueError("event_types must be non-empty when provided")
    selected_types = list(event_types) if event_types is not None else None
    async with pool.acquire() as connection, connection.transaction():
        # A worker can die on its last permitted attempt. Do not leave that
        # expired lease in ``sending`` forever merely because it is no longer
        # eligible for another claim.
        await connection.execute(
            """
            UPDATE outbox_events
            SET status='dead',locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,
                last_error_code=CASE WHEN last_error_code=''
                    THEN 'lease_expired' ELSE last_error_code END
            WHERE status='sending' AND lease_expires_at<=now()
              AND attempts>=max_attempts
              AND ($1::text[] IS NULL OR event_type=ANY($1::text[]))
            """,
            selected_types,
        )
        row = await connection.fetchrow(
            """
            WITH candidate AS (
                SELECT id
                FROM outbox_events
                WHERE attempts < max_attempts
                  AND ($3::text[] IS NULL OR event_type=ANY($3::text[]))
                  AND (
                    (status IN ('pending','retry') AND available_at <= now())
                    OR (status='sending' AND lease_expires_at <= now())
                  )
                ORDER BY available_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE outbox_events AS event
            SET status='sending',attempts=event.attempts+1,locked_at=now(),
                locked_by=$1,
                lease_expires_at=now()+make_interval(secs => $2)
            FROM candidate
            WHERE event.id=candidate.id
            RETURNING event.*
            """,
            worker_id,
            lease_seconds,
            selected_types,
        )
    if row is None:
        return None
    return ClaimedOutbox(
        id=row["id"],
        event_type=row["event_type"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        idempotency_key=row["idempotency_key"],
        locked_by=row["locked_by"],
        lease_expires_at=row["lease_expires_at"],
        approval_id=row.get("approval_id"),
        provider_request_id=row.get("provider_request_id"),
    )


async def renew_outbox_lease(
    pool: asyncpg.Pool,
    event_id: UUID,
    worker_id: str,
    *,
    lease_seconds: int = 60,
) -> bool:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    result = await pool.execute(
        """
        UPDATE outbox_events
        SET lease_expires_at=now()+make_interval(secs => $3)
        WHERE id=$1 AND status='sending' AND locked_by=$2 AND lease_expires_at>now()
        """,
        event_id,
        worker_id,
        lease_seconds,
    )
    return result == "UPDATE 1"


async def set_outbox_provider_request_id(
    pool: asyncpg.Pool,
    event_id: UUID,
    worker_id: str,
    provider_request_id: str,
) -> bool:
    if not provider_request_id.strip():
        raise ValueError("provider_request_id must be non-empty")
    result = await pool.execute(
        """
        UPDATE outbox_events
        SET provider_request_id=$3
        WHERE id=$1 AND status='sending' AND locked_by=$2
          AND lease_expires_at>now() AND provider_request_id IS NULL
        """,
        event_id,
        worker_id,
        provider_request_id,
    )
    return result == "UPDATE 1"


async def complete_outbox(
    pool: asyncpg.Pool,
    event_id: UUID,
    worker_id: str,
    *,
    external_id: str | None = None,
) -> bool:
    result = await pool.execute(
        """
        UPDATE outbox_events
        SET status='sent',locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,
            external_id=$3,sent_at=now()
        WHERE id=$1 AND status='sending' AND locked_by=$2 AND lease_expires_at>now()
        """,
        event_id,
        worker_id,
        external_id,
    )
    return result == "UPDATE 1"


async def fail_outbox(
    pool: asyncpg.Pool,
    event: ClaimedOutbox,
    error_code: str,
    *,
    retryable: bool,
    now: datetime | None = None,
) -> bool:
    current_time = now or datetime.now(UTC)
    terminal = not retryable or event.attempts >= event.max_attempts
    available_at = current_time
    if not terminal:
        available_at += timedelta(seconds=bounded_backoff_seconds(event.attempts))
    result = await pool.execute(
        """
        UPDATE outbox_events
        SET status=$3,available_at=$4,locked_at=NULL,locked_by=NULL,
            lease_expires_at=NULL,last_error_code=$5
        WHERE id=$1 AND status='sending' AND locked_by=$2
        """,
        event.id,
        event.locked_by,
        "dead" if terminal else "retry",
        available_at,
        error_code[:500],
    )
    if result != "UPDATE 1":
        raise RuntimeError("outbox lease was lost before failure could be recorded")
    return terminal


async def claim_job(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    lease_seconds: int = 300,
) -> ClaimedJob | None:
    if not worker_id:
        raise ValueError("worker_id must be non-empty")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """
            UPDATE jobs
            SET status='dead',locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,
                last_error_code=CASE WHEN last_error_code=''
                    THEN 'lease_expired' ELSE last_error_code END,
                completed_at=now(),updated_at=now()
            WHERE status='running' AND lease_expires_at<=now()
              AND attempts>=max_attempts
            """
        )
        row = await connection.fetchrow(
            """
            WITH candidate AS (
                SELECT id
                FROM jobs
                WHERE attempts < max_attempts
                  AND (
                    (status IN ('pending','retry') AND available_at <= now())
                    OR (status='running' AND lease_expires_at <= now())
                  )
                ORDER BY available_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE jobs AS j
            SET status='running',
                attempts=j.attempts+1,
                locked_at=now(),
                locked_by=$1,
                lease_expires_at=now()+make_interval(secs => $2),
                updated_at=now()
            FROM candidate
            WHERE j.id=candidate.id
            RETURNING j.*
            """,
            worker_id,
            lease_seconds,
        )
    if row is None:
        return None
    return ClaimedJob(
        id=row["id"],
        kind=row["kind"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        idempotency_key=row["idempotency_key"],
        locked_by=row["locked_by"],
        lease_expires_at=row["lease_expires_at"],
    )


async def renew_job_lease(
    pool: asyncpg.Pool,
    job_id: UUID,
    worker_id: str,
    *,
    lease_seconds: int = 300,
) -> bool:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    result = await pool.execute(
        """
        UPDATE jobs
        SET lease_expires_at=now()+make_interval(secs => $3),updated_at=now()
        WHERE id=$1 AND status='running' AND locked_by=$2 AND lease_expires_at>now()
        """,
        job_id,
        worker_id,
        lease_seconds,
    )
    return result == "UPDATE 1"


async def complete_job(pool: asyncpg.Pool, job_id: UUID, worker_id: str) -> bool:
    result = await pool.execute(
        """
        UPDATE jobs
        SET status='succeeded',locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,
            completed_at=now(),updated_at=now()
        WHERE id=$1 AND status='running' AND locked_by=$2 AND lease_expires_at>now()
        """,
        job_id,
        worker_id,
    )
    return result == "UPDATE 1"


async def fail_job(
    pool: asyncpg.Pool,
    job: ClaimedJob,
    error_code: str,
    *,
    retryable: bool,
    now: datetime | None = None,
) -> bool:
    """Release a claimed job. Return True when it moved to the dead state."""
    current_time = now or datetime.now(UTC)
    terminal = not retryable or job.attempts >= job.max_attempts
    available_at = current_time
    if not terminal:
        available_at += timedelta(seconds=bounded_backoff_seconds(job.attempts))
    result = await pool.execute(
        """
        UPDATE jobs
        SET status=$3,available_at=$4,locked_at=NULL,locked_by=NULL,
            lease_expires_at=NULL,last_error_code=$5,updated_at=now(),
            completed_at=CASE WHEN $3='dead' THEN now() ELSE NULL END
        WHERE id=$1 AND status='running' AND locked_by=$2
        """,
        job.id,
        job.locked_by,
        "dead" if terminal else "retry",
        available_at,
        error_code[:500],
    )
    if result != "UPDATE 1":
        raise RuntimeError("job lease was lost before failure could be recorded")
    return terminal


async def persist_telegram_message(
    pool: asyncpg.Pool,
    *,
    update_id: int,
    telegram_user_id: int,
    chat_id: int,
    telegram_message_id: int,
    message_kind: str,
    raw_update: dict[str, Any],
    username: str = "",
    display_name: str = "",
    language_code: str = "ru",
    text_content: str = "",
    received_at: datetime | None = None,
    attachment: IncomingAttachment | None = None,
    acknowledgement_text: str | None = None,
) -> AcceptedIngress | None:
    """Persist the complete Telegram ingress boundary in one transaction.

    A committed update always has its message, optional attachment and download
    job, and optional acknowledgement outbox event.  A duplicate update returns
    ``None`` and creates no additional work.
    """
    async with pool.acquire() as connection, connection.transaction():
        if not await accept_update(connection, update_id, raw_update):
            return None

        user_id = await connection.fetchval(
            """
            INSERT INTO users(telegram_id,username,display_name,language_code)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (telegram_id) DO UPDATE
            SET username=CASE WHEN EXCLUDED.username<>''
                    THEN EXCLUDED.username ELSE users.username END,
                display_name=CASE WHEN EXCLUDED.display_name<>''
                    THEN EXCLUDED.display_name ELSE users.display_name END,
                language_code=EXCLUDED.language_code,
                updated_at=now()
            RETURNING id
            """,
            telegram_user_id,
            username,
            display_name,
            language_code,
        )
        message_id = await connection.fetchval(
            """
            INSERT INTO messages(
                user_id,channel,direction,external_chat_id,external_message_id,
                message_kind,text_content,raw_payload,received_at
            )
            VALUES ($1,'telegram','inbound',$2,$3,$4,$5,$6::jsonb,COALESCE($7,now()))
            ON CONFLICT (channel,external_chat_id,external_message_id) DO UPDATE
            SET raw_payload=messages.raw_payload
            RETURNING id
            """,
            user_id,
            str(chat_id),
            str(telegram_message_id),
            message_kind,
            text_content,
            raw_update,
            received_at,
        )

        attachment_id: UUID | None = None
        download_job_id: UUID | None = None
        if attachment is not None:
            attachment_id = await connection.fetchval(
                """
                INSERT INTO incoming_attachments(
                    message_id,external_file_id,external_file_unique_id,kind,
                    original_filename,mime_type,declared_size,declared_duration_seconds
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (message_id,external_file_id) DO UPDATE
                SET external_file_unique_id=incoming_attachments.external_file_unique_id
                RETURNING id
                """,
                message_id,
                attachment.external_file_id,
                attachment.external_file_unique_id,
                attachment.kind,
                attachment.original_filename,
                attachment.mime_type,
                attachment.declared_size,
                attachment.declared_duration_seconds,
            )
            file_identity = attachment.external_file_unique_id or attachment.external_file_id
            download_job_id = await enqueue_job(
                connection,
                kind="download_attachment",
                payload={"attachment_id": str(attachment_id), "message_id": str(message_id)},
                idempotency_key=stable_key(
                    "attachment.download", chat_id, telegram_message_id, file_identity
                ),
            )

        acknowledgement_event_id: UUID | None = None
        if acknowledgement_text is not None:
            acknowledgement_event_id = await enqueue_outbox(
                connection,
                event_type="telegram.send_message",
                payload={"chat_id": chat_id, "text": acknowledgement_text},
                idempotency_key=stable_key("telegram.ack", update_id),
            )

        await connection.execute(
            """
            UPDATE processed_updates
            SET status='completed',completed_at=now(),error_code=''
            WHERE update_id=$1
            """,
            update_id,
        )
        return AcceptedIngress(
            user_id=user_id,
            message_id=message_id,
            attachment_id=attachment_id,
            download_job_id=download_job_id,
            acknowledgement_event_id=acknowledgement_event_id,
        )
