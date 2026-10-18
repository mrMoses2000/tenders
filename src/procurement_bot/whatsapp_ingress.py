from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import asyncpg

from procurement_bot.phone import normalize_phone
from procurement_bot.supplier_dialogue import is_opt_out
from procurement_bot.supplier_media import WhatsAppMediaInput
from procurement_bot.whatsapp_inbound import (
    AuthenticatedWhatsAppInbound,
    WhatsAppInboundDisposition,
    WhatsAppInboundResult,
    accept_whatsapp_inbound,
)

IngressDisposition = Literal["accepted", "ignored", "rejected"]
_EVENT_KINDS = {"message", "message.ack", "session.status", "other", "unknown"}
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_OPEN_LID_QUARANTINE_ROWS = 5000
MAX_TOTAL_LID_QUARANTINE_ROWS = 10000


@dataclass(frozen=True, slots=True)
class AuthenticatedLidQuarantine:
    session_name: str
    provider_event_id: str
    sender_jid: str
    received_at: datetime
    message_kind: Literal["text", "image", "audio", "other"]
    text_content: str


@dataclass(frozen=True, slots=True)
class OpenLidQuarantineMessage:
    id: UUID
    session_name: str
    provider_event_id: str
    sender_jid: str
    received_at: datetime
    message_kind: str
    text_content: str


@dataclass(frozen=True, slots=True)
class VerifiedWahaLidMapping:
    """One entry returned by the documented WAHA session lids endpoint."""

    session_name: str
    lid: str
    pn: str
    source: Literal["waha_lids_api"] = "waha_lids_api"


@dataclass(frozen=True, slots=True)
class VerifiedQuarantinedMedia:
    """Media restored by fetching the exact provider event id from WAHA."""

    provider_event_id: str
    media: WhatsAppMediaInput


@dataclass(frozen=True, slots=True)
class LidQuarantineResolutionResult:
    quarantine_id: UUID
    disposition: str
    resolved: bool
    contact_id: UUID | None = None
    inbound_result: WhatsAppInboundResult | None = None


def provider_id_fingerprint(value: object) -> str | None:
    """Return a stable digest for a bounded provider id, never the id itself."""

    if not isinstance(value, str) or not value or len(value) > 500:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def event_kind(value: object) -> str:
    """Map attacker-controlled event labels onto a fixed, bounded vocabulary."""

    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().casefold()
    return normalized if normalized in _EVENT_KINDS - {"other", "unknown"} else "other"


async def persist_ingress_disposition(
    connection: asyncpg.Connection,
    *,
    disposition: IngressDisposition,
    reason_code: str,
    kind: str,
    provider_fingerprint: str | None = None,
    observed_at: datetime | None = None,
) -> None:
    """Count one ingress result in a fixed daily bucket without retaining payloads.

    The key dimensions are closed vocabularies. A single hashed provider id may
    be retained as a sample for each bucket; repeated distinct ids do not add
    rows or increase storage cardinality.
    """

    if disposition not in {"accepted", "ignored", "rejected"}:
        raise ValueError("unsupported ingress disposition")
    if not _REASON_CODE.fullmatch(reason_code):
        raise ValueError("reason_code must be a short machine-readable value")
    if kind not in _EVENT_KINDS:
        kind = "other"
    if provider_fingerprint is not None and not re.fullmatch(r"[0-9a-f]{64}", provider_fingerprint):
        raise ValueError("provider_fingerprint must be a SHA-256 digest")
    timestamp = observed_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    timestamp = timestamp.astimezone(UTC)
    await connection.execute(
        """
        INSERT INTO whatsapp_ingress_audit(
            utc_day,disposition,reason_code,event_kind,first_seen_at,last_seen_at,
            observation_count,sample_provider_id_fingerprint
        ) VALUES ($1,$2,$3,$4,$5,$5,1,$6)
        ON CONFLICT (utc_day,disposition,reason_code,event_kind) DO UPDATE
        SET first_seen_at=LEAST(whatsapp_ingress_audit.first_seen_at,EXCLUDED.first_seen_at),
            last_seen_at=GREATEST(whatsapp_ingress_audit.last_seen_at,EXCLUDED.last_seen_at),
            observation_count=LEAST(whatsapp_ingress_audit.observation_count+1,9223372036854775807),
            sample_provider_id_fingerprint=COALESCE(
                whatsapp_ingress_audit.sample_provider_id_fingerprint,
                EXCLUDED.sample_provider_id_fingerprint
            )
        """,
        timestamp.date(),
        disposition,
        reason_code,
        kind,
        timestamp,
        provider_fingerprint,
    )


async def persist_authenticated_lid_quarantine(
    connection: asyncpg.Connection,
    value: AuthenticatedLidQuarantine,
) -> bool:
    """Keep a verified-hop @lid message for later proof-based resolution.

    Only normalized message facts reach this function. Media URLs and arbitrary
    provider payload properties are excluded. Open-row count is capped under a
    transaction lock, and the unique provider fingerprint makes retries safe.
    """

    if not 1 <= len(value.session_name) <= 200:
        raise ValueError("session_name length is invalid")
    if not 1 <= len(value.provider_event_id) <= 500:
        raise ValueError("provider_event_id length is invalid")
    if not 1 <= len(value.sender_jid) <= 500 or not value.sender_jid.endswith("@lid"):
        raise ValueError("sender_jid is not an @lid identifier")
    if value.message_kind not in {"text", "image", "audio", "other"}:
        raise ValueError("message_kind is invalid")
    if len(value.text_content) > 100_000:
        raise ValueError("text_content is too long")
    if value.received_at.tzinfo is None or value.received_at.utcoffset() is None:
        raise ValueError("received_at must be timezone-aware")
    fingerprint = provider_id_fingerprint(value.provider_event_id)
    assert fingerprint is not None
    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            "whatsapp-inbound-quarantine-cap",
        )
        await connection.execute(
            """DELETE FROM whatsapp_inbound_quarantine
               WHERE status<>'open' AND created_at < now()-interval '60 days'"""
        )
        existing = await connection.fetchval(
            """SELECT EXISTS(SELECT 1 FROM whatsapp_inbound_quarantine
               WHERE session_name=$1 AND provider_id_fingerprint=$2)""",
            value.session_name,
            fingerprint,
        )
        if existing:
            return True
        open_count = await connection.fetchval(
            "SELECT count(*) FROM whatsapp_inbound_quarantine WHERE status='open'"
        )
        total_count = await connection.fetchval(
            "SELECT count(*) FROM whatsapp_inbound_quarantine"
        )
        if (
            int(open_count or 0) >= MAX_OPEN_LID_QUARANTINE_ROWS
            or int(total_count or 0) >= MAX_TOTAL_LID_QUARANTINE_ROWS
        ):
            return False
        await connection.execute(
            """INSERT INTO whatsapp_inbound_quarantine(
                session_name,provider_id_fingerprint,provider_event_id,sender_jid,
                received_at,message_kind,text_content
            ) VALUES ($1,$2,$3,$4,$5,$6,$7)""",
            value.session_name,
            fingerprint,
            value.provider_event_id,
            value.sender_jid,
            value.received_at.astimezone(UTC),
            value.message_kind,
            value.text_content,
        )
    return True


async def fetch_open_lid_quarantine(
    connection: asyncpg.Connection,
    *,
    limit: int = 100,
) -> tuple[OpenLidQuarantineMessage, ...]:
    """Fetch a bounded page for proof-based resolution and exact-id replay."""

    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    rows = await connection.fetch(
        """SELECT id,session_name,provider_event_id,sender_jid,received_at,
                  message_kind,text_content
           FROM whatsapp_inbound_quarantine WHERE status='open'
           ORDER BY created_at,id LIMIT $1""",
        limit,
    )
    return tuple(OpenLidQuarantineMessage(**dict(row)) for row in rows)


async def resolve_lid_quarantine(
    connection: asyncpg.Connection,
    quarantine_id: UUID,
    *,
    case_id: UUID | None = None,
    mappings: Sequence[VerifiedWahaLidMapping],
    mapping_snapshot_complete: bool,
    verified_at: datetime,
    media: VerifiedQuarantinedMedia | None = None,
) -> LidQuarantineResolutionResult:
    """Replay one @lid reply only with a unique official LID-to-phone proof.

    The caller must complete every page of an authenticated WAHA
    ``GET /api/{session}/lids`` snapshot and pass every exact-lid match. This
    helper performs no provider I/O.
    It checks for exactly one existing contact and exactly one currently
    eligible same-session conversation before entering the normal inbound
    idempotent path; ambiguous or missing context stays open for review.
    """

    if not mapping_snapshot_complete:
        return LidQuarantineResolutionResult(
            quarantine_id, "mapping_snapshot_incomplete", False
        )
    if len(mappings) > 1000:
        raise ValueError("verified mapping snapshot exceeds 1,000 rows")
    if verified_at.tzinfo is None or verified_at.utcoffset() is None:
        raise ValueError("verified_at must be timezone-aware")
    async with connection.transaction():
        row = await connection.fetchrow(
            """SELECT id,session_name,provider_event_id,sender_jid,received_at,
                      message_kind,text_content,status,resolved_contact_id
               FROM whatsapp_inbound_quarantine WHERE id=$1 FOR UPDATE""",
            quarantine_id,
        )
        if row is None:
            return LidQuarantineResolutionResult(quarantine_id, "not_found", False)
        if row["status"] != "open":
            return LidQuarantineResolutionResult(
                quarantine_id, "already_resolved", True,
                contact_id=row["resolved_contact_id"],
            )
        exact = [
            entry for entry in mappings
            if entry.session_name == row["session_name"]
            and entry.lid == row["sender_jid"]
        ]
        if len(exact) != 1 or exact[0].source != "waha_lids_api":
            return LidQuarantineResolutionResult(
                quarantine_id, "mapping_not_unique", False
            )
        pn = exact[0].pn
        if (
            not isinstance(pn, str)
            or len(pn) > 100
            or not pn.endswith("@c.us")
        ):
            return LidQuarantineResolutionResult(
                quarantine_id, "mapping_phone_invalid", False
            )
        try:
            normalized_phone = normalize_phone(pn.removesuffix("@c.us"))
        except ValueError:
            return LidQuarantineResolutionResult(
                quarantine_id, "mapping_phone_invalid", False
            )

        contacts = await connection.fetch(
            """SELECT id FROM supplier_contacts
               WHERE normalized_value=$1 AND active
                 AND contact_type IN ('phone','whatsapp')
               ORDER BY id LIMIT 2 FOR SHARE""",
            normalized_phone,
        )
        if len(contacts) != 1:
            return LidQuarantineResolutionResult(
                quarantine_id,
                "contact_not_unique" if contacts else "contact_not_found",
                False,
            )
        contact_id = contacts[0]["id"]
        opt_out = is_opt_out(row["text_content"])
        conversations = await connection.fetch(
            """SELECT c.id FROM supplier_conversations AS c
               JOIN procurement_cases AS procurement_case ON procurement_case.id=c.case_id
               WHERE c.supplier_contact_id=$1 AND c.session_name=$2
                 AND c.channel='whatsapp' AND NOT c.opted_out
                 AND c.state <> ALL($3::text[])
                 AND ($5::uuid IS NULL OR c.case_id=$5)
                 AND (procurement_case.status NOT IN ('closed','cancelled') OR $4::boolean)
                 AND (
                   $4::boolean
                   OR c.state NOT IN ('complete','closed_no_reply')
                   OR EXISTS (
                     SELECT 1 FROM supplier_conversation_targets AS target
                     JOIN offers AS offer ON offer.id=target.offer_id
                       AND offer.request_item_id=target.request_item_id
                       AND offer.supplier_id=target.supplier_id
                     WHERE target.conversation_id=c.id AND target.case_id=c.case_id
                       AND target.supplier_id=c.supplier_id
                       AND offer.status IN ('lead','confirm','exact','mismatch')
                   )
                 )
               ORDER BY c.updated_at DESC,c.id LIMIT 2 FOR UPDATE OF c""",
            contact_id,
            row["session_name"],
            ["opted_out"],
            opt_out,
            case_id,
        )
        if len(conversations) != 1:
            return LidQuarantineResolutionResult(
                quarantine_id,
                "conversation_not_unique" if conversations else "conversation_not_found",
                False,
                contact_id=contact_id,
            )

        message_kind = row["message_kind"]
        if message_kind in {"image", "audio"}:
            if (
                media is None
                or media.provider_event_id != row["provider_event_id"]
                or media.media.kind != message_kind
                or not media.media.url
                or media.media.error
            ):
                return LidQuarantineResolutionResult(
                    quarantine_id, "verified_media_required", False, contact_id=contact_id
                )
            media_input = media.media
        else:
            media_input = None
        event = AuthenticatedWhatsAppInbound(
            provider_event_id=row["provider_event_id"],
            session_name=row["session_name"],
            sender_phone=normalized_phone,
            provider_message_id=row["provider_event_id"],
            received_at=row["received_at"],
            text=row["text_content"],
            media=media_input,
        )
        inbound_result = await accept_whatsapp_inbound(
            connection, event, worker_id="waha-lid-quarantine-replay"
        )
        if inbound_result.disposition not in {
            WhatsAppInboundDisposition.PROCESSED,
            WhatsAppInboundDisposition.DUPLICATE,
        }:
            return LidQuarantineResolutionResult(
                quarantine_id,
                inbound_result.disposition.value,
                False,
                contact_id=contact_id,
                inbound_result=inbound_result,
            )
        mapping_fingerprint = hashlib.sha256(
            f"{row['session_name']}\0{row['sender_jid']}\0{pn}".encode()
        ).hexdigest()
        updated = await connection.execute(
            """UPDATE whatsapp_inbound_quarantine SET status='resolved',resolved_at=$2,
                   resolved_contact_id=$3,resolution_source='waha_lids_api',
                   mapping_fingerprint=$4,mapping_verified_at=$5
               WHERE id=$1 AND status='open'""",
            quarantine_id,
            datetime.now(UTC),
            contact_id,
            mapping_fingerprint,
            verified_at.astimezone(UTC),
        )
        if updated != "UPDATE 1":
            raise RuntimeError("LID quarantine status changed during replay")
        return LidQuarantineResolutionResult(
            quarantine_id,
            inbound_result.disposition.value,
            True,
            contact_id=contact_id,
            inbound_result=inbound_result,
        )
