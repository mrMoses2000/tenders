"""Bounded receive-only reconciliation independent of outbound permission."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from procurement_bot.waha_webhook_http import _lid_quarantine_from, parse_waha_message_outcome
from procurement_bot.whatsapp_account import (
    ensure_account,
    get_account_state,
    observe_provider_state,
)
from procurement_bot.whatsapp_inbound import AuthenticatedWhatsAppInbound, accept_whatsapp_inbound
from procurement_bot.whatsapp_ingress import (
    persist_authenticated_lid_quarantine,
    persist_ingress_disposition,
)


async def probe_account(pool, client) -> dict:
    """Reserve one probe window; unsupported restriction APIs remain unknown."""
    now = datetime.now(UTC)
    async with pool.acquire() as c, c.transaction():
        await ensure_account(c, client.session_name)
        due = await c.fetchval(
            "UPDATE whatsapp_account_controls SET "
            "next_probe_at=$2::timestamptz+interval '30 minutes',"
            "probe_requested_at=NULL WHERE session_name=$1 AND "
            "(next_probe_at<=$2 OR probe_requested_at IS NOT NULL) RETURNING session_name",
            client.session_name,
            now,
        )
    if due and hasattr(client, "get_account_restrictions"):
        try:
            observed = await client.get_account_restrictions()
            state = observed.get("state", "unknown")
            until = observed.get("restriction_until")
            until = datetime.fromtimestamp(until, UTC) if type(until) in (int, float) else None
        except Exception:
            state, until = "unavailable", None
        async with pool.acquire() as c:
            await observe_provider_state(
                c,
                client.session_name,
                state=state,
                observed_at=now,
                source="authenticated_provider_get",
                reason="scheduled_restriction_probe",
                restriction_until=until,
            )
    async with pool.acquire() as c:
        state = await get_account_state(c, client.session_name)
    return {
        key: state[key]
        for key in (
            "operator_held",
            "provider_state",
            "circuit_open",
            "outbound_allowed",
            "unresolved_reservations",
        )
    }


async def reconcile_reserved_sends(pool, client, case_id: UUID) -> int:
    """Selected reserved IDs use native journal/exact GET recovery, never POST."""
    from procurement_bot.outbox import WhatsAppOutboxWorker

    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT e.id FROM outbox_events e JOIN supplier_conversations c "
            "ON e.payload->>'conversation_id'=c.id::text "
            "WHERE c.case_id=$1 AND c.session_name=$2 AND e.event_type='whatsapp.send_text' "
            "AND e.provider_request_id IS NOT NULL AND e.status IN ('retry','dead') "
            "AND e.available_at<=now() ORDER BY e.available_at,e.id LIMIT 10",
            case_id,
            client.session_name,
        )
    worker = WhatsAppOutboxWorker(
        pool=pool, client=client, worker_id="reserved-outcome-reconciliation"
    )
    count = 0
    for row in rows:
        count += bool(await worker.run_once(event_id=row["id"]))
    return count


class _HistoryProofClient:
    """A receive-only client scoped to one already authenticated history fact."""

    def __init__(self, session_name: str, chat_id: str, message: dict) -> None:
        self.session_name = session_name
        self.chat_id = chat_id
        self.message = message

    async def fetch_message(self, chat_id: str, _message_id: str) -> dict:
        if chat_id != self.chat_id:
            raise ValueError("history proof chat changed")
        return self.message

    async def send_text(self, **_kwargs):
        raise RuntimeError("history recovery must never send")


async def reconcile_history_acceptance(pool, *, session_name: str, conversation_id: UUID,
                                       external_chat_id: str, message: dict) -> int:
    """Feed exact own-message evidence through the normal fenced outbox projection."""
    from procurement_bot.outbox import WhatsAppOutboxWorker
    from procurement_bot.whatsapp_delivery import parse_whatsapp_delivery_payload
    from procurement_bot.whatsapp_send_journal import exact_recovered_message

    if message.get("fromMe") is not True:
        return 0
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT e.id,e.provider_request_id,e.payload FROM outbox_events e "
            "JOIN supplier_conversations c ON e.payload->>'conversation_id'=c.id::text "
            "WHERE c.id=$1 AND c.session_name=$2 AND c.external_chat_id=$3 "
            "AND e.event_type='whatsapp.send_text' AND e.provider_request_id IS NOT NULL "
            "AND e.status IN ('retry','dead') AND e.available_at<=now() "
            "ORDER BY e.created_at,e.id LIMIT 100",
            conversation_id, session_name, external_chat_id,
        )
    for row in rows:
        payload = parse_whatsapp_delivery_payload(row["payload"])
        proof = exact_recovered_message(
            message, expected_chat_id=external_chat_id,
            provider_request_id=row["provider_request_id"], expected_text=payload.text,
        )
        if proof is None:
            continue
        worker = WhatsAppOutboxWorker(
            pool=pool,
            client=_HistoryProofClient(session_name, external_chat_id, message),
            worker_id="authenticated-history-acceptance",
        )
        await worker.run_once(event_id=row["id"])
        async with pool.acquire() as c:
            accepted = await c.fetchval(
                "SELECT EXISTS(SELECT 1 FROM whatsapp_send_acceptance_journal "
                "WHERE outbox_event_id=$1)", row["id"],
            )
        return int(accepted)
    return 0


async def reconcile_history(pool, client, case_id: UUID, *, limit: int = 10) -> dict:
    """One fixed-window page per chat per tick; immutable IDs deduplicate replay."""
    result = {"incoming": 0, "history_empty": 0, "history_unavailable": 0}
    readiness = ""
    if hasattr(client, "get_session_details"):
        try:
            details = await client.get_session_details()
            engine = details.get("engine")
            if isinstance(engine, dict):
                engine = engine.get("engine")
            store = details.get("config", {}).get("noweb", {}).get("store", {})
            if engine == "NOWEB" and store.get("enabled") is not True:
                readiness = "history_store_not_verified"
        except Exception:
            readiness = "history_readiness_unavailable"
    async with pool.acquire() as c:
        chats = await c.fetch(
            "SELECT id,external_chat_id,created_at,history_check_at,history_page_offset,"
            "history_window_start,history_window_end,history_page_fingerprint "
            "FROM supplier_conversations WHERE case_id=$1 AND session_name=$2 "
            "ORDER BY COALESCE(history_check_at,'-infinity'),id LIMIT $3",
            case_id,
            client.session_name,
            limit,
        )
    for chat in chats:
        error = readiness
        now = datetime.now(UTC)
        anchor = chat["created_at"] - timedelta(minutes=5)
        start = chat["history_window_start"] or anchor
        end = chat["history_window_end"] or now
        offset = chat["history_page_offset"]
        # A linked device may backfill old messages after an empty page. Keep
        # the bounded traversal anchored to this conversation's original span;
        # a completed/empty pass is not proof that earlier history is complete.
        # Preserve an in-progress page's fixed window until that pass finishes.
        if offset == 0:
            start = min(start, anchor)
        fingerprint = chat["history_page_fingerprint"]
        next_offset = offset
        try:
            if error:
                raise ValueError(error)
            page = await client.fetch_chat_messages(
                chat["external_chat_id"],
                limit=50,
                offset=offset,
                timestamp_gte=max(0, int(start.timestamp())),
                timestamp_lte=int(end.timestamp()),
                download_media=True,
            )
            page_fingerprint = hashlib.sha256(
                "|".join(str(m.get("id", "")) for m in page).encode()
            ).hexdigest()
            if page and offset and page_fingerprint == fingerprint:
                error = "history_page_repeated"
            elif offset >= 10_000:
                error = "history_page_limit_needs_review"
            else:
                for message in page:
                    if message.get("fromMe") is True:
                        recovered = await reconcile_history_acceptance(
                            pool, session_name=client.session_name, conversation_id=chat["id"],
                            external_chat_id=chat["external_chat_id"], message=message,
                        )
                        result["acceptances_recovered"] = (
                            result.get("acceptances_recovered", 0) + recovered
                        )
                    raw = {"event": "message", "session": client.session_name, "payload": message}
                    outcome = parse_waha_message_outcome(raw)
                    async with pool.acquire() as c, c.transaction():
                        if isinstance(outcome.event, AuthenticatedWhatsAppInbound):
                            accepted = await accept_whatsapp_inbound(
                                c, outcome.event, worker_id="history-reconciliation"
                            )
                            result["incoming"] += accepted.disposition.value == "processed"
                        elif outcome.reason == "unsupported_sender":
                            quarantine = _lid_quarantine_from(raw)
                            if quarantine:
                                saved = await persist_authenticated_lid_quarantine(c, quarantine)
                                if not saved:
                                    raise ValueError("history_lid_quarantine_capacity")
                        await persist_ingress_disposition(
                            c,
                            disposition=outcome.disposition,
                            reason_code=outcome.reason,
                            kind=outcome.event_kind,
                            provider_fingerprint=outcome.provider_id_fingerprint,
                        )
                if len(page) == 50:
                    next_offset = offset + 50
                    fingerprint = page_fingerprint
                else:
                    result["history_empty"] += not page
                    start, end, next_offset, fingerprint = anchor, None, 0, ""
        except Exception as exc:
            error = error or type(exc).__name__
        result["history_unavailable"] += bool(error)
        async with pool.acquire() as c:
            await c.execute(
                "UPDATE supplier_conversations SET history_check_at=$2,history_error_code=$3,"
                "history_window_start=$4,history_window_end=$5,history_page_offset=$6,"
                "history_page_fingerprint=$7 WHERE id=$1",
                chat["id"],
                now,
                error,
                start,
                end,
                next_offset,
                fingerprint,
            )
    return result


async def reconcile_lid_quarantine(pool, client, case_id: UUID) -> int:
    """Bounded verified provider snapshot; incomplete/ambiguous mappings stay private."""
    from procurement_bot.whatsapp_ingress import (
        OpenLidQuarantineMessage,
        VerifiedQuarantinedMedia,
        VerifiedWahaLidMapping,
        resolve_lid_quarantine,
    )

    if not hasattr(client, "fetch_lid_mappings"):
        return 0
    async with pool.acquire() as c:
        exists = await c.fetchval(
            "SELECT EXISTS(SELECT 1 FROM whatsapp_inbound_quarantine "
            "WHERE session_name=$1 AND status='open')",
            client.session_name,
        )
        phones = set(
            await c.fetchval(
                "SELECT array_agg(DISTINCT contact.normalized_value) "
                "FROM supplier_contacts contact JOIN supplier_conversations conv "
                "ON conv.supplier_contact_id=contact.id WHERE conv.case_id=$1 "
                "AND conv.session_name=$2 AND contact.active",
                case_id,
                client.session_name,
            )
            or []
        )
    if not exists or not phones:
        return 0
    mappings = []
    complete = False
    try:
        # WAHA LID snapshots can exceed three pages. Keep the traversal finite;
        # a full tenth page still means the snapshot is incomplete and cannot
        # authorize quarantine replay.
        for offset in range(0, 1000, 100):
            page = await client.fetch_lid_mappings(limit=100, offset=offset)
            mappings.extend(
                VerifiedWahaLidMapping(
                    session_name=client.session_name, lid=row.get("lid", ""), pn=row.get("pn", "")
                )
                for row in page
            )
            if len(page) < 100:
                complete = True
                break
    except Exception:
        return 0
    from procurement_bot.phone import normalize_phone

    lids = []
    for mapping in mappings:
        try:
            if (
                mapping.pn.endswith("@c.us")
                and normalize_phone(mapping.pn.removesuffix("@c.us")) in phones
            ):
                lids.append(mapping.lid)
        except (ValueError, AttributeError):
            pass
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id,session_name,provider_event_id,sender_jid,received_at,"
            "message_kind,text_content FROM whatsapp_inbound_quarantine "
            "WHERE status='open' AND session_name=$1 AND sender_jid=ANY($2::text[]) "
            "ORDER BY created_at,id LIMIT 50",
            client.session_name,
            lids,
        )
    pending = [OpenLidQuarantineMessage(**dict(row)) for row in rows]
    resolved = 0
    for row in pending:
        media = None
        if row.message_kind in {"image", "audio"}:
            exact_mapping = [entry for entry in mappings if entry.lid == row.sender_jid]
            if not complete or len(exact_mapping) != 1:
                continue

            try:
                pn = exact_mapping[0].pn
                if not isinstance(pn, str) or not pn.endswith("@c.us"):
                    continue
                phone = normalize_phone(pn.removesuffix("@c.us"))
                async with pool.acquire() as c:
                    scoped = await c.fetchval(
                        "SELECT count(DISTINCT conv.id) FROM supplier_conversations conv "
                        "JOIN supplier_contacts contact ON contact.id=conv.supplier_contact_id "
                        "WHERE conv.case_id=$1 AND conv.session_name=$2 "
                        "AND contact.normalized_value=$3 AND contact.active "
                        "AND NOT conv.opted_out",
                        case_id,
                        client.session_name,
                        phone,
                    )
                if scoped != 1:
                    continue
                raw = await client.fetch_message(pn, row.provider_event_id, download_media=True)
                if (
                    not raw
                    or raw.get("id") != row.provider_event_id
                    or raw.get("fromMe") is not False
                    or raw.get("from") not in {row.sender_jid, exact_mapping[0].pn}
                ):
                    continue
                outcome = parse_waha_message_outcome(
                    {
                        "event": "message",
                        "session": client.session_name,
                        "payload": {**raw, "from": exact_mapping[0].pn},
                    }
                )
                if not outcome.event or not outcome.event.media:
                    continue
                media = VerifiedQuarantinedMedia(row.provider_event_id, outcome.event.media)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Quarantined media lookup unavailable: %s", type(exc).__name__
                )
                continue
        async with pool.acquire() as c:
            result = await resolve_lid_quarantine(
                c,
                row.id,
                mappings=mappings,
                mapping_snapshot_complete=complete,
                verified_at=datetime.now(UTC),
                case_id=case_id,
                media=media,
            )
        resolved += result.resolved
    return resolved
