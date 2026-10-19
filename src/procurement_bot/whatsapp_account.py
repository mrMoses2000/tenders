"""Durable fail-closed account/session controls for WhatsApp outbound traffic."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from procurement_bot.providers.waha import WahaAccountIdentity, WahaClient, normalize_account_jid
from procurement_bot.supplier_dialogue import WhatsAppSendDeferred

ACCOUNT_TIMEZONE = "Asia/Almaty"
UNKNOWN_DEFER_SECONDS = 30 * 60
DEFAULT_DAILY_MESSAGE_LIMIT = 30
DEFAULT_DAILY_NEW_CONTACT_LIMIT = 0
DEFAULT_MINIMUM_INTERVAL_SECONDS = 120
DEFAULT_MAX_CONCURRENT_POSTS = 1
PROVIDER_CLEAR_MAX_AGE = timedelta(minutes=30)
ACCOUNT_IDENTITY_OBSERVATION_MAX_AGE = timedelta(seconds=30)
OPERATOR_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,119}$")

ProviderState = Literal["unknown", "clear", "restricted", "timelock", "capped", "unavailable"]
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
    pinned_account_jid: str | None = None
    account_pin_version: int | None = None


@dataclass(frozen=True, slots=True)
class WhatsAppAccountIdentityPin:
    account_jid: str
    version: int


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


async def ensure_account(connection: asyncpg.Connection, session_name: str) -> asyncpg.Record:
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


async def _legacy_unknown_inventory(
    connection: asyncpg.Connection,
    session_name: str,
) -> tuple[datetime, list[asyncpg.Record], str]:
    cutoff = await connection.fetchval(
        "SELECT applied_at FROM schema_migrations "
        "WHERE name='043_whatsapp_account_identity_binding.sql'"
    )
    if cutoff is None:
        raise RuntimeError("identity-binding migration cutoff is unavailable")
    rows = await connection.fetch(
        """SELECT reservation.outbox_event_id,reservation.conversation_id,
                  conversation.case_id,reservation.state,reservation.reserved_at,
                  reservation.created_at,reservation.outcome_source,
                  reservation.pinned_account_jid,reservation.account_pin_version,
                  reservation.observed_account_jid,reservation.account_identity_observed_at
           FROM whatsapp_account_send_reservations reservation
           JOIN supplier_conversations conversation
             ON conversation.id=reservation.conversation_id
           WHERE reservation.session_name=$1 AND reservation.state='unknown'
             AND reservation.reserved_at<$2 AND reservation.created_at<$2
             AND reservation.pinned_account_jid IS NULL
             AND reservation.account_pin_version IS NULL
             AND reservation.observed_account_jid IS NULL
             AND reservation.account_identity_observed_at IS NULL
           ORDER BY reservation.outbox_event_id""",
        session_name,
        cutoff,
    )
    canonical = [
        {
            "outbox_event_id": str(row["outbox_event_id"]),
            "conversation_id": str(row["conversation_id"]),
            "case_id": str(row["case_id"]),
            "state": row["state"],
            "reserved_at": _now(row["reserved_at"]).isoformat(),
            "created_at": _now(row["created_at"]).isoformat(),
            "outcome_source": row["outcome_source"],
            "identity": {
                "pinned_account_jid": row["pinned_account_jid"],
                "account_pin_version": row["account_pin_version"],
                "observed_account_jid": row["observed_account_jid"],
                "account_identity_observed_at": row["account_identity_observed_at"],
            },
        }
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(
            {
                "session_name": session_name,
                "cutoff_at": _now(cutoff).isoformat(),
                "items": canonical,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return cutoff, list(rows), digest


async def load_legacy_unknown_inventory(
    pool: asyncpg.Pool,
    *,
    session_name: str,
    actor_user_id: UUID,
) -> dict[str, Any]:
    """Read the exact eligible legacy inventory for an active owner."""

    if not isinstance(session_name, str) or not session_name.strip():
        raise ValueError("session_name must be non-empty")
    session_name = session_name.strip()
    async with pool.acquire() as connection, connection.transaction():
        await ensure_account(connection, session_name)
        await connection.fetchrow(
            "SELECT session_name FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            session_name,
        )
        await _lock_authorize_session_owner(connection, session_name, actor_user_id)
        cutoff, rows, digest = await _legacy_unknown_inventory(connection, session_name)
        await _lock_authorize_inventory_cases(connection, actor_user_id, rows)
        return {
            "session_name": session_name,
            "cutoff_at": _now(cutoff),
            "reservation_ids": [row["outbox_event_id"] for row in rows],
            "inventory_sha256": digest,
            "reservation_count": len(rows),
            "oldest_age_seconds": (
                max(
                    0,
                    int(
                        (
                            datetime.now(UTC)
                            - _now(min(row["reserved_at"] for row in rows))
                        ).total_seconds()
                    ),
                )
                if rows
                else None
            ),
        }


async def attest_legacy_unknown_inventory(
    pool: asyncpg.Pool,
    *,
    session_name: str,
    actor_user_id: UUID,
    reservation_ids: list[UUID],
    inventory_sha256: str,
    source_reference: str,
    reason: str,
    operator_idempotency_key: str,
) -> dict[str, Any]:
    """Attest a complete exact legacy inventory without changing original rows."""

    if not isinstance(session_name, str) or not session_name.strip():
        raise ValueError("session_name must be non-empty")
    session_name = session_name.strip()
    key = _operator_key(operator_idempotency_key)
    if key is None:
        raise ValueError("operator_idempotency_key is required")
    reason = _reason(reason)
    if not isinstance(source_reference, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/#-]{2,159}", source_reference
    ):
        raise ValueError("source_reference must be a safe evidence reference")
    if not isinstance(inventory_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", inventory_sha256
    ):
        raise ValueError("inventory_sha256 must be a lowercase SHA-256")
    supplied_ids = [UUID(str(value)) for value in reservation_ids]
    if not supplied_ids or len(supplied_ids) != len(set(supplied_ids)):
        raise ValueError("reservation_ids must be a non-empty unique exact inventory")

    async with pool.acquire() as connection, connection.transaction():
        await ensure_account(connection, session_name)
        await connection.fetchrow(
            "SELECT session_name FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            session_name,
        )
        await _lock_authorize_session_owner(connection, session_name, actor_user_id)

        prior = await connection.fetchrow(
            """SELECT id,actor_user_id,inventory_sha256,reservation_count,
                      source_reference,reason
               FROM whatsapp_legacy_unknown_quarantines
               WHERE session_name=$1 AND operator_idempotency_key=$2""",
            session_name,
            key,
        )
        if prior is not None:
            prior_ids = await connection.fetch(
                """SELECT item.outbox_event_id,reservation.conversation_id
                   FROM whatsapp_legacy_unknown_quarantine_items item
                   JOIN whatsapp_account_send_reservations reservation
                     ON reservation.outbox_event_id=item.outbox_event_id
                   WHERE item.quarantine_id=$1 ORDER BY item.outbox_event_id""",
                prior["id"],
            )
            await _lock_authorize_inventory_cases(connection, actor_user_id, prior_ids)
            if (
                prior["actor_user_id"] != actor_user_id
                or prior["inventory_sha256"] != inventory_sha256
                or prior["source_reference"] != source_reference
                or prior["reason"] != reason
                or [row["outbox_event_id"] for row in prior_ids] != sorted(supplied_ids)
            ):
                raise PermissionError(
                    "operator idempotency key was already used for another quarantine"
                )
            return {
                "quarantine_id": prior["id"],
                "reservation_count": int(prior["reservation_count"]),
                "inventory_sha256": prior["inventory_sha256"],
                "replayed": True,
            }

        existing_event = await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM whatsapp_account_control_events
               WHERE session_name=$1 AND operator_idempotency_key=$2)""",
            session_name,
            key,
        )
        if existing_event:
            raise PermissionError("operator idempotency key was already used for another command")

        cutoff, rows, actual_digest = await _legacy_unknown_inventory(connection, session_name)
        await _lock_authorize_inventory_cases(connection, actor_user_id, rows)
        actual_ids = [row["outbox_event_id"] for row in rows]
        if supplied_ids != sorted(supplied_ids) or supplied_ids != actual_ids:
            raise PermissionError(
                "legacy unknown inventory IDs differ from the current exact inventory"
            )
        if actual_digest != inventory_sha256:
            raise PermissionError("legacy unknown inventory hash differs from the current snapshot")

        quarantine_id = await connection.fetchval(
            """INSERT INTO whatsapp_legacy_unknown_quarantines(
                   session_name,actor_user_id,operator_idempotency_key,inventory_sha256,
                   reservation_count,source_reference,reason,cutoff_at
               ) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            session_name,
            actor_user_id,
            key,
            actual_digest,
            len(rows),
            source_reference,
            reason,
            cutoff,
        )
        await connection.executemany(
            """INSERT INTO whatsapp_legacy_unknown_quarantine_items(
                   quarantine_id,outbox_event_id) VALUES($1,$2)""",
            [(quarantine_id, row["outbox_event_id"]) for row in rows],
        )
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,actor_user_id,details,
                   operator_idempotency_key
               ) VALUES($1,'legacy_unknown_quarantined',$2,$3,$4::jsonb,$5)""",
            session_name,
            reason,
            actor_user_id,
            {"inventory_sha256": actual_digest, "reservation_count": len(rows),
             "source_reference": source_reference},
            key,
        )
        return {
            "quarantine_id": quarantine_id,
            "reservation_count": len(rows),
            "inventory_sha256": actual_digest,
            "replayed": False,
        }


async def get_account_state(connection: asyncpg.Connection, session_name: str) -> dict[str, Any]:
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
            "account_identity_pinned": False,
            "account_pin_version": 0,
            "outbound_allowed": False,
            "unresolved_reservations": 0,
            "unknown_reservations": 0,
            "quarantined_unknown_reservations": 0,
            "oldest_quarantined_unknown_age_seconds": None,
        }
    unresolved = int(
        await connection.fetchval(
            """SELECT count(*) FROM whatsapp_account_send_reservations reservation
               WHERE reservation.session_name=$1 AND (
                   reservation.state='reserved'
                   OR (reservation.state='unknown' AND NOT EXISTS (
                       SELECT 1 FROM whatsapp_legacy_unknown_quarantine_items item
                       WHERE item.outbox_event_id=reservation.outbox_event_id
                   )))""",
            session_name.strip(),
        )
        or 0
    )
    unknown_total = int(
        await connection.fetchval(
            """SELECT count(*) FROM whatsapp_account_send_reservations
               WHERE session_name=$1 AND state='unknown'""",
            session_name.strip(),
        )
        or 0
    )
    quarantined = await connection.fetchrow(
        """SELECT count(*) AS count,min(reservation.reserved_at) AS oldest
           FROM whatsapp_legacy_unknown_quarantine_items item
           JOIN whatsapp_account_send_reservations reservation
             ON reservation.outbox_event_id=item.outbox_event_id
           WHERE reservation.session_name=$1 AND reservation.state='unknown'""",
        session_name.strip(),
    )
    result = dict(row)
    result["account_identity_pinned"] = bool(result.get("pinned_account_jid"))
    result["account_pin_version"] = int(result.get("pin_version") or 0)
    for private_field in ("pinned_account_jid", "pinned_by", "pinned_at", "pin_observed_at"):
        result.pop(private_field, None)
    result["unresolved_reservations"] = unresolved
    result["unknown_reservations"] = unknown_total
    result["quarantined_unknown_reservations"] = int(quarantined["count"] or 0)
    result["oldest_quarantined_unknown_age_seconds"] = (
        max(0, int((datetime.now(UTC) - _now(quarantined["oldest"])).total_seconds()))
        if quarantined["oldest"] is not None
        else None
    )
    result["provider_clear_fresh"] = _provider_clear_is_fresh(result, datetime.now(UTC))
    result["outbound_allowed"] = (
        not result["operator_held"]
        and not result["circuit_open"]
        and result["account_identity_pinned"]
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
    if not state["pinned_account_jid"] or int(state["pin_version"]) < 1:
        raise _blocked("whatsapp_account_identity_not_pinned", now=current)
    if await connection.fetchval(
        """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations reservation
           WHERE reservation.session_name=$1 AND reservation.state='unknown'
             AND NOT EXISTS (SELECT 1 FROM whatsapp_legacy_unknown_quarantine_items item
                             WHERE item.outbox_event_id=reservation.outbox_event_id))""",
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


async def _lock_authorize_session_owner(
    connection: asyncpg.Connection,
    session_name: str,
    actor_user_id: UUID,
) -> list[asyncpg.Record]:
    """Lock the active actor and all session case links after the account lock."""

    actor = await connection.fetchrow(
        "SELECT active FROM users WHERE id=$1 FOR SHARE",
        actor_user_id,
    )
    cases = await connection.fetch(
        """SELECT conversation.id AS conversation_id,
                  procurement_case.id AS case_id,
                  procurement_case.owner_user_id
           FROM supplier_conversations conversation
           JOIN procurement_cases procurement_case
             ON procurement_case.id=conversation.case_id
           WHERE conversation.session_name=$1
             AND procurement_case.status NOT IN ('closed','cancelled','selected')
           ORDER BY procurement_case.id,conversation.id
           FOR SHARE OF conversation,procurement_case""",
        session_name,
    )
    if (
        actor is None
        or not actor["active"]
        or not cases
        or any(row["owner_user_id"] != actor_user_id for row in cases)
    ):
        raise PermissionError("actor must own the active cases using this WhatsApp session")
    return cases


async def _lock_authorize_inventory_cases(
    connection: asyncpg.Connection,
    actor_user_id: UUID,
    rows: list[asyncpg.Record],
) -> None:
    conversation_ids = sorted({row["conversation_id"] for row in rows})
    if not conversation_ids:
        return
    cases = await connection.fetch(
        """SELECT conversation.id AS conversation_id,procurement_case.owner_user_id
           FROM supplier_conversations conversation
           JOIN procurement_cases procurement_case
             ON procurement_case.id=conversation.case_id
           WHERE conversation.id=ANY($1::uuid[])
           ORDER BY procurement_case.id,conversation.id
           FOR SHARE OF conversation,procurement_case""",
        conversation_ids,
    )
    if len(cases) != len(conversation_ids) or any(
        row["owner_user_id"] != actor_user_id for row in cases
    ):
        raise PermissionError("actor must own every legacy reservation case")


def _operator_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not OPERATOR_KEY_RE.fullmatch(value):
        raise ValueError("operator_idempotency_key must be 8-120 safe characters")
    return value


async def _existing_pin_retry(
    connection: asyncpg.Connection,
    *,
    session_name: str,
    actor_user_id: UUID,
    expected_jid: str,
    reason: str,
    key: str,
) -> int | None:
    event = await connection.fetchrow(
        """SELECT actor_user_id,reason,details,event_type
           FROM whatsapp_account_control_events
           WHERE session_name=$1 AND operator_idempotency_key=$2""",
        session_name,
        key,
    )
    if event is None:
        return None
    if event["event_type"] not in {"account_identity_pinned", "account_identity_changed"}:
        raise PermissionError("operator idempotency key was already used for another command")
    if (
        event["actor_user_id"] != actor_user_id
        or event["reason"] != reason
        or event["details"].get("account_jid") != expected_jid
    ):
        raise PermissionError("operator idempotency key was already used for another pin")
    return int(event["details"]["pin_version"])


async def authorize_session_owner(
    connection: asyncpg.Connection,
    session_name: str,
    actor_user_id: UUID,
    *,
    require_all: bool = False,
) -> None:
    """Verify that an active actor owns one or every active case using a session."""

    await _authorize_session_owner(connection, session_name, actor_user_id, require_all=require_all)


async def load_account_identity_pin(
    pool: asyncpg.Pool,
    session_name: str,
) -> WhatsAppAccountIdentityPin:
    """Read the current pin; reserve_outbound rechecks this snapshot atomically."""

    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT pinned_account_jid,pin_version FROM whatsapp_account_controls "
            "WHERE session_name=$1",
            session_name,
        )
    if row is None or not row["pinned_account_jid"] or int(row["pin_version"] or 0) < 1:
        raise WhatsAppSendDeferred(
            available_at=datetime.now(UTC) + timedelta(minutes=5),
            reason="whatsapp_account_identity_not_pinned",
        )
    return WhatsAppAccountIdentityPin(
        account_jid=normalize_account_jid(row["pinned_account_jid"]),
        version=int(row["pin_version"]),
    )


async def record_account_identity_preflight_event(
    pool: asyncpg.Pool,
    *,
    session_name: str,
    outbox_event_id: UUID,
    event_type: Literal["account_identity_mismatch", "account_identity_check_failed"],
    reason: str,
    pin: WhatsAppAccountIdentityPin,
    observed_identity: WahaAccountIdentity | None = None,
) -> None:
    """Audit a preflight denial before any provider request ID or send reservation exists."""

    reason = _reason(reason)
    details: dict[str, Any] = {
        "pinned_account_jid": pin.account_jid,
        "pin_version": pin.version,
        "observation_available": observed_identity is not None,
    }
    if observed_identity is not None:
        details["observed_account_jid"] = normalize_account_jid(observed_identity.account_jid)
        details["observed_at"] = _now(observed_identity.observed_at).isoformat()
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,outbox_event_id,details
               ) VALUES($1,$2,$3,$4,$5::jsonb)""",
            session_name,
            event_type,
            reason,
            outbox_event_id,
            details,
        )


async def pin_whatsapp_account_identity(
    pool: asyncpg.Pool,
    client: WahaClient,
    *,
    actor_user_id: UUID,
    expected_account_jid: str,
    reason: str,
    now: datetime | None = None,
    operator_idempotency_key: str | None = None,
) -> int:
    """Pin an owner-confirmed live self JID without changing send gates.

    Callers without an operator key retain the historical one-call/one-version
    behavior. Authenticated operator handlers should pass a stable key so a
    network retry returns the committed version without another WAHA read.
    """

    expected_jid = normalize_account_jid(expected_account_jid)
    reason = _reason(reason)
    key = _operator_key(operator_idempotency_key)
    session_name = client.session_name

    if key is not None:
        async with pool.acquire() as connection, connection.transaction():
            await ensure_account(connection, session_name)
            account = await connection.fetchrow(
                "SELECT session_name FROM whatsapp_account_controls "
                "WHERE session_name=$1 FOR UPDATE",
                session_name,
            )
            if account is None:
                raise RuntimeError("WhatsApp account control row disappeared")
            await _lock_authorize_session_owner(connection, session_name, actor_user_id)
            prior = await _existing_pin_retry(
                connection,
                session_name=session_name,
                actor_user_id=actor_user_id,
                expected_jid=expected_jid,
                reason=reason,
                key=key,
            )
        if prior is not None:
            return prior
    else:
        async with pool.acquire() as connection:
            await authorize_session_owner(
                connection, session_name, actor_user_id, require_all=True
            )

    identity: WahaAccountIdentity = await client.get_account_identity()
    if normalize_account_jid(identity.account_jid) != expected_jid:
        raise PermissionError("live WAHA identity differs from the owner-confirmed identity")

    async with pool.acquire() as connection, connection.transaction():
        await ensure_account(connection, session_name)
        account = await connection.fetchrow(
            "SELECT * FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            session_name,
        )
        if account is None:
            raise RuntimeError("WhatsApp account control row disappeared")
        await _lock_authorize_session_owner(connection, session_name, actor_user_id)
        if key is not None:
            prior = await _existing_pin_retry(
                connection,
                session_name=session_name,
                actor_user_id=actor_user_id,
                expected_jid=expected_jid,
                reason=reason,
                key=key,
            )
            if prior is not None:
                return prior

        current = _now(now)
        observed_at = _now(identity.observed_at)
        if not timedelta(0) <= current - observed_at <= ACCOUNT_IDENTITY_OBSERVATION_MAX_AGE:
            raise PermissionError("live WAHA identity observation is stale or future-dated")
        active_post = await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations
               WHERE session_name=$1 AND state='reserved')""",
            session_name,
        )
        if active_post:
            raise PermissionError(
                "cannot change account pin while a send reservation is active"
            )
        version = int(account["pin_version"]) + 1
        event_type = (
            "account_identity_pinned"
            if account["pinned_account_jid"] is None
            else "account_identity_changed"
        )
        await connection.execute(
            """UPDATE whatsapp_account_controls SET pinned_account_jid=$2,
                   pinned_by=$3,pinned_at=$4,pin_observed_at=$5,pin_version=$6,
                   updated_at=$4 WHERE session_name=$1""",
            session_name,
            normalize_account_jid(identity.account_jid),
            actor_user_id,
            current,
            observed_at,
            version,
        )
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,actor_user_id,details,
                   operator_idempotency_key
               ) VALUES($1,$2,$3,$4,$5::jsonb,$6)""",
            session_name,
            event_type,
            reason,
            actor_user_id,
            {
                "account_jid": expected_jid,
                "pin_version": version,
                "observed_at": observed_at.isoformat(),
            },
            key,
        )
    return version


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
                """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations reservation
                   WHERE reservation.session_name=$1 AND (
                       reservation.state='reserved'
                       OR (reservation.state='unknown' AND NOT EXISTS (
                           SELECT 1 FROM whatsapp_legacy_unknown_quarantine_items item
                           WHERE item.outbox_event_id=reservation.outbox_event_id
                       ))))""",
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
    await _authorize_session_owner(connection, session_name, actor_user_id, require_all=True)
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
        await _authorize_session_owner(connection, session_name, actor_user_id, require_all=False)
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
    identity_observation: WahaAccountIdentity | None = None,
    expected_account_jid: str | None = None,
    expected_pin_version: int | None = None,
    expected_session_name: str | None = None,
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
        if expected_session_name is not None and session_name != expected_session_name:
            raise _blocked("waha_session_mismatch", now=_now(now))
        account = await ensure_account(connection, session_name)
        account = await connection.fetchrow(
            "SELECT * FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            session_name,
        )
        if account is None:
            raise RuntimeError("WhatsApp account control row disappeared")
        current = _now(now)  # Account-lock waits must count toward observation age.
        local = current.astimezone(ZoneInfo(ACCOUNT_TIMEZONE))
        day_started_at = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        if not account["pinned_account_jid"] or int(account["pin_version"]) < 1:
            raise _blocked("whatsapp_account_identity_not_pinned", now=current)
        if (
            identity_observation is None
            or expected_account_jid is None
            or expected_pin_version is None
        ):
            raise _blocked("whatsapp_account_identity_observation_required", now=current)
        expected_jid = normalize_account_jid(expected_account_jid)
        observed_jid = normalize_account_jid(identity_observation.account_jid)
        observed_at = _now(identity_observation.observed_at)
        age = current - observed_at
        if age < timedelta(0) or age > ACCOUNT_IDENTITY_OBSERVATION_MAX_AGE:
            raise WhatsAppSendDeferred(
                available_at=current + timedelta(minutes=1),
                reason="whatsapp_account_identity_observation_stale",
            )
        if (
            account["pinned_account_jid"] != expected_jid
            or int(account["pin_version"]) != expected_pin_version
            or observed_jid != expected_jid
        ):
            raise WhatsAppSendDeferred(
                available_at=current + timedelta(minutes=30),
                reason="whatsapp_account_identity_changed",
            )
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
                       post_worker=NULL,post_generation=NULL,post_lease_expires_at=NULL,updated_at=$2
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
            """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations reservation
               WHERE reservation.session_name=$1 AND reservation.state='unknown'
                 AND NOT EXISTS (SELECT 1 FROM whatsapp_legacy_unknown_quarantine_items item
                                 WHERE item.outbox_event_id=reservation.outbox_event_id))""",
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
                   post_lease_expires_at,pinned_account_jid,account_pin_version,
                   observed_account_jid,account_identity_observed_at
               ) VALUES($1,$2,$3,$4,'reserved',$5,$6,$7,$8,$9,$10,$11,$12,$13)
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
            account["pinned_account_jid"],
            account["pin_version"],
            observed_jid,
            observed_at,
        )
        stored = await connection.fetchrow(
            """SELECT session_name,conversation_id,supplier_contact_id,state,
                      post_worker,post_generation,post_lease_expires_at,
                      pinned_account_jid,account_pin_version
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
            or stored["pinned_account_jid"] != account["pinned_account_jid"]
            or int(stored["account_pin_version"] or 0) != int(account["pin_version"])
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
        # Budget/outbox row-lock waits must not let an old observation commit.
        if not timedelta(0) <= _now(now) - observed_at <= ACCOUNT_IDENTITY_OBSERVATION_MAX_AGE:
            raise WhatsAppSendDeferred(
                available_at=_now(now) + timedelta(minutes=1),
                reason="whatsapp_account_identity_observation_stale",
            )
        await connection.execute(
            """INSERT INTO whatsapp_account_control_events(
                   session_name,event_type,reason,actor_user_id,outbox_event_id,details
               ) VALUES($1,'send_reserved','final_post_reservation',NULL,$2,$3::jsonb)""",
            session_name,
            event.id,
            {
                "is_new_contact": is_new_contact,
                "generation": generation,
                "pinned_account_jid": expected_jid,
                "observed_account_jid": observed_jid,
                "pin_version": expected_pin_version,
                "observed_at": observed_at.isoformat(),
            },
        )
        return WhatsAppAccountReservation(
            outbox_event_id=event.id,
            session_name=session_name,
            supplier_contact_id=identity["supplier_contact_id"],
            is_new_contact=is_new_contact,
            generation=generation,
            lease_expires_at=lease_expires_at,
            pinned_account_jid=account["pinned_account_jid"],
            account_pin_version=int(account["pin_version"]),
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
        session_name = await connection.fetchval(
            "SELECT session_name FROM whatsapp_account_send_reservations WHERE outbox_event_id=$1",
            event_id,
        )
        if session_name is None:
            session_name = await connection.fetchval(
                """SELECT conversation.session_name FROM outbox_events event
                   JOIN supplier_conversations conversation
                     ON conversation.id::text=event.payload->>'conversation_id'
                   WHERE event.id=$1 AND event.event_type='whatsapp.send_text'""",
                event_id,
            )
        if session_name is None:
            raise ValueError("WhatsApp account reservation does not exist")
        await ensure_account(connection, session_name)
        account = await connection.fetchrow(
            "SELECT * FROM whatsapp_account_controls WHERE session_name=$1 FOR UPDATE",
            session_name,
        )
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

        if account is None:
            raise RuntimeError("WhatsApp account control row disappeared")
        old_state = reservation["state"]
        # A known accepted result cannot be downgraded by a later timeout.
        next_state = "accepted" if old_state == "accepted" else state
        accepted_value = (
            reservation["accepted_at"] or accepted if next_state == "accepted" else None
        )
        outcome_source = (
            source
            or {
                "accepted": "reconciliation" if state == "accepted" else "reservation",
                "unknown": "reservation",
                "rejected": "provider_rejection",
            }[state]
        )
        if outcome_source not in {
            "reservation",
            "http",
            "reconciliation",
            "reconstructed_native_trace",
            "operator_reconciliation",
            "provider_rejection",
            "migration",
        }:
            raise ValueError("invalid WhatsApp send outcome source")
        await connection.execute(
            """UPDATE whatsapp_account_send_reservations SET state=$2,
                   accepted_at=$3,outcome_source=$4,post_worker=NULL,post_generation=NULL,
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
            await _recover_circuit_if_resolved(connection, reservation["session_name"], current)


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
        """SELECT EXISTS(SELECT 1 FROM whatsapp_account_send_reservations reservation
           WHERE reservation.session_name=$1 AND (
               reservation.state='reserved'
               OR (reservation.state='unknown' AND NOT EXISTS (
                   SELECT 1 FROM whatsapp_legacy_unknown_quarantine_items item
                   WHERE item.outbox_event_id=reservation.outbox_event_id
               ))))""",
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
