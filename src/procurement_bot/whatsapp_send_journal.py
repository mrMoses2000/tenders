from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.errors import PermanentProviderError
from procurement_bot.providers.waha import WahaSendResult, normalize_chat_id
from procurement_bot.queue import ClaimedOutbox
from procurement_bot.whatsapp_delivery import parse_whatsapp_delivery_payload


def payload_sha256(
    *,
    session_name: str,
    external_chat_id: str,
    text: str,
    idempotency_key: str,
    provider_request_id: str,
) -> str:
    """Hash the exact send identity without copying phone or message text to logs."""
    identity = {
        "session": session_name,
        "chatId": external_chat_id,
        "text": text,
        "idempotencyKey": idempotency_key,
        "id": provider_request_id,
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expected_payload(event: ClaimedOutbox) -> tuple[Any, str]:
    payload = parse_whatsapp_delivery_payload(event.payload)
    return payload, normalize_chat_id(payload.recipient)


async def load_acceptance(
    connection: asyncpg.Connection,
    event: ClaimedOutbox,
) -> tuple[dict[str, Any], WahaSendResult] | None:
    row = await connection.fetchrow(
        "SELECT * FROM whatsapp_send_acceptance_journal WHERE outbox_event_id=$1",
        event.id,
    )
    if row is None:
        return None
    payload, chat_id = _expected_payload(event)
    expected_hash = payload_sha256(
        session_name=row["session_name"],
        external_chat_id=chat_id,
        text=payload.text,
        idempotency_key=event.idempotency_key,
        provider_request_id=event.provider_request_id or "",
    )
    if (
        row["idempotency_key"] != event.idempotency_key
        or row["external_chat_id"] != chat_id
        or row["provider_request_id"] != event.provider_request_id
        or row["payload_sha256"].strip() != expected_hash
    ):
        raise PermanentProviderError("WAHA send journal does not match the claimed outbox payload")
    result_identity = row["result_identity"]
    if (
        not isinstance(result_identity, dict)
        or result_identity.get("external_message_id") != row["external_message_id"]
        or result_identity.get("accepted") is not True
        or not isinstance(result_identity.get("provider_result"), dict)
    ):
        raise PermanentProviderError("WAHA send journal result is invalid")
    return row, WahaSendResult(
        external_message_id=row["external_message_id"],
        raw=result_identity["provider_result"],
    )


async def load_reconciliation_identity(
    connection: asyncpg.Connection,
    event: ClaimedOutbox,
) -> dict[str, Any] | None:
    payload, _expected_chat = _expected_payload(event)
    return await connection.fetchrow(
        "SELECT session_name,external_chat_id FROM supplier_conversations WHERE id=$1",
        payload.conversation_id,
    )


def exact_recovered_message(
    value: object,
    *,
    expected_chat_id: str,
    provider_request_id: str,
    expected_text: str,
) -> WahaSendResult | None:
    """Accept only an own WAHA message with exact reserved id, chat, and text."""
    if not isinstance(value, dict) or value.get("fromMe") is not True:
        return None
    expected_fallback_id = f"true_{expected_chat_id}_{provider_request_id}"
    raw_id = value.get("id")
    external_id: str | None = None
    if isinstance(raw_id, str):
        if raw_id in {provider_request_id, expected_fallback_id}:
            external_id = raw_id
    elif isinstance(raw_id, dict):
        for candidate in (raw_id.get("_serialized"), raw_id.get("id")):
            if isinstance(candidate, str) and candidate in {
                provider_request_id,
                expected_fallback_id,
            }:
                external_id = candidate
                break
    if external_id is None:
        return None
    chats = [value.get(key) for key in ("chatId", "to", "remote")]
    if isinstance(raw_id, dict):
        chats.append(raw_id.get("remote"))
    exact_chat_proven_by_full_id = external_id == expected_fallback_id
    if not exact_chat_proven_by_full_id and not any(chat == expected_chat_id for chat in chats):
        return None
    observed_text = value.get("body", value.get("text"))
    if not isinstance(observed_text, str) or observed_text != expected_text:
        return None
    return WahaSendResult(
        external_message_id=external_id,
        raw=value,
    )


async def persist_acceptance(
    connection: asyncpg.Connection,
    event: ClaimedOutbox,
    *,
    session_name: str,
    external_chat_id: str,
    provider_request_id: str,
    result: WahaSendResult,
    source: str,
    accepted_at: datetime | None = None,
) -> None:
    if source not in {"http", "reconciliation", "reconstructed_native_trace"}:
        raise ValueError("invalid WAHA acceptance source")
    if not session_name or not external_chat_id or not provider_request_id:
        raise ValueError("WAHA acceptance identity is incomplete")
    if source == "reconciliation" and result.external_message_id not in {
        provider_request_id,
        f"true_{external_chat_id}_{provider_request_id}",
    }:
        raise PermanentProviderError("reconciled WAHA message id differs from reservation")
    payload, expected_chat = _expected_payload(event)
    if external_chat_id != expected_chat:
        raise PermanentProviderError("WAHA acceptance chat differs from approved recipient")
    request_hash = payload_sha256(
        session_name=session_name,
        external_chat_id=external_chat_id,
        text=payload.text,
        idempotency_key=event.idempotency_key,
        provider_request_id=provider_request_id,
    )
    result_identity = {
        "external_message_id": result.external_message_id,
        "accepted": True,
        "provider_result": result.raw,
    }
    timestamp = accepted_at or datetime.now(UTC)
    claimed = await connection.fetchrow(
        """SELECT status,locked_by,lease_expires_at,idempotency_key,payload,
        provider_request_id FROM outbox_events WHERE id=$1 FOR UPDATE""",
        event.id,
    )
    if (
        claimed is None
        or claimed["status"] != "sending"
        or claimed["locked_by"] != event.locked_by
        or claimed["lease_expires_at"] <= datetime.now(UTC)
        or claimed["idempotency_key"] != event.idempotency_key
        or claimed["provider_request_id"] != provider_request_id
    ):
        raise RuntimeError("outbox lease changed before WAHA acceptance was journaled")
    stored_payload = parse_whatsapp_delivery_payload(claimed["payload"])
    if stored_payload != payload:
        raise PermanentProviderError("WAHA acceptance payload differs from persisted approval")
    await connection.execute(
        """INSERT INTO whatsapp_send_acceptance_journal(
            outbox_event_id,idempotency_key,session_name,external_chat_id,
            provider_request_id,payload_sha256,external_message_id,source,
            result_identity,accepted_at
        ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10)
        ON CONFLICT(outbox_event_id) DO NOTHING""",
        event.id,
        event.idempotency_key,
        session_name,
        external_chat_id,
        provider_request_id,
        request_hash,
        result.external_message_id,
        source,
        result_identity,
        timestamp,
    )
    persisted = await connection.fetchrow(
        "SELECT * FROM whatsapp_send_acceptance_journal WHERE outbox_event_id=$1",
        event.id,
    )
    if (
        persisted is None
        or persisted["idempotency_key"] != event.idempotency_key
        or persisted["session_name"] != session_name
        or persisted["external_chat_id"] != external_chat_id
        or persisted["provider_request_id"] != provider_request_id
        or persisted["payload_sha256"].strip() != request_hash
        or persisted["external_message_id"] != result.external_message_id
    ):
        raise PermanentProviderError("conflicting WAHA acceptance already exists")


async def mark_acceptance_unknown(
    connection: asyncpg.Connection,
    event: ClaimedOutbox,
    *,
    conversation_id,
) -> bool:
    changed = await connection.execute(
        """UPDATE supplier_conversations c SET
        waiting_reason='provider_acceptance_unknown',
        reply_due_at=COALESCE(c.reply_due_at,e.created_at+interval '24 hours'),
        updated_at=now()
        FROM outbox_events e WHERE c.id=$1 AND e.id=$2 AND e.status='sending'
          AND e.locked_by=$3 AND e.lease_expires_at>now()
          AND NOT c.opted_out AND c.state NOT IN ('complete','closed_no_reply','opted_out')""",
        conversation_id,
        event.id,
        event.locked_by,
    )
    if changed == "UPDATE 1":
        from procurement_bot.pipeline_reviews import record_review

        await record_review(
            connection, case_id=UUID(str(event.payload["case_id"])),
            kind="unknown_send", subject_id=event.id,
            conversation_id=conversation_id, reason_code="provider_acceptance_unknown",
        )
    return changed == "UPDATE 1"


def acceptance_unknown_due() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=30)
