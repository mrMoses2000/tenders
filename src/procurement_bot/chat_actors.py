from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from procurement_bot.queue import ClaimedOutbox

_UNKNOWN_SEND_ERRORS = (
    "provider_outcome_unknown",
    "provider_acceptance_unknown",
    "WhatsAppSendOutcomeUnknown",
    "RetryableProviderError",
    "TimeoutError",
    "OSError",
)
_JOURNAL_COMPLETION_ERRORS = (
    "RuntimeError",
    "DatatypeMismatchError",
    "InterfaceError",
    "ConnectionDoesNotExistError",
)


async def _record_outcome_recovery(connection: asyncpg.Connection, row, worker_id: str) -> None:
    await connection.execute(
        """INSERT INTO workflow_events(
               case_id,event_type,actor_type,actor_id,idempotency_key,data
           ) VALUES($1,'whatsapp_provider_outcome_reconciliation','worker',$2,$3,$4)
           ON CONFLICT(idempotency_key) DO NOTHING""",
        row["case_id"],
        worker_id,
        f"waha-outcome-reconcile:{row['id']}:{row['provider_request_id']}:{row['status']}:{row['attempts']}",
        {
            "outbox_event_id": str(row["id"]),
            "provider_request_id": row["provider_request_id"],
            "prior_status": row["status"],
            "prior_attempts": row["attempts"],
            "prior_error_code": row["last_error_code"],
        },
    )


class ChatActorBusy(RuntimeError):
    """The physical WhatsApp chat is currently owned by another worker."""


@dataclass(frozen=True, slots=True)
class ChatActorLease:
    actor_id: UUID
    session_name: str
    external_chat_id: str
    worker_id: str
    generation: int
    lease_expires_at: datetime


def _validate_identity(session_name: str, external_chat_id: str, worker_id: str) -> None:
    if not session_name.strip() or not external_chat_id.strip() or not worker_id.strip():
        raise ValueError("session_name, external_chat_id, and worker_id are required")


async def claim_whatsapp_outbox(
    pool: asyncpg.Pool,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    event_id: UUID | None = None,
) -> tuple[ClaimedOutbox, ChatActorLease] | None:
    """Claim the oldest eligible WhatsApp event and its physical-chat actor.

    Outbound events for the same `(session_name, external_chat_id)` remain
    ordered while any earlier event is pending, retrying, or sending. Enabled
    autonomous cases may claim an unknown reserved ID for exact GET/journal
    reconciliation; it never reaches the POST branch. The short transaction
    commits before the provider call.
    """

    if not worker_id.strip():
        raise ValueError("worker_id must be non-empty")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")

    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """
            UPDATE outbox_events AS event
            SET status='dead',last_error_code='invalid_conversation_reference'
            WHERE event.event_type='whatsapp.send_text'
              AND event.status IN ('pending','retry')
              AND NOT EXISTS (
                SELECT 1 FROM supplier_conversations AS conversation
                WHERE conversation.id=CASE
                  WHEN event.payload->>'conversation_id' ~*
                       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                    THEN (event.payload->>'conversation_id')::uuid
                  ELSE NULL
                END
              )
            """
        )
        expired_autonomous = await connection.fetch(
            """SELECT event.id,event.provider_request_id,event.attempts,
                      event.last_error_code,event.status,conversation.case_id
               FROM outbox_events event
               JOIN supplier_conversations conversation ON conversation.id=CASE
                   WHEN event.payload->>'conversation_id' ~*
                        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   THEN (event.payload->>'conversation_id')::uuid ELSE NULL END
               JOIN case_pipeline_runtime runtime ON runtime.case_id=conversation.case_id
               JOIN procurement_cases procurement_case ON procurement_case.id=conversation.case_id
               WHERE event.event_type='whatsapp.send_text' AND event.status='sending'
                 AND event.lease_expires_at<=now() AND event.provider_request_id IS NOT NULL
                 AND event.external_id IS NULL AND runtime.enabled AND NOT runtime.paused
                 AND procurement_case.status NOT IN ('closed','cancelled','selected')
                 AND event.payload->>'case_id'=conversation.case_id::text
               ORDER BY event.lease_expires_at,event.id LIMIT 32
               FOR UPDATE OF event SKIP LOCKED"""
        )
        for row in expired_autonomous:
            await _record_outcome_recovery(connection, row, worker_id)
            await connection.execute(
                """UPDATE outbox_events SET status='retry',available_at=now(),
                locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,
                last_error_code='provider_outcome_unknown'
                WHERE id=$1 AND status='sending' AND lease_expires_at<=now()""",
                row["id"],
            )

        await connection.execute(
            """
            UPDATE outbox_events
            SET status='dead',locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,
                last_error_code='provider_outcome_unknown'
            WHERE event_type='whatsapp.send_text' AND status='sending'
              AND lease_expires_at<=now() AND provider_request_id IS NOT NULL
              AND external_id IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM supplier_conversations conversation
                JOIN case_pipeline_runtime runtime ON runtime.case_id=conversation.case_id
                JOIN procurement_cases procurement_case ON procurement_case.id=conversation.case_id
                WHERE conversation.id=CASE
                    WHEN outbox_events.payload->>'conversation_id' ~*
                         '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                    THEN (outbox_events.payload->>'conversation_id')::uuid ELSE NULL END
                  AND runtime.enabled AND NOT runtime.paused
                  AND procurement_case.status NOT IN ('closed','cancelled','selected')
                  AND outbox_events.payload->>'case_id'=conversation.case_id::text
              )
            """
        )
        await connection.execute(
            """
            UPDATE outbox_events
            SET status='dead',locked_at=NULL,locked_by=NULL,lease_expires_at=NULL,
                last_error_code=CASE WHEN last_error_code=''
                    THEN 'lease_expired' ELSE last_error_code END
            WHERE status='sending' AND lease_expires_at<=now()
              AND attempts>=max_attempts AND event_type='whatsapp.send_text'
            """
        )
        candidates = await connection.fetch(
            """
            SELECT event.*,conversation.case_id,conversation.session_name,
                   conversation.external_chat_id
            FROM outbox_events AS event
            JOIN supplier_conversations AS conversation
              ON conversation.id=CASE
                WHEN event.payload->>'conversation_id' ~*
                     '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                  THEN (event.payload->>'conversation_id')::uuid
                ELSE NULL
              END
            WHERE event.event_type='whatsapp.send_text'
              AND ($3::uuid IS NULL OR event.id=$3)
              AND (
                event.attempts < event.max_attempts
                OR (
                  event.provider_request_id IS NOT NULL AND event.external_id IS NULL
                  AND (
                    event.last_error_code=ANY($1::text[])
                    OR (
                      event.last_error_code=ANY($2::text[])
                      AND event.available_at<=now()
                      AND EXISTS (
                      SELECT 1 FROM whatsapp_send_acceptance_journal journal
                      WHERE journal.outbox_event_id=event.id
                      )
                    )
                  )
                  AND EXISTS (
                    SELECT 1 FROM case_pipeline_runtime runtime
                    JOIN procurement_cases active_case ON active_case.id=runtime.case_id
                    WHERE runtime.case_id=conversation.case_id AND runtime.enabled
                      AND NOT runtime.paused
                      AND active_case.status NOT IN ('closed','cancelled','selected')
                      AND event.payload->>'case_id'=conversation.case_id::text
                  )
                )
              )
              AND (
                (event.status IN ('pending','retry') AND event.available_at<=now())
                OR (event.status='sending' AND event.lease_expires_at<=now()
                    AND event.provider_request_id IS NULL)
                OR (
                  event.status='dead' AND event.provider_request_id IS NOT NULL
                  AND event.available_at<=now()
                  AND event.external_id IS NULL
                  AND (
                    event.last_error_code=ANY($1::text[])
                    OR (
                      event.last_error_code=ANY($2::text[])
                      AND event.available_at<=now()
                      AND EXISTS (
                      SELECT 1 FROM whatsapp_send_acceptance_journal journal
                      WHERE journal.outbox_event_id=event.id
                      )
                    )
                  )
                  AND EXISTS (
                    SELECT 1 FROM case_pipeline_runtime runtime
                    JOIN procurement_cases active_case ON active_case.id=runtime.case_id
                    WHERE runtime.case_id=conversation.case_id AND runtime.enabled
                      AND NOT runtime.paused
                      AND active_case.status NOT IN ('closed','cancelled','selected')
                      AND event.payload->>'case_id'=conversation.case_id::text
                  )
                )
              )
              AND NOT EXISTS (
                SELECT 1
                FROM outbox_events AS prior
                JOIN supplier_conversations AS prior_conversation
                  ON prior_conversation.id=CASE
                    WHEN prior.payload->>'conversation_id' ~*
                         '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                      THEN (prior.payload->>'conversation_id')::uuid
                    ELSE NULL
                  END
                WHERE prior.event_type='whatsapp.send_text'
                  AND prior_conversation.session_name=conversation.session_name
                  AND prior_conversation.external_chat_id=conversation.external_chat_id
                  AND (prior.created_at,prior.id)<(event.created_at,event.id)
                  AND (
                    prior.status IN ('pending','retry','sending')
                    OR (
                      prior.status='dead'
                      AND prior.provider_request_id IS NOT NULL
                      AND prior.external_id IS NULL
                    )
                  )
              )
            ORDER BY event.available_at,event.created_at,event.id
            FOR UPDATE OF event SKIP LOCKED
            LIMIT 32
            """,
            list(_UNKNOWN_SEND_ERRORS),
            list(_JOURNAL_COMPLETION_ERRORS),
            event_id,
        )
        for row in candidates:
            actor_row = await connection.fetchrow(
                """
                INSERT INTO chat_actor_leases(session_name,external_chat_id)
                VALUES ($1,$2)
                ON CONFLICT (session_name,external_chat_id) DO NOTHING
                RETURNING actor_id,owner_worker_id,generation,lease_expires_at
                """,
                row["session_name"],
                row["external_chat_id"],
            )
            if actor_row is None:
                actor_row = await connection.fetchrow(
                    """
                    SELECT actor_id,owner_worker_id,generation,lease_expires_at
                    FROM chat_actor_leases
                    WHERE session_name=$1 AND external_chat_id=$2
                    FOR UPDATE
                    """,
                    row["session_name"],
                    row["external_chat_id"],
                )
            if actor_row is None:
                continue
            claimed_actor = await connection.fetchrow(
                """
                UPDATE chat_actor_leases
                SET owner_worker_id=$3,generation=generation+1,
                    lease_expires_at=now()+make_interval(secs => $4),updated_at=now()
                WHERE session_name=$1 AND external_chat_id=$2
                  AND (owner_worker_id IS NULL OR lease_expires_at<=now())
                RETURNING actor_id,owner_worker_id,generation,lease_expires_at
                """,
                row["session_name"],
                row["external_chat_id"],
                worker_id,
                lease_seconds,
            )
            if claimed_actor is None:
                continue
            if row["status"] == "dead":
                await _record_outcome_recovery(connection, row, worker_id)
            claimed_event = await connection.fetchrow(
                """
                UPDATE outbox_events
                SET status='sending',attempts=outbox_events.attempts+1,
                    locked_at=now(),locked_by=$2,
                    lease_expires_at=now()+make_interval(secs => $3)
                WHERE id=$1
                RETURNING *
                """,
                row["id"],
                worker_id,
                lease_seconds,
            )
            if claimed_event is None:
                raise RuntimeError("WhatsApp event vanished while claiming its actor")
            event = ClaimedOutbox(
                id=claimed_event["id"],
                event_type=claimed_event["event_type"],
                payload=claimed_event["payload"],
                attempts=claimed_event["attempts"],
                max_attempts=claimed_event["max_attempts"],
                idempotency_key=claimed_event["idempotency_key"],
                locked_by=claimed_event["locked_by"],
                lease_expires_at=claimed_event["lease_expires_at"],
                approval_id=claimed_event.get("approval_id"),
                provider_request_id=claimed_event.get("provider_request_id"),
            )
            actor = ChatActorLease(
                actor_id=claimed_actor["actor_id"],
                session_name=row["session_name"],
                external_chat_id=row["external_chat_id"],
                worker_id=worker_id,
                generation=claimed_actor["generation"],
                lease_expires_at=claimed_actor["lease_expires_at"],
            )
            return event, actor
    return None


async def renew_chat_actor(
    pool: asyncpg.Pool,
    actor: ChatActorLease,
    *,
    lease_seconds: int,
) -> bool:
    async with pool.acquire() as connection:
        updated = await connection.fetchval(
            """
            UPDATE chat_actor_leases
            SET lease_expires_at=now()+make_interval(secs => $5),updated_at=now()
            WHERE actor_id=$1 AND session_name=$2 AND external_chat_id=$3
              AND owner_worker_id=$4 AND generation=$6 AND lease_expires_at>now()
            RETURNING TRUE
            """,
            actor.actor_id,
            actor.session_name,
            actor.external_chat_id,
            actor.worker_id,
            lease_seconds,
            actor.generation,
        )
    return bool(updated)


async def release_chat_actor(pool: asyncpg.Pool, actor: ChatActorLease) -> bool:
    """Release only the generation still owned by this worker."""

    async with pool.acquire() as connection:
        released = await connection.fetchval(
            """
            UPDATE chat_actor_leases
            SET owner_worker_id=NULL,lease_expires_at=NULL,updated_at=now()
            WHERE actor_id=$1 AND session_name=$2 AND external_chat_id=$3
              AND owner_worker_id=$4 AND generation=$5
            RETURNING TRUE
            """,
            actor.actor_id,
            actor.session_name,
            actor.external_chat_id,
            actor.worker_id,
            actor.generation,
        )
    return bool(released)


async def acquire_chat_actor_in_transaction(
    connection: asyncpg.Connection,
    *,
    session_name: str,
    external_chat_id: str,
    worker_id: str,
    lease_seconds: int = 30,
) -> ChatActorLease:
    """Take the physical chat for a short projection transaction.

    The caller must release the returned generation before committing. A
    concurrent WAHA sender holds a durable lease but no database row lock; an
    active sender lease therefore causes the projection to defer and retry.
    """

    _validate_identity(session_name, external_chat_id, worker_id)
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    await connection.execute(
        """
        INSERT INTO chat_actor_leases(session_name,external_chat_id)
        VALUES ($1,$2)
        ON CONFLICT (session_name,external_chat_id) DO NOTHING
        """,
        session_name,
        external_chat_id,
    )
    row = await connection.fetchrow(
        """
        SELECT actor_id,owner_worker_id,generation,lease_expires_at
        FROM chat_actor_leases
        WHERE session_name=$1 AND external_chat_id=$2
        FOR UPDATE
        """,
        session_name,
        external_chat_id,
    )
    if row is None:
        raise RuntimeError("chat actor row could not be locked")
    claimed = await connection.fetchrow(
        """
        UPDATE chat_actor_leases
        SET owner_worker_id=$3,generation=generation+1,
            lease_expires_at=now()+make_interval(secs => $4),updated_at=now()
        WHERE session_name=$1 AND external_chat_id=$2
          AND (owner_worker_id IS NULL OR lease_expires_at<=now())
        RETURNING actor_id,owner_worker_id,generation,lease_expires_at
        """,
        session_name,
        external_chat_id,
        worker_id,
        lease_seconds,
    )
    if claimed is None:
        raise ChatActorBusy("physical WhatsApp chat is owned by an active worker")
    return ChatActorLease(
        actor_id=claimed["actor_id"],
        session_name=session_name,
        external_chat_id=external_chat_id,
        worker_id=worker_id,
        generation=claimed["generation"],
        lease_expires_at=claimed["lease_expires_at"],
    )


async def release_chat_actor_in_transaction(
    connection: asyncpg.Connection,
    actor: ChatActorLease,
) -> None:
    """Release a short transaction's actor claim before its commit."""

    released = await connection.execute(
        """
        UPDATE chat_actor_leases
        SET owner_worker_id=NULL,lease_expires_at=NULL,updated_at=now()
        WHERE actor_id=$1 AND session_name=$2 AND external_chat_id=$3
          AND owner_worker_id=$4 AND generation=$5
        """,
        actor.actor_id,
        actor.session_name,
        actor.external_chat_id,
        actor.worker_id,
        actor.generation,
    )
    if released != "UPDATE 1":
        raise RuntimeError("chat actor generation changed during short projection")


async def assign_supplier_dialogue(
    connection: asyncpg.Connection,
    *,
    group_id: UUID,
    lane_id: UUID,
    item_id: UUID,
    supplier_id: UUID,
    conversation_id: UUID,
    target: int = 20,
    lane_count: int = 4,
    max_per_lane: int = 5,
    max_active_groups: int = 3,
) -> UUID:
    """Assign a supplier-item to its stable research lane within quota."""

    if target < 1 or lane_count != 4 or max_per_lane < 1 or max_active_groups < 1:
        raise ValueError("target and quotas must be positive; lane_count must be 4")
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('supplier-dialogue-active-groups',0))"
        )
        active_groups = await connection.fetchrow(
            """
            SELECT count(DISTINCT group_id)::int AS group_count,
                   COALESCE(bool_or(group_id=$1),false) AS group_is_active
            FROM supplier_dialogue_assignments
            WHERE status IN ('assigned','waiting_reply')
            """,
            group_id,
        )
        if (
            active_groups is not None
            and not active_groups["group_is_active"]
            and int(active_groups["group_count"] or 0) >= max_active_groups
        ):
            raise ChatActorBusy("the maximum number of active dialogue groups is already assigned")
        lane = await connection.fetchrow(
            """
            SELECT lane.id,lane.group_id,lane.lane_no,group_row.request_item_id
            FROM research_lanes AS lane
            JOIN research_item_groups AS group_row ON group_row.id=lane.group_id
            WHERE lane.id=$1 FOR UPDATE OF lane
            """,
            lane_id,
        )
        if lane is None or lane["group_id"] != group_id:
            raise ValueError("lane does not belong to research group")
        if lane["request_item_id"] != item_id:
            raise ValueError("item does not belong to research group")
        if lane["lane_no"] < 0 or lane["lane_no"] >= lane_count:
            raise ValueError("lane number is outside the configured lane count")
        conversation_supplier = await connection.fetchval(
            "SELECT supplier_id FROM supplier_conversations WHERE id=$1",
            conversation_id,
        )
        if conversation_supplier != supplier_id:
            raise ValueError("conversation does not belong to supplier")
        has_item_target = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM supplier_conversation_targets
                WHERE conversation_id=$1 AND request_item_id=$2
            )
            """,
            conversation_id,
            item_id,
        )
        if not has_item_target:
            raise ValueError("conversation is not linked to the assigned item")
        existing = await connection.fetchrow(
            """
            SELECT id,lane_id,conversation_id,status FROM supplier_dialogue_assignments
            WHERE group_id=$1 AND request_item_id=$2 AND supplier_id=$3
            """,
            group_id,
            item_id,
            supplier_id,
        )
        if existing is not None:
            if (
                existing["lane_id"] != lane_id
                or existing["conversation_id"] != conversation_id
            ):
                raise ValueError("supplier dialogue is already assigned to another lane or chat")
            if existing.get("status", "assigned") in {"assigned", "waiting_reply"}:
                return existing["id"]
        active_count = await connection.fetchval(
            """
            SELECT count(*) FROM supplier_dialogue_assignments
            WHERE lane_id=$1 AND status IN ('assigned','waiting_reply')
            """,
            lane_id,
        )
        if int(active_count or 0) >= max_per_lane:
            raise ChatActorBusy("research lane already has its maximum active assignments")
        if existing is not None:
            await connection.execute(
                "UPDATE supplier_dialogue_assignments SET status='assigned',updated_at=now() "
                "WHERE id=$1", existing["id"],
            )
            return existing["id"]
        assignment_id = await connection.fetchval(
            """
            INSERT INTO supplier_dialogue_assignments(
                group_id,lane_id,request_item_id,supplier_id,conversation_id,
                target, lane_count, max_per_lane
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (group_id,request_item_id,supplier_id) DO NOTHING
            RETURNING id
            """,
            group_id,
            lane_id,
            item_id,
            supplier_id,
            conversation_id,
            target,
            lane_count,
            max_per_lane,
        )
        if assignment_id is not None:
            return assignment_id
        existing = await connection.fetchrow(
            """
            SELECT id,lane_id,conversation_id,status FROM supplier_dialogue_assignments
            WHERE group_id=$1 AND request_item_id=$2 AND supplier_id=$3
            """,
            group_id,
            item_id,
            supplier_id,
        )
        if existing is None:
            raise RuntimeError("supplier dialogue assignment insert was not resolved")
        if (
            existing["lane_id"] != lane_id
            or existing["conversation_id"] != conversation_id
        ):
            raise ValueError("supplier dialogue is already assigned to another lane or chat")
        return existing["id"]
