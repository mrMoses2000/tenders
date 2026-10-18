from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from procurement_bot.supplier_media import WhatsAppMediaInput
from procurement_bot.whatsapp_inbound import WhatsAppInboundDisposition, WhatsAppInboundResult
from procurement_bot.whatsapp_ingress import (
    LidQuarantineResolutionResult,
    VerifiedQuarantinedMedia,
    VerifiedWahaLidMapping,
    event_kind,
    persist_ingress_disposition,
    provider_id_fingerprint,
    resolve_lid_quarantine,
)


class _Connection:
    def __init__(self) -> None:
        self.call: tuple[str, tuple[object, ...]] | None = None

    async def execute(self, sql: str, *args: object) -> str:
        self.call = (sql, args)
        return "INSERT 0 1"


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _ReplayConnection:
    def __init__(self) -> None:
        self.quarantine_id = uuid4()
        self.contact_id = uuid4()
        self.row = {
            "id": self.quarantine_id,
            "session_name": "procurement",
            "provider_event_id": "provider-event-9",
            "sender_jid": "opaque@lid",
            "received_at": datetime(2026, 10, 6, 12, tzinfo=UTC),
            "message_kind": "text",
            "text_content": "Есть на складе",
            "status": "open",
        }
        self.contact_rows = [{"id": self.contact_id}]
        self.conversation_rows = [{"id": uuid4()}]
        self.conversation_args: tuple[object, ...] = ()
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, sql: str, *_args: object) -> dict[str, object] | None:
        assert "whatsapp_inbound_quarantine" in sql
        return self.row

    async def fetch(self, sql: str, *_args: object) -> list[dict[str, object]]:
        if "FROM supplier_contacts" in sql:
            return self.contact_rows
        if "FROM supplier_conversations" in sql:
            self.conversation_args = _args
            return self.conversation_rows
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: object) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"


def test_fingerprint_is_stable_and_only_accepts_bounded_provider_ids() -> None:
    assert provider_id_fingerprint("provider-id") == provider_id_fingerprint("provider-id")
    assert len(provider_id_fingerprint("provider-id") or "") == 64
    assert provider_id_fingerprint("x" * 501) is None
    assert provider_id_fingerprint(42) is None


def test_event_kind_maps_untrusted_values_to_fixed_vocabulary() -> None:
    assert event_kind("message") == "message"
    assert event_kind("SESSION.STATUS") == "session.status"
    assert event_kind("a" * 10000) == "other"
    assert event_kind(None) == "unknown"


@pytest.mark.asyncio
async def test_audit_persists_only_fixed_dimensions_timestamps_and_digest() -> None:
    connection = _Connection()
    timestamp = datetime(2026, 10, 6, 12, 34, tzinfo=UTC)
    fingerprint = provider_id_fingerprint("provider-id")

    await persist_ingress_disposition(
        connection,  # type: ignore[arg-type]
        disposition="ignored",
        reason_code="unsupported_sender",
        kind="message",
        provider_fingerprint=fingerprint,
        observed_at=timestamp,
    )

    assert connection.call is not None
    sql, args = connection.call
    assert "ON CONFLICT (utc_day,disposition,reason_code,event_kind) DO UPDATE" in sql
    assert args == (
        timestamp.date(), "ignored", "unsupported_sender", "message", timestamp, fingerprint
    )
    assert "@" not in repr(args)
    assert "provider-id" not in repr(args)


def test_migration_uses_daily_aggregate_dimensions_without_raw_payload() -> None:
    migration = Path(__file__).parents[1] / "migrations" / "039_whatsapp_ingress_audit.sql"
    sql = migration.read_text(encoding="utf-8")

    assert "PRIMARY KEY (utc_day, disposition, reason_code, event_kind)" in sql
    assert "observation_count BIGINT" in sql
    assert "sample_provider_id_fingerprint" in sql
    assert "raw_payload" not in sql
    assert "phone" not in sql


@pytest.mark.asyncio
async def test_lid_quarantine_replay_requires_unique_complete_verified_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import procurement_bot.whatsapp_ingress as ingress

    connection = _ReplayConnection()
    accepted: list[object] = []

    async def accept(_connection, event, *, worker_id):
        accepted.append((event, worker_id))
        return WhatsAppInboundResult(
            webhook_event_id=uuid4(),
            disposition=WhatsAppInboundDisposition.PROCESSED,
        )

    monkeypatch.setattr(ingress, "accept_whatsapp_inbound", accept)
    mapping = VerifiedWahaLidMapping(
        session_name="procurement", lid="opaque@lid", pn="77000000003@c.us"
    )
    case_id = uuid4()

    incomplete = await resolve_lid_quarantine(
        connection, connection.quarantine_id,
        case_id=case_id,
        mappings=(mapping,), mapping_snapshot_complete=False,
        verified_at=datetime.now(UTC),
    )
    assert incomplete.disposition == "mapping_snapshot_incomplete"
    assert accepted == []

    ambiguous = await resolve_lid_quarantine(
        connection, connection.quarantine_id,
        case_id=case_id,
        mappings=(mapping, mapping), mapping_snapshot_complete=True,
        verified_at=datetime.now(UTC),
    )
    assert ambiguous.disposition == "mapping_not_unique"
    assert accepted == []

    resolved = await resolve_lid_quarantine(
        connection, connection.quarantine_id,
        case_id=case_id,
        mappings=(mapping,), mapping_snapshot_complete=True,
        verified_at=datetime(2026, 10, 6, 13, tzinfo=UTC),
    )

    assert isinstance(resolved, LidQuarantineResolutionResult)
    assert resolved.resolved is True
    assert resolved.disposition == "processed"
    assert resolved.contact_id == connection.contact_id
    event, worker_id = accepted[0]  # type: ignore[misc]
    assert event.sender_phone == "+77000000003"
    assert event.provider_event_id == "provider-event-9"
    assert event.text == "Есть на складе"
    assert worker_id == "waha-lid-quarantine-replay"
    update_sql, update_args = connection.executed[-1]
    assert "resolution_source='waha_lids_api'" in update_sql
    assert update_args[0] == connection.quarantine_id
    assert update_args[2] == connection.contact_id
    assert len(update_args[3]) == 64  # mapping fingerprint only; PN not copied to proof row
    assert update_args[4] == datetime(2026, 10, 6, 13, tzinfo=UTC)
    assert connection.conversation_args[-1] == case_id


@pytest.mark.asyncio
async def test_lid_media_replay_requires_exact_provider_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import procurement_bot.whatsapp_ingress as ingress

    connection = _ReplayConnection()
    connection.row["message_kind"] = "image"
    accepted: list[object] = []

    async def accept(_connection, event, *, worker_id):
        accepted.append(event)
        return WhatsAppInboundResult(
            webhook_event_id=uuid4(),
            disposition=WhatsAppInboundDisposition.PROCESSED,
        )

    monkeypatch.setattr(ingress, "accept_whatsapp_inbound", accept)
    mapping = VerifiedWahaLidMapping(
        session_name="procurement", lid="opaque@lid", pn="77000000003@c.us"
    )
    case_id = uuid4()
    media = WhatsAppMediaInput(kind="image", url="https://provider.invalid/restored")

    wrong_event = await resolve_lid_quarantine(
        connection, connection.quarantine_id,
        case_id=case_id,
        mappings=(mapping,), mapping_snapshot_complete=True,
        verified_at=datetime.now(UTC),
        media=VerifiedQuarantinedMedia("different-event", media),
    )
    assert wrong_event.disposition == "verified_media_required"
    assert accepted == []

    correct_event = await resolve_lid_quarantine(
        connection, connection.quarantine_id,
        case_id=case_id,
        mappings=(mapping,), mapping_snapshot_complete=True,
        verified_at=datetime.now(UTC),
        media=VerifiedQuarantinedMedia("provider-event-9", media),
    )
    assert correct_event.resolved is True
    assert accepted[0].media == media  # type: ignore[union-attr]
