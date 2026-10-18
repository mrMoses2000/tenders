"""Durable fail-closed account/session controls for WhatsApp outbound traffic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from procurement_bot.supplier_dialogue import WhatsAppSendDeferred

ACCOUNT_TIMEZONE = "Asia/Almaty"
UNKNOWN_DEFER_SECONDS = 30 * 60
DEFAULT_DAILY_MESSAGE_LIMIT = 30
DEFAULT_DAILY_NEW_CONTACT_LIMIT = 0
DEFAULT_MINIMUM_INTERVAL_SECONDS = 120
DEFAULT_MAX_CONCURRENT_POSTS = 1
PROVIDER_CLEAR_MAX_AGE = timedelta(minutes=30)

ProviderState = Literal[
    "unknown", "clear", "restricted", "timelock", "capped", "unavailable"
]
SendOutcome = Literal["accepted", "unknown", "rejected"]


class WhatsAppAccountDenied(WhatsAppSendDeferred):
    """The account-wide gate deferred a new POST; queued work remains intact."""


@dataclass(frozen=True, slots=True)
class WhatsAppAccountReservation:
    outbox_event_id: UUID
    session_name: str
    supplier_contact_id: UUID
    is_new_contact: bool
    generation: int
    lease_expires_at: datetime


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("account-control timestamp must be timezone-aware")
    return current.astimezone(UTC)


def _reason(value: str, *, name: str = "reason") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value.strip()[:240]


def _blocked(reason: str, *, now: datetime) -> WhatsAppAccountDenied:
    return WhatsAppAccountDenied(
        available_at=now + timedelta(seconds=UNKNOWN_DEFER_SECONDS),
        reason=reason,
    )


def _provider_clear_is_fresh(state: dict[str, Any], now: datetime) -> bool:
    observed_at = state.get("provider_observed_at")
    if state.get("provider_state") != "clear" or observed_at is None:
        return False
    age = now - _now(observed_at)
    return timedelta(0) <= age <= PROVIDER_CLEAR_MAX_AGE


async def ensure_account(
    connection: asyncpg.Connection, session_name: str
) -> asyncpg.Record:
    """Create an unseen session in held/unknown state and record its origin."""

    if not isinstance(session_name, str) or not session_name.strip():
        raise ValueError("session_name must be non-empty")
    normalized = session_name.strip()
    row = await connection.fetchrow(
        """INSERT INTO whatsapp_account_controls(
               session_name,operator_held,operator_hold_reason,provider_state
           ) VALUES($1,TRUE,'new_session_fail_closed','unknown')
           ON CONFLICT(session_name) DO NOTHING
           RETURNING *""",
        normalized,
    )
    if row is not None:
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,details
               ) VALUES($1,'session_bootstrapped','new_session_fail_closed',
                        '{"operator_held":true,"provider_state":"unknown"}'::jsonb)""",
            normalized,
        )
        return row
    existing = await connection.fetchrow(
        "SELECT * FROM whatsapp_account_controls WHERE session_name=$1",
        normalized,
    )
    if existing is None:
        raise RuntimeError("WhatsApp account control row disappeared")
    return existing


async def get_account_state(
    connection: asyncpg.Connection, session_name: str
) -> dict[str, Any]:
    """Return account gates and budgets; an absent row is represented as held."""

    if not isinstance(session_name, str) or not session_name.strip():
        raise ValueError("session_name must be non-empty")
    row = await connection.fetchrow(
        "SELECT * FROM whatsapp_account_controls WHERE session_name=$1",
        session_name.strip(),
    )
    if row is None:
        return {
            "session_name": session_name.strip(),
            "operator_held": True,
            "operator_hold_reason": "new_session_fail_closed",
            "provider_state": "unknown",
            "provider_observed_at": None,
            "circuit_open": False,
            "circuit_reason": "",
            "daily_message_limit": DEFAULT_DAILY_MESSAGE_LIMIT,
            "daily_new_contact_limit": DEFAULT_DAILY_NEW_CONTACT_LIMIT,
            "minimum_interval_seconds": DEFAULT_MINIMUM_INTERVAL_SECONDS,
            "max_concurrent_posts": DEFAULT_MAX_CONCURRENT_POSTS,
            "outbound_allowed": False,
            "unresolved_reservations": 0,
        }
    unresolved = int(
        await connection.fetchval(
            """SELECT count(*) FROM whatsapp_account_send_reservations
               WHERE session_name=$1 AND state IN ('reserved','unknown')""",
            session_name.strip(),
        )
        or 0
    )
    result = dict(row)
    result["unresolved_reservations"] = unresolved
    result["provider_clear_fresh"] = _provider_clear_is_fresh(result, datetime.now(UTC))
    result["outbound_allowed"] = (
        not result["operator_held"]
        and not result["circuit_open"]
        and result["provider_clear_fresh"]
        and unresolved == 0
    )
    return result


async def assert_account_send_allowed(
    connection: asyncpg.Connection,
    session_name: str,
    *,
    now: datetime | None = None,
) -> None:
    """Fast fail-closed preflight before asking the provider for a message id."""

    current = _now(now)
    state = await ensure_account(connection, session_name)
    if state["operator_held"]:
        raise _blocked("whatsapp_account_operator_hold", now=current)
    if not _provider_clear_is_fresh(dict(state), current):
        raise _blocked("whatsapp_provider_restriction_state_not_fresh_clear", now=current)
    if state["circuit_open"]:
        raise _blocked("whatsapp_account_circuit_open", now=current)
    if await connection.fetchval(
        """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations
           WHERE session_name=$1 AND state='unknown')""",
        session_name,
    ):
        raise _blocked("whatsapp_account_outcome_unresolved", now=current)


async def _authorize_session_owner(
    connection: asyncpg.Connection,
    session_name: str,
    actor_user_id: UUID,
    *,
    require_all: bool,
) -> None:
    row = await connection.fetchrow(
        """SELECT actor.active,
                  count(DISTINCT procurement_case.owner_user_id) AS owner_count,
                  bool_or(procurement_case.owner_user_id=$2) AS actor_owns_session,
                  bool_and(procurement_case.owner_user_id=$2) AS actor_owns_all
           FROM users actor
           LEFT JOIN supplier_conversations conversation
             ON conversation.session_name=$1
           LEFT JOIN procurement_cases procurement_case
             ON procurement_case.id=conversation.case_id
            AND procurement_case.status NOT IN ('closed','cancelled','selected')
           WHERE actor.id=$2
           GROUP BY actor.active""",
        session_name,
        actor_user_id,
    )
    if (
        row is None
        or not row["active"]
        or int(row["owner_count"] or 0) == 0
        or (require_all and not row["actor_owns_all"])
        or (not require_all and not row["actor_owns_session"])
    ):
        raise PermissionError("actor must own the active cases using this WhatsApp session")


async def authorize_session_owner(
    connection: asyncpg.Connection,
    session_name: str,
    actor_user_id: UUID,
    *,
    require_all: bool = False,
) -> None:
    """Verify that an active actor owns one or every active case using a session."""

    await _authorize_session_owner(
        connection, session_name, actor_user_id, require_all=require_all
    )


async def set_account_hold(
    connection: asyncpg.Connection,
    session_name: str,
    *,
    held: bool,
    reason: str,
    actor_user_id: UUID,
    now: datetime | None = None,
) -> None:
    """Set an audited owner hold. Releasing also requires clear provider state."""

    if not isinstance(held, bool):
        raise ValueError("held must be a boolean")
    reason = _reason(reason)
    current = _now(now)
    await _authorize_session_owner(
        connection,
        session_name,
        actor_user_id,
        require_all=not held,
    )
    await ensure_account(connection, session_name)
    async with connection.transaction():
        row = await connection.fetchrow(
            "SELECT * FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            session_name,
        )
        if row is None:
            raise RuntimeError("WhatsApp account control row disappeared")
        if not held:
            unresolved = await connection.fetchval(
                """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations
                   WHERE session_name=$1 AND state IN ('reserved','unknown'))""",
                session_name,
            )
            if unresolved:
                raise PermissionError(
                    "cannot release account hold while provider outcomes are unresolved"
                )
            if not _provider_clear_is_fresh(dict(row), current):
                raise PermissionError(
                    "cannot release account hold without a fresh clear provider observation"
                )
            if row["circuit_open"]:
                raise PermissionError("cannot release account hold while circuit is open")
        await connection.execute(
            """UPDATE whatsapp_account_controls SET
                   operator_held=$2,operator_hold_reason=$3,operator_hold_actor=$4,
                   operator_hold_at=$5,updated_at=$5
               WHERE session_name=$1""",
            session_name,
            held,
            reason,
            actor_user_id,
            current,
        )
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,actor_user_id,details
               ) VALUES($1,$2,$3,$4,$5::jsonb)""",
            session_name,
            "hold_set" if held else "hold_released",
            reason,
            actor_user_id,
            {"operator_held": held},
        )
        if not held:
            from procurement_bot.pipeline_runtime import wake_session_pipelines

            await wake_session_pipelines(
                connection,
                session_name=session_name,
                reason="whatsapp_account_hold_released",
            )


async def configure_account_budget(
    connection: asyncpg.Connection,
    session_name: str,
    *,
    daily_message_limit: int,
    daily_new_contact_limit: int,
    minimum_interval_seconds: int,
    max_concurrent_posts: int,
    reason: str,
    actor_user_id: UUID,
    now: datetime | None = None,
) -> None:
    """Update application budgets through an audited owner-only control path."""

    if daily_message_limit < 0 or daily_new_contact_limit < 0:
        raise ValueError("daily budgets must be non-negative")
    if minimum_interval_seconds < 0:
        raise ValueError("minimum_interval_seconds must be non-negative")
    if not 1 <= max_concurrent_posts <= 4:
        raise ValueError("max_concurrent_posts must be between 1 and 4")
    reason = _reason(reason)
    current = _now(now)
    await _authorize_session_owner(
        connection, session_name, actor_user_id, require_all=True
    )
    await ensure_account(connection, session_name)
    async with connection.transaction():
        await connection.fetchrow(
            "SELECT session_name FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            session_name,
        )
        await connection.execute(
            """UPDATE whatsapp_account_controls SET daily_message_limit=$2,
                   daily_new_contact_limit=$3,minimum_interval_seconds=$4,
                   max_concurrent_posts=$5,updated_at=$6 WHERE session_name=$1""",
            session_name,
            daily_message_limit,
            daily_new_contact_limit,
            minimum_interval_seconds,
            max_concurrent_posts,
            current,
        )
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,actor_user_id,details
               ) VALUES($1,'budget_configured',$2,$3,$4::jsonb)""",
            session_name,
            reason,
            actor_user_id,
            {
                "daily_message_limit": daily_message_limit,
                "daily_new_contact_limit": daily_new_contact_limit,
                "minimum_interval_seconds": minimum_interval_seconds,
                "max_concurrent_posts": max_concurrent_posts,
            },
        )
        await request_account_probe(
            connection,
            session_name,
            reason="account_budget_updated",
            actor_user_id=actor_user_id,
            now=current,
        )


async def observe_provider_state(
    connection: asyncpg.Connection,
    session_name: str,
    *,
    state: ProviderState,
    observed_at: datetime,
    source: str,
    reason: str,
    restriction_until: datetime | None = None,
) -> None:
    """Persist a restriction probe. `clear` changes only provider state."""

    if state not in {"unknown", "clear", "restricted", "timelock", "capped", "unavailable"}:
        raise ValueError("invalid provider restriction state")
    source = _reason(source, name="source")
    reason = _reason(reason)
    current = _now(observed_at)
    until = _now(restriction_until) if restriction_until is not None else None
    await ensure_account(connection, session_name)
    async with connection.transaction():
        await connection.fetchrow(
            "SELECT session_name FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            session_name,
        )
        updated = await connection.execute(
            """UPDATE whatsapp_account_controls SET provider_state=$2,
                   provider_observed_at=$3,provider_source=$4,
                   provider_restriction_until=$5,updated_at=$3
               WHERE session_name=$1
                 AND (provider_observed_at IS NULL OR provider_observed_at<=$3)""",
            session_name,
            state,
            current if state != "unknown" else None,
            source,
            until,
        )
        if updated != "UPDATE 1":
            raise ValueError("stale WhatsApp provider restriction observation")
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,details
               ) VALUES($1,'provider_observed',$2,$3::jsonb)""",
            session_name,
            reason,
            {
                "state": state,
                "source": source,
                "observed_at": current.isoformat(),
                "restriction_until": until.isoformat() if until else None,
            },
        )
        if state == "clear":
            await _recover_circuit_if_resolved(connection, session_name, current)


async def request_account_probe(
    connection: asyncpg.Connection,
    session_name: str,
    *,
    reason: str,
    actor_user_id: UUID | None = None,
    now: datetime | None = None,
) -> None:
    """Record a wake hint and wake eligible case ticks without changing send gates."""

    reason = _reason(reason)
    current = _now(now)
    if actor_user_id is not None:
        await _authorize_session_owner(
            connection, session_name, actor_user_id, require_all=False
        )
    await ensure_account(connection, session_name)
    async with connection.transaction():
        await connection.execute(
            """UPDATE whatsapp_account_controls SET probe_requested_at=$2,
                   probe_request_reason=$3,updated_at=$2 WHERE session_name=$1""",
            session_name,
            current,
            reason,
        )
        from procurement_bot.pipeline_runtime import wake_session_pipelines

        await wake_session_pipelines(
            connection,
            session_name=session_name,
            reason=f"whatsapp_account_probe:{reason}"[:200],
        )
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,details
               ) VALUES($1,'probe_requested',$2,'{}'::jsonb)""",
            session_name,
            reason,
        )


async def reserve_outbound(
    pool: asyncpg.Pool,
    event: Any,
    *,
    provider_request_id: str,
    worker_id: str,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> WhatsAppAccountReservation | None:
    """Atomically reserve budgets/account slot and the outbox provider id."""

    if not provider_request_id.strip() or not worker_id.strip():
        raise ValueError("provider_request_id and worker_id are required")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    current = _now(now)
    payload = event.payload
    try:
        conversation_id = UUID(str(payload["conversation_id"]))
        case_id = UUID(str(payload["case_id"]))
        recipient = str(payload["recipient"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("WhatsApp account reservation payload is invalid") from exc
    from procurement_bot.phone import normalize_phone

    recipient = normalize_phone(recipient)
    local = current.astimezone(ZoneInfo(ACCOUNT_TIMEZONE))
    day_started_at = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    async with pool.acquire() as connection, connection.transaction():
        from procurement_bot.whatsapp_delivery import (
            assert_whatsapp_delivery_allowed_on_connection,
        )

        await assert_whatsapp_delivery_allowed_on_connection(
            connection,
            event,
            now=current,
            include_account_gate=False,
            require_consumed_approval=True,
        )
        identity = await connection.fetchrow(
            """SELECT conversation.session_name,conversation.external_chat_id,
                      conversation.supplier_contact_id,contact.normalized_value
               FROM supplier_conversations conversation
               JOIN supplier_contacts contact ON contact.id=conversation.supplier_contact_id
               WHERE conversation.id=$1 AND conversation.case_id=$2
               FOR SHARE OF conversation,contact""",
            conversation_id,
            case_id,
        )
        if (
            identity is None
            or not identity["normalized_value"]
            or normalize_phone(identity["normalized_value"]) != recipient
        ):
            raise ValueError("WhatsApp reservation recipient differs from active contact")
        session_name = identity["session_name"]
        account = await ensure_account(connection, session_name)
        account = await connection.fetchrow(
            "SELECT * FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            session_name,
        )
        if account is None:
            raise RuntimeError("WhatsApp account control row disappeared")
        expired = await connection.fetch(
            """SELECT outbox_event_id FROM whatsapp_account_send_reservations
               WHERE session_name=$1 AND state='reserved'
                 AND post_lease_expires_at IS NOT NULL
                 AND post_lease_expires_at<=$2 FOR UPDATE""",
            session_name,
            current,
        )
        if expired:
            expired_ids = [row["outbox_event_id"] for row in expired]
            await connection.execute(
                """UPDATE whatsapp_account_send_reservations SET state='unknown',
                       post_worker=NULL,post_lease_expires_at=NULL,updated_at=$2
                   WHERE outbox_event_id=ANY($1::uuid[])""",
                expired_ids,
                current,
            )
            await _open_circuit(
                connection,
                session_name,
                reason="expired_post_lease_outcome_unknown",
                outbox_event_id=expired_ids[0],
                now=current,
            )
            # Return normally so the transaction commits the unknown outcome
            # and circuit. The caller defers instead of issuing a POST.
            return None

        if account["operator_held"]:
            raise _blocked("whatsapp_account_operator_hold", now=current)
        if not _provider_clear_is_fresh(dict(account), current):
            raise _blocked("whatsapp_provider_restriction_state_not_fresh_clear", now=current)
        if account["circuit_open"]:
            raise _blocked("whatsapp_account_circuit_open", now=current)

        unresolved = await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations
               WHERE session_name=$1 AND state='unknown')""",
            session_name,
        )
        if unresolved:
            raise _blocked("whatsapp_account_outcome_unresolved", now=current)
        active_posts = int(
            await connection.fetchval(
                """SELECT count(*) FROM whatsapp_account_send_reservations
                   WHERE session_name=$1 AND state='reserved'
                     AND post_lease_expires_at>$2""",
                session_name,
                current,
            )
            or 0
        )
        if active_posts >= int(account["max_concurrent_posts"]):
            raise _blocked("whatsapp_account_concurrent_post_limit", now=current)
        last_post = account["last_post_reserved_at"]
        if last_post is not None:
            next_post = last_post + timedelta(seconds=account["minimum_interval_seconds"])
            if current < next_post:
                raise WhatsAppAccountDenied(
                    available_at=next_post,
                    reason="whatsapp_account_minimum_interval",
                )

        outbound_today = int(
            await connection.fetchval(
                """SELECT count(*) FROM whatsapp_account_send_reservations
                   WHERE session_name=$1 AND state IN ('reserved','accepted','unknown')
                     AND reserved_at >= $2""",
                session_name,
                day_started_at,
            )
            or 0
        )
        if outbound_today >= int(account["daily_message_limit"]):
            tomorrow = day_started_at + timedelta(days=1)
            raise WhatsAppAccountDenied(
                available_at=tomorrow,
                reason="whatsapp_account_daily_message_budget",
            )

        known_contact = await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations
               WHERE session_name=$1 AND supplier_contact_id=$2
                 AND state IN ('reserved','accepted','unknown'))""",
            session_name,
            identity["supplier_contact_id"],
        )
        is_new_contact = known_contact is not True
        if is_new_contact:
            new_contacts_today = int(
                await connection.fetchval(
                    """SELECT count(DISTINCT supplier_contact_id)
                       FROM whatsapp_account_send_reservations
                       WHERE session_name=$1 AND is_new_contact
                         AND state IN ('reserved','accepted','unknown')
                         AND reserved_at >= $2""",
                    session_name,
                    day_started_at,
                )
                or 0
            )
            if new_contacts_today >= int(account["daily_new_contact_limit"]):
                raise WhatsAppAccountDenied(
                    available_at=day_started_at + timedelta(days=1),
                    reason="whatsapp_account_new_contact_budget",
                )

        generation = int(account["post_generation_sequence"]) + 1
        lease_expires_at = current + timedelta(seconds=lease_seconds)
        await connection.execute(
            """UPDATE whatsapp_account_controls SET post_generation_sequence=$2,
                   last_post_reserved_at=$3,updated_at=$3 WHERE session_name=$1""",
            session_name,
            generation,
            current,
        )
        await connection.execute(
            """INSERT INTO whatsapp_account_send_reservations(
                   outbox_event_id,session_name,conversation_id,supplier_contact_id,
                   state,is_new_contact,reserved_at,post_worker,post_generation,
                   post_lease_expires_at
               ) VALUES($1,$2,$3,$4,'reserved',$5,$6,$7,$8,$9)
               ON CONFLICT(outbox_event_id) DO NOTHING""",
            event.id,
            session_name,
            conversation_id,
            identity["supplier_contact_id"],
            is_new_contact,
            current,
            worker_id,
            generation,
            lease_expires_at,
        )
        stored = await connection.fetchrow(
            """SELECT session_name,conversation_id,supplier_contact_id,state,
                      post_worker,post_generation,post_lease_expires_at
               FROM whatsapp_account_send_reservations WHERE outbox_event_id=$1 FOR UPDATE""",
            event.id,
        )
        if (
            stored is None
            or stored["session_name"] != session_name
            or stored["conversation_id"] != conversation_id
            or stored["state"] != "reserved"
            or stored["post_worker"] != worker_id
            or int(stored["post_generation"] or 0) != generation
        ):
            raise RuntimeError("conflicting WhatsApp account reservation exists")
        updated = await connection.execute(
            """UPDATE outbox_events SET provider_request_id=$3
               WHERE id=$1 AND status='sending' AND locked_by=$2
                 AND lease_expires_at>$4 AND provider_request_id IS NULL""",
            event.id,
            worker_id,
            provider_request_id,
            current,
        )
        if updated != "UPDATE 1":
            raise RuntimeError("outbox lease changed before account reservation")
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,actor_user_id,outbox_event_id,details
               ) VALUES($1,'send_reserved','final_post_reservation',NULL,$2,$3::jsonb)""",
            session_name,
            event.id,
            {"is_new_contact": is_new_contact, "generation": generation},
        )
        return WhatsAppAccountReservation(
            outbox_event_id=event.id,
            session_name=session_name,
            supplier_contact_id=identity["supplier_contact_id"],
            is_new_contact=is_new_contact,
            generation=generation,
            lease_expires_at=lease_expires_at,
        )


async def renew_post_lease(
    pool: asyncpg.Pool,
    reservation: WhatsAppAccountReservation,
    *,
    worker_id: str,
    lease_seconds: int = 60,
    now: datetime | None = None,
) -> bool:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    current = _now(now)
    async with pool.acquire() as connection:
        updated = await connection.execute(
            """UPDATE whatsapp_account_send_reservations SET
                   post_lease_expires_at=$5,updated_at=$3
               WHERE outbox_event_id=$1 AND session_name=$2 AND state='reserved'
                 AND post_worker=$4 AND post_generation=$6
                 AND post_lease_expires_at>$3""",
            reservation.outbox_event_id,
            reservation.session_name,
            current,
            worker_id,
            current + timedelta(seconds=lease_seconds),
            reservation.generation,
        )
    return updated == "UPDATE 1"


async def mark_outcome(
    connection: asyncpg.Connection,
    event_id: UUID,
    *,
    state: SendOutcome,
    reason: str,
    accepted_at: datetime | None = None,
    projection_ok: bool = True,
    source: str | None = None,
    now: datetime | None = None,
) -> None:
    """Persist physical outcome; unknowns open the circuit and never refund."""

    if state not in {"accepted", "unknown", "rejected"}:
        raise ValueError("invalid WhatsApp send outcome")
    reason = _reason(reason)
    current = _now(now)
    accepted = _now(accepted_at) if accepted_at is not None else current
    async with connection.transaction():
        reservation = await connection.fetchrow(
            """SELECT * FROM whatsapp_account_send_reservations
               WHERE outbox_event_id=$1 FOR UPDATE""",
            event_id,
        )
        if reservation is None:
            # A legacy worker can finish its request-ID write just as this
            # migration rolls out. Adopt that physical action before treating
            # its later journal/GET evidence as resolved.
            legacy = await connection.fetchrow(
                """SELECT event.provider_request_id,event.created_at,
                          conversation.session_name,conversation.id AS conversation_id,
                          conversation.supplier_contact_id
                   FROM outbox_events event
                   JOIN supplier_conversations conversation ON conversation.id=CASE
                       WHEN event.payload->>'conversation_id' ~*
                            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                       THEN (event.payload->>'conversation_id')::uuid ELSE NULL END
                   WHERE event.id=$1 AND event.event_type='whatsapp.send_text'""",
                event_id,
            )
            if legacy is None or not legacy["provider_request_id"]:
                raise ValueError("WhatsApp account reservation does not exist")
            await ensure_account(connection, legacy["session_name"])
            is_new_contact = not bool(
                await connection.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations
                       WHERE session_name=$1 AND supplier_contact_id=$2)""",
                    legacy["session_name"],
                    legacy["supplier_contact_id"],
                )
            )
            await connection.execute(
                """INSERT INTO whatsapp_account_send_reservations(
                       outbox_event_id,session_name,conversation_id,supplier_contact_id,
                       state,is_new_contact,reserved_at,accepted_at,outcome_source
                   ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'reconstructed_native_trace')
                   ON CONFLICT(outbox_event_id) DO NOTHING""",
                event_id,
                legacy["session_name"],
                legacy["conversation_id"],
                legacy["supplier_contact_id"],
                state,
                is_new_contact,
                legacy["created_at"],
                accepted if state == "accepted" else None,
            )
            reservation = await connection.fetchrow(
                """SELECT * FROM whatsapp_account_send_reservations
                   WHERE outbox_event_id=$1 FOR UPDATE""",
                event_id,
            )
            if reservation is None:
                raise RuntimeError("legacy WhatsApp reservation adoption failed")
        account = await connection.fetchrow(
            "SELECT * FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            reservation["session_name"],
        )
        if account is None:
            raise RuntimeError("WhatsApp account control row disappeared")
        old_state = reservation["state"]
        # A known accepted result cannot be downgraded by a later timeout.
        next_state = "accepted" if old_state == "accepted" else state
        accepted_value = (
            reservation["accepted_at"] or accepted if next_state == "accepted" else None
        )
        outcome_source = source or {
            "accepted": "reconciliation" if state == "accepted" else "reservation",
            "unknown": "reservation",
            "rejected": "provider_rejection",
        }[state]
        if outcome_source not in {
            "reservation", "http", "reconciliation", "reconstructed_native_trace",
            "operator_reconciliation", "provider_rejection", "migration",
        }:
            raise ValueError("invalid WhatsApp send outcome source")
        await connection.execute(
            """UPDATE whatsapp_account_send_reservations SET state=$2,
                   accepted_at=$3,outcome_source=$4,post_worker=NULL,
                   post_lease_expires_at=NULL,updated_at=$5
               WHERE outbox_event_id=$1""",
            event_id,
            next_state,
            accepted_value,
            outcome_source,
            current,
        )
        event_type = {
            "accepted": "send_accepted",
            "unknown": "send_unknown",
            "rejected": "send_rejected",
        }[state]
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,outbox_event_id,details
               ) VALUES($1,$2,$3,$4,$5::jsonb)""",
            reservation["session_name"],
            event_type,
            reason,
            event_id,
            {"state": next_state, "projection_ok": projection_ok},
        )
        if state == "unknown" or (state == "accepted" and not projection_ok):
            await _open_circuit(
                connection,
                reservation["session_name"],
                reason="provider_outcome_unknown" if state == "unknown" else "projection_failed",
                outbox_event_id=event_id,
                now=current,
            )
        else:
            await _recover_circuit_if_resolved(
                connection, reservation["session_name"], current
            )


async def trip_circuit_breaker(
    connection: asyncpg.Connection,
    session_name: str,
    *,
    event_id: UUID | None,
    reason: str,
    now: datetime | None = None,
) -> None:
    """Open an account circuit after local projection failure without refund."""

    current = _now(now)
    reason = _reason(reason)
    await ensure_account(connection, session_name)
    async with connection.transaction():
        await _open_circuit(
            connection,
            session_name,
            reason=reason,
            outbox_event_id=event_id,
            now=current,
        )


async def _open_circuit(
    connection: asyncpg.Connection,
    session_name: str,
    *,
    reason: str,
    outbox_event_id: UUID | None,
    now: datetime,
) -> None:
    updated = await connection.fetchrow(
        """UPDATE whatsapp_account_controls SET circuit_open=TRUE,
               circuit_reason=$2,circuit_opened_at=COALESCE(circuit_opened_at,$3),
               updated_at=$3 WHERE session_name=$1
           RETURNING circuit_reason""",
        session_name,
        reason,
        now,
    )
    if updated is None:
        raise RuntimeError("WhatsApp account control row disappeared")
    await connection.execute(
        """INSERT INTO whatsapp_account_control_events(
               session_name,event_type,reason,outbox_event_id,details
           ) VALUES($1,'circuit_opened',$2,$3,'{}'::jsonb)""",
        session_name,
        reason,
        outbox_event_id,
    )


async def _recover_circuit_if_resolved(
    connection: asyncpg.Connection, session_name: str, now: datetime
) -> bool:
    if await connection.fetchval(
        """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations
           WHERE session_name=$1 AND state IN ('reserved','unknown'))""",
        session_name,
    ):
        return False
    current = await connection.fetchval(
        """UPDATE whatsapp_account_controls SET circuit_open=FALSE,
               circuit_reason='',circuit_opened_at=NULL,updated_at=$2
           WHERE session_name=$1 AND circuit_open AND provider_state='clear'
           RETURNING session_name""",
        session_name,
        now,
    )
    if current is None:
        return False
    await connection.execute(
        """INSERT INTO whatsapp_account_control_events(
               session_name,event_type,reason,details
           ) VALUES($1,'circuit_recovered','all_outcomes_resolved_and_provider_clear',
                    '{}'::jsonb)""",
        session_name,
    )
    return True
