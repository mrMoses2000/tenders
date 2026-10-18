from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from procurement_bot.errors import RetryableProviderError
from procurement_bot.queue import enqueue_job, stable_key

QUOTA_SLEEP_SECONDS = 1800
RESEARCH_SCHEDULER_LOCK_KEY = 731940251


class ProviderQuotaDeferred(RetryableProviderError):
    def __init__(self, available_at: datetime) -> None:
        self.available_at = available_at
        self.reason = "provider_quota_sleep"
        super().__init__(self.reason)


def is_quota_failure(reason: str) -> bool:
    return any(marker in reason for marker in ("cli_usage_limit", "cli_rate_limit"))


async def autonomy_enabled(connection: asyncpg.Connection, run_id: UUID) -> bool:
    return bool(
        await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM case_pipeline_runtime p
        JOIN procurement_cases c ON c.id=p.case_id
        WHERE p.research_run_id=$1 AND p.enabled AND NOT p.paused
          AND c.status NOT IN ('closed','cancelled','selected'))""",
            run_id,
        )
    )


async def before_provider_call(connection: asyncpg.Connection, run_id: UUID) -> None:
    if not await autonomy_enabled(connection, run_id):
        return
    wait = await connection.fetchrow(
        "SELECT next_probe_at FROM research_provider_waits WHERE research_run_id=$1 FOR UPDATE",
        run_id,
    )
    if wait is None:
        return
    now = datetime.now(UTC)
    if wait["next_probe_at"] > now:
        raise ProviderQuotaDeferred(wait["next_probe_at"])
    # A single bounded real page is the probe. Other workers stay asleep until
    # it succeeds, denies quota again or its 30-minute reservation expires.
    await connection.execute(
        """UPDATE research_provider_waits SET next_probe_at=now()+interval '30 minutes',
        probe_count=probe_count+1,updated_at=now() WHERE research_run_id=$1""",
        run_id,
    )


async def sleep_for_quota(
    connection: asyncpg.Connection,
    *,
    run_id: UUID,
    task_id: UUID,
    job_id: UUID,
    worker_id: str,
    generation: int,
) -> datetime | None:
    if not await autonomy_enabled(connection, run_id):
        return None
    changed = await connection.execute(
        """UPDATE research_branch_checkpoints cp SET status='queued',owner_worker=NULL,
        lease_until=NULL,batch_attempts=GREATEST(recovery_base_attempts,batch_attempts-1),
        quota_waits=quota_waits+1,last_error_code='provider_quota_sleep',updated_at=now()
        FROM jobs j WHERE cp.id=$1 AND cp.current_job_id=$2 AND cp.owner_generation=$4
          AND cp.owner_worker=$3 AND j.id=$2 AND j.status='running'
          AND j.locked_by=$3 AND j.lease_expires_at>now()""",
        task_id,
        job_id,
        worker_id,
        generation,
    )
    if changed != "UPDATE 1":
        raise RuntimeError("quota sleep lost checkpoint ownership")
    due = datetime.now(UTC) + timedelta(seconds=QUOTA_SLEEP_SECONDS)
    await connection.execute(
        """INSERT INTO research_provider_waits(research_run_id,next_probe_at,reason)
        VALUES($1,$2,'quota') ON CONFLICT(research_run_id) DO UPDATE
        SET next_probe_at=EXCLUDED.next_probe_at,reason='quota',updated_at=now()""",
        run_id,
        due,
    )
    await connection.execute(
        """UPDATE case_pipeline_runtime SET next_tick_at=LEAST(next_tick_at,$2),
        wake_reason='provider_quota_sleep',updated_at=now() WHERE research_run_id=$1""",
        run_id,
        due,
    )
    return due


async def provider_succeeded(connection: asyncpg.Connection, run_id: UUID) -> None:
    deleted = await connection.execute(
        "DELETE FROM research_provider_waits WHERE research_run_id=$1",
        run_id,
    )
    if deleted == "DELETE 1":
        await connection.execute(
            """UPDATE jobs SET available_at=now() WHERE kind='parallel_research_task'
            AND payload->>'research_run_id'=$1 AND status='pending'
            AND last_error_code='provider_quota_sleep' """,
            str(run_id),
        )


async def wake_pipeline(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_id: UUID,
    reason: str,
) -> bool:
    if not reason.strip() or len(reason) > 200:
        raise ValueError("invalid wake reason")
    async with connection.transaction():
        # Recovery and owner wake both change checkpoint generations and group
        # admission. Take the research scheduler lock before any row locks so
        # those paths cannot deadlock in opposite order.
        await connection.execute("SELECT pg_advisory_xact_lock($1)", RESEARCH_SCHEDULER_LOCK_KEY)
        row = await connection.fetchrow(
            """SELECT p.research_run_id FROM case_pipeline_runtime p
            JOIN procurement_cases c ON c.id=p.case_id JOIN users u ON u.id=c.owner_user_id
            WHERE p.case_id=$1 AND c.owner_user_id=$2 AND u.active AND p.enabled
              AND c.status NOT IN ('closed','cancelled','selected') FOR UPDATE OF p""",
            case_id,
            owner_id,
        )
        if row is None:
            return False
        # Explicit owner wake can resume an owner pause, retaining historical
        # attempts and markers. Scheduler admission performs the actual enqueue.
        await connection.execute(
            "UPDATE research_branch_checkpoints SET status='queued',"
            "owner_generation=owner_generation+1,owner_worker=NULL,lease_until=NULL,"
            "current_job_id=NULL,updated_at=now() WHERE research_run_id=$1 AND status='paused'",
            row["research_run_id"],
        )
        await connection.execute(
            "UPDATE research_item_groups SET status='queued',updated_at=now() "
            "WHERE research_run_id=$1 AND status='paused'",
            row["research_run_id"],
        )
        await connection.execute(
            "UPDATE research_lanes SET status='queued',updated_at=now() "
            "WHERE group_id IN (SELECT id FROM research_item_groups WHERE research_run_id=$1) "
            "AND status='paused'",
            row["research_run_id"],
        )
        await connection.execute(
            """UPDATE case_pipeline_runtime SET next_tick_at=now(),paused=false,
            wake_reason=$2,updated_at=now() WHERE case_id=$1""",
            case_id,
            reason,
        )
        await connection.execute(
            """UPDATE research_provider_waits SET next_probe_at=now(),updated_at=now()
            WHERE research_run_id=$1""",
            row["research_run_id"],
        )
        await connection.execute(
            """UPDATE jobs SET available_at=now() WHERE kind='parallel_research_task'
            AND payload->>'research_run_id'=$1 AND status='pending'
            AND last_error_code='provider_quota_sleep' """,
            str(row["research_run_id"]),
        )
        # Owner continuation can probe a sleeping reply interpreter too. Keep
        # the original message/job and restrict this to the owned case's chats.
        await connection.execute(
            """UPDATE jobs AS j SET available_at=now()
            FROM supplier_conversations AS c
            WHERE j.kind='process_supplier_reply' AND j.status='pending'
              AND j.last_error_code='provider_quota_sleep'
              AND j.payload->>'conversation_id'=c.id::text AND c.case_id=$1""",
            case_id,
        )
        # An explicit owner wake can also retry known-accepted sends whose
        # provider/session checks were deferred. Preserve their status,
        # attempts and reserved provider id so outbox recovery continues through
        # its normal journal/reconciliation and send-policy checks.
        await connection.execute(
            """UPDATE outbox_events AS e SET available_at=now()
            FROM supplier_conversations AS c
            WHERE e.event_type='whatsapp.send_text' AND e.status='retry'
              AND e.last_error_code IN ('provider_acceptance_unknown','waha_session_unavailable')
              AND c.case_id=$1 AND c.id=CASE
                WHEN e.payload->>'conversation_id' ~*
                     '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                  THEN (e.payload->>'conversation_id')::uuid
                ELSE NULL
              END""",
            case_id,
        )
        from procurement_bot.whatsapp_account import request_account_probe

        sessions = await connection.fetch(
            "SELECT DISTINCT session_name FROM supplier_conversations WHERE case_id=$1", case_id)
        for session in sessions:
            await request_account_probe(connection, session["session_name"], reason="owner-wake")
        from procurement_bot.outreach_schedule import coalesce_legacy_outreach, request_outreach

        await coalesce_legacy_outreach(connection, case_id)
        job_id = await request_outreach(
            connection, case_id=case_id, source_key=stable_key("owner-wake", reason, case_id,
                datetime.now(UTC).isoformat()),
        )
        if job_id is not None:
            await connection.execute(
                "UPDATE jobs SET available_at=now() WHERE id=$1 AND status IN ('pending','retry')",
                job_id,
            )
        return True


async def wake_session_pipelines(
    connection: asyncpg.Connection,
    *,
    session_name: str,
    reason: str,
) -> int:
    """Wake enabled, unpaused active cases linked to one WhatsApp session."""

    if not session_name.strip() or len(session_name) > 200:
        raise ValueError("invalid session name")
    if not reason.strip() or len(reason) > 200:
        raise ValueError("invalid wake reason")
    result = await connection.execute(
        """UPDATE case_pipeline_runtime AS runtime
           SET next_tick_at=LEAST(runtime.next_tick_at,now()),
               wake_reason=$2,updated_at=now()
           FROM procurement_cases AS procurement_case
           WHERE runtime.case_id=procurement_case.id
             AND runtime.enabled AND NOT runtime.paused
             AND procurement_case.status NOT IN ('closed','cancelled','selected')
             AND EXISTS (
               SELECT 1 FROM supplier_conversations AS conversation
               WHERE conversation.case_id=runtime.case_id
                 AND conversation.session_name=$1 AND conversation.channel='whatsapp'
             )""",
        session_name.strip(),
        reason.strip(),
    )
    return int(result.split()[-1]) if result.startswith("UPDATE ") else 0


async def wake_conversation_pipeline(
    connection: asyncpg.Connection,
    *,
    conversation_id: UUID,
    reason: str,
) -> bool:
    """Wake only the active case owning a newly accepted WhatsApp reply."""

    if not reason.strip() or len(reason) > 200:
        raise ValueError("invalid wake reason")
    result = await connection.execute(
        """UPDATE case_pipeline_runtime AS runtime
           SET next_tick_at=LEAST(runtime.next_tick_at,now()),
               wake_reason=$2,updated_at=now()
           FROM supplier_conversations AS conversation
           JOIN procurement_cases AS procurement_case
             ON procurement_case.id=conversation.case_id
           WHERE runtime.case_id=conversation.case_id
             AND conversation.id=$1 AND conversation.channel='whatsapp'
             AND runtime.enabled AND NOT runtime.paused
             AND procurement_case.status NOT IN ('closed','cancelled','selected')""",
        conversation_id,
        reason.strip(),
    )
    return result == "UPDATE 1"


async def schedule_due_pipelines(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as c, c.transaction():
        rows = await c.fetch(
            """SELECT p.case_id,p.tick_generation FROM case_pipeline_runtime p
            JOIN procurement_cases pc ON pc.id=p.case_id JOIN users u ON u.id=pc.owner_user_id
            WHERE p.enabled AND NOT p.paused AND p.next_tick_at<=now() AND u.active
              AND pc.status NOT IN ('closed','cancelled','selected')
              AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.kind='pipeline_tick'
                AND j.payload->>'case_id'=p.case_id::text
                AND j.status IN ('pending','retry','running'))
            ORDER BY p.next_tick_at LIMIT 20 FOR UPDATE OF p SKIP LOCKED"""
        )
        for row in rows:
            generation = int(row["tick_generation"]) + 1
            await enqueue_job(
                c,
                kind="pipeline_tick",
                payload={"case_id": str(row["case_id"])},
                idempotency_key=stable_key("pipeline-tick", row["case_id"], generation),
                max_attempts=20,
            )
            await c.execute(
                """UPDATE case_pipeline_runtime SET tick_generation=$2,
                next_tick_at=now()+interval '30 minutes',last_tick_at=now(),updated_at=now()
                WHERE case_id=$1""",
                row["case_id"],
                generation,
            )
        return len(rows)
