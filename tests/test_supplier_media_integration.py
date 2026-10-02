from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import asyncpg
import pytest
from aiohttp.test_utils import TestClient, TestServer

from procurement_bot.config import Settings
from procurement_bot.conversation_targets import ConversationTargetInput, attach_conversation_target
from procurement_bot.conversations import (
    ConversationInput,
    InboundMessageInput,
    WebhookEventInput,
    accept_webhook_event,
    create_conversation,
    record_inbound_message,
)
from procurement_bot.db import run_migrations
from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.mini_app import create_mini_app
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.quote_snapshots import get_current_quote_snapshot
from procurement_bot.semantic_supplier_reply import (
    load_semantic_reply_snapshot,
    validate_semantic_reply_proposal,
)
from procurement_bot.supplier_media_processing import process_supplier_media_payload
from procurement_bot.supplier_reply_processing import process_supplier_reply
from procurement_bot.suppliers import (
    OfferInput,
    SourceEvidenceInput,
    SupplierContactInput,
    SupplierInput,
    upsert_offer,
    upsert_source_evidence,
    upsert_supplier,
    upsert_supplier_contact,
)
from procurement_bot.transcription import Transcript
from procurement_bot.worker import JobWorker

ROOT = Path(__file__).parents[1]
_PNG_FIXTURE = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c636000020000050001a5f645400000000049454e44ae426082"
)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_mode",
    ["projection_rollback", "stale_lease", "retry", "terminal_success"],
)
async def test_terminal_media_failure_is_fenced_and_atomic(
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    admin, pool, schema = await _integration_pool()
    try:
        async with pool.acquire() as connection, connection.transaction():
            ids = await _seed_media_message(
                connection,
                kind="audio",
                dialogue_state="waiting_price",
                preferred_language="kk",
                original="[Дауыстық хабарлама]",
            )
            job_id = await enqueue_job(
                connection,
                kind="process_supplier_media",
                payload={
                    "conversation_id": str(ids["conversation_id"]),
                    "message_id": str(ids["message_id"]),
                },
                idempotency_key=f"terminal-media-test:{uuid4()}",
                max_attempts=2 if failure_mode == "retry" else 1,
            )

        worker = object.__new__(JobWorker)
        worker.pool = pool
        worker.worker_id = "terminal-media-test"
        worker.lease_seconds = 30

        async def dispatch(_self: JobWorker, _job) -> None:
            if failure_mode == "stale_lease":
                await pool.execute(
                    "UPDATE jobs SET status='retry',locked_at=NULL,locked_by=NULL,"
                    "lease_expires_at=NULL WHERE id=$1",
                    job_id,
                )
            if failure_mode == "retry":
                raise RetryableProviderError("temporary media failure")
            raise PermanentProviderError("permanent media failure")

        monkeypatch.setattr(JobWorker, "_dispatch", dispatch)
        if failure_mode == "projection_rollback":

            async def fail_reply_enqueue(*_args, **_kwargs):
                raise RuntimeError("simulated reply enqueue failure")

            monkeypatch.setattr("procurement_bot.worker.enqueue_job", fail_reply_enqueue)

        assert await worker.run_once() is True

        async with pool.acquire() as connection:
            job_status = await connection.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
            media_state = await connection.fetchval(
                "SELECT state FROM supplier_message_media WHERE message_id=$1",
                ids["message_id"],
            )
            reply_jobs = await connection.fetchval(
                "SELECT count(*) FROM jobs WHERE kind='process_supplier_reply' "
                "AND payload->>'message_id'=$1",
                str(ids["message_id"]),
            )
            reply_payload = await connection.fetchval(
                "SELECT payload FROM jobs WHERE kind='process_supplier_reply' "
                "AND payload->>'message_id'=$1",
                str(ids["message_id"]),
            )
            media_row = await connection.fetchrow(
                "SELECT state,needs_clarification,clarification_reason "
                "FROM supplier_message_media WHERE message_id=$1",
                ids["message_id"],
            )

        if failure_mode == "projection_rollback":
            assert job_status == "running"
            assert media_state == "downloaded"
            assert reply_jobs == 0
        elif failure_mode == "stale_lease":
            assert job_status == "retry"
            assert media_state == "downloaded"
            assert reply_jobs == 0
        elif failure_mode == "terminal_success":
            assert job_status == "dead"
            assert media_row["state"] == "failed"
            assert media_row["needs_clarification"] is True
            assert media_row["clarification_reason"] == "media_provider_failed"
            assert reply_jobs == 1
            assert reply_payload == {
                "conversation_id": str(ids["conversation_id"]),
                "message_id": str(ids["message_id"]),
            }
        else:
            assert job_status == "retry"
            assert media_state == "downloaded"
            assert reply_jobs == 0
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _signed_init_data(token: str, telegram_id: int) -> str:
    values = {
        "auth_date": str(int(datetime.now(UTC).timestamp())),
        "user": json.dumps({"id": telegram_id}),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


async def _integration_pool():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")
    schema = f"test_supplier_media_{uuid4().hex}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')

    async def init_connection(connection: asyncpg.Connection) -> None:
        for type_name in ("json", "jsonb"):
            await connection.set_type_codec(
                type_name,
                schema="pg_catalog",
                encoder=json.dumps,
                decoder=json.loads,
                format="text",
            )

    pool = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=3,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    await run_migrations(pool, ROOT / "migrations")
    return admin, pool, schema


async def _seed_media_message(
    connection: asyncpg.Connection,
    *,
    kind: str,
    dialogue_state: str,
    preferred_language: str,
    original: str,
) -> dict[str, Any]:
    owner_id = await connection.fetchval(
        "INSERT INTO users(telegram_id,display_name) VALUES ($1,'Media test owner') RETURNING id",
        int(uuid4().int % 2_000_000_000) + 1,
    )
    owner_telegram_id = await connection.fetchval(
        "SELECT telegram_id FROM users WHERE id=$1", owner_id
    )
    case_id = await connection.fetchval(
        "INSERT INTO procurement_cases(owner_user_id,title,city,status) "
        "VALUES ($1,'Media test','Қарағанды','evaluating') RETURNING id",
        owner_id,
    )
    item_id = await connection.fetchval(
        """INSERT INTO request_items(
               case_id,line_number,name,specification_text,quantity,unit,status
           ) VALUES ($1,1,'Қолғап','бір реттік, нитрил',10,'шт','researching') RETURNING id""",
        case_id,
    )
    await connection.execute(
        "INSERT INTO request_item_financials(request_item_id,tender_unit_price_private,currency) "
        "VALUES ($1,1000,'KZT')",
        item_id,
    )
    now = datetime.now(UTC)
    evidence = await upsert_source_evidence(
        connection,
        SourceEvidenceInput(
            source_type="manual",
            source_locator=f"manual:supplier-media:{uuid4()}",
            captured_at=now,
            content_sha256=hashlib.sha256(b"seed supplier evidence").hexdigest(),
            raw_payload={"test": True},
            idempotency_key=f"media-seed-evidence:{uuid4()}",
        ),
    )
    supplier = await upsert_supplier(
        connection,
        SupplierInput(
            display_name="Supplier Media Integration Test",
            identity_type="source_record",
            identity_value=f"media-test-{uuid4()}",
            source_evidence_id=evidence.id,
        ),
    )
    contact = await upsert_supplier_contact(
        connection,
        supplier.id,
        SupplierContactInput(
            contact_type="whatsapp",
            display_value=f"+7{uuid4().int % 10_000_000_000:010d}",
            source_evidence_id=evidence.id,
            is_whatsapp_confirmed=True,
        ),
    )
    offer = await upsert_offer(
        connection,
        OfferInput(
            request_item_id=item_id,
            supplier_id=supplier.id,
            idempotency_key=f"media-test-offer:{uuid4()}",
            supplier_product_name="Nitrile gloves",
            model_sku="GLOVE-MEDIA-TEST",
            status="exact",
        ),
    )
    session_name = f"media-test-{uuid4().hex[:8]}"
    external_chat_id = f"chat-{uuid4()}"
    conversation = await create_conversation(
        connection,
        ConversationInput(
            case_id=case_id,
            supplier_id=supplier.id,
            supplier_contact_id=contact.id,
            session_name=session_name,
            external_chat_id=external_chat_id,
            idempotency_key=f"media-test-conversation:{uuid4()}",
            preferred_language=preferred_language,
        ),
    )
    await connection.execute(
        "UPDATE supplier_conversations SET state=$2 WHERE id=$1",
        conversation.id,
        dialogue_state,
    )
    target = await attach_conversation_target(
        connection,
        ConversationTargetInput(
            conversation_id=conversation.id,
            offer_id=offer.id,
            request_item_id=item_id,
            provenance_kind="operator",
            provenance_ref="postgres integration test",
            idempotency_key=f"media-test-target:{uuid4()}",
            source_evidence_id=evidence.id,
        ),
    )
    external_message_id = f"media-{uuid4()}"
    webhook = await accept_webhook_event(
        connection,
        WebhookEventInput(
            session_name=session_name,
            event_name="message",
            external_event_id=external_message_id,
            raw_payload={"id": external_message_id, "kind": kind},
            idempotency_key=f"media-test-webhook:{uuid4()}",
            received_at=now,
        ),
    )
    message = await record_inbound_message(
        connection,
        InboundMessageInput(
            conversation_id=conversation.id,
            webhook_event_id=webhook.id,
            session_name=session_name,
            external_chat_id=external_chat_id,
            external_message_id=external_message_id,
            received_at=now,
            raw_payload={"id": external_message_id, "kind": kind},
            message_kind="audio" if kind == "audio" else "image",
            text_content=original,
        ),
    )
    media_bytes = _PNG_FIXTURE if kind == "image" else b"integration-media"
    object_suffix = ".png" if kind == "image" else ".ogg"
    mime_type = "image/png" if kind == "image" else "audio/ogg"
    await connection.execute(
        """INSERT INTO supplier_message_media(
               message_id,kind,state,object_key,sha256,byte_size,mime_type
           ) VALUES ($1,$2,'downloaded',$3,$4,$5,$6)""",
        message.id,
        kind,
        f"media/{message.id}{object_suffix}",
        hashlib.sha256(media_bytes).hexdigest(),
        len(media_bytes),
        mime_type,
    )
    return {
        "case_id": case_id,
        "conversation_id": conversation.id,
        "item_id": item_id,
        "message_id": message.id,
        "offer_id": offer.id,
        "owner_id": owner_id,
        "owner_telegram_id": owner_telegram_id,
        "supplier_id": supplier.id,
        "target_id": target.id,
        "source_evidence_id": evidence.id,
    }


class _Storage:
    def __init__(self, path: Path) -> None:
        self.path = path

    def resolve(self, _object_key: str) -> Path:
        return self.path


class _ConfidentTranscriber:
    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript
        self.submit_calls = 0
        self.poll_calls = 0

    async def submit(self, _path: Path, language: str = "auto") -> str:
        self.submit_calls += 1
        assert language == "auto"
        return "assemblyai-job-1"

    async def poll(self, request_id: str) -> Transcript:
        self.poll_calls += 1
        assert request_id == "assemblyai-job-1"
        return self.transcript


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("dialogue_state", ["waiting_price", "closed_no_reply"])
async def test_voice_media_projects_literal_kazakh_quote_once_and_keeps_original(
    tmp_path: Path, dialogue_state: str
) -> None:
    admin, pool, schema = await _integration_pool()
    media_path = tmp_path / "supplier.ogg"
    media_path.write_bytes(b"integration-media")
    transcript = Transcript(
        "Бағасы 500 теңге за дана. Бар 20 дана.",
        "kk",
        0.98,
        "assemblyai:universal-2:test",
        0.94,
        (),
    )
    provider = _ConfidentTranscriber(transcript)
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                ids = await _seed_media_message(
                    connection,
                    kind="audio",
                    dialogue_state=dialogue_state,
                    preferred_language="kk",
                    original="[Дауыстық хабарлама]",
                )
        payload = {
            "conversation_id": str(ids["conversation_id"]),
            "message_id": str(ids["message_id"]),
        }
        for _ in range(2):
            await process_supplier_media_payload(
                payload,
                pool=pool,
                storage=_Storage(media_path),
                waha=None,
                transcriber=provider,
                image_reader=None,
                audio_probe=None,
                kazakh_transcriber=None,
            )
        async with pool.acquire() as connection:
            snapshot = await load_semantic_reply_snapshot(
                connection,
                conversation_id=ids["conversation_id"],
                message_id=ids["message_id"],
            )
            assert snapshot.message_text == (
                "[Дауыстық хабарлама]\n\nБағасы 500 теңге за дана. Бар 20 дана."
            )
            proposal = validate_semantic_reply_proposal(
                snapshot,
                {
                    "context_sha256": snapshot.context_sha256,
                    "classification": "available",
                    "classification_quote": "Бар 20 дана.",
                    "target_offer_id": str(ids["offer_id"]),
                    "target_item_id": str(ids["item_id"]),
                    "facts": [
                        {
                            "offer_id": str(ids["offer_id"]),
                            "item_id": str(ids["item_id"]),
                            "field": "availability",
                            "value": "in_stock",
                            "evidence_quote": "Бар 20 дана.",
                        },
                        {
                            "offer_id": str(ids["offer_id"]),
                            "item_id": str(ids["item_id"]),
                            "field": "price",
                            "value": "500",
                            "evidence_quote": "500 теңге",
                        },
                        {
                            "offer_id": str(ids["offer_id"]),
                            "item_id": str(ids["item_id"]),
                            "field": "currency",
                            "value": "тенге",
                            "evidence_quote": "500 теңге",
                        },
                        {
                            "offer_id": str(ids["offer_id"]),
                            "item_id": str(ids["item_id"]),
                            "field": "price_unit",
                            "value": "дана",
                            "evidence_quote": "500 теңге за дана",
                        },
                        {
                            "offer_id": str(ids["offer_id"]),
                            "item_id": str(ids["item_id"]),
                            "field": "stock",
                            "value": "20",
                            "evidence_quote": "Бар 20 дана.",
                        },
                        {
                            "offer_id": str(ids["offer_id"]),
                            "item_id": str(ids["item_id"]),
                            "field": "stock_unit",
                            "value": "дана",
                            "evidence_quote": "Бар 20 дана.",
                        },
                    ],
                    "reason": "literal source-language quote",
                },
            )
            result = await process_supplier_reply(
                connection,
                conversation_id=ids["conversation_id"],
                message_id=ids["message_id"],
                semantic_snapshot=snapshot,
                semantic_proposal=proposal,
            )
            duplicate = await process_supplier_reply(
                connection,
                conversation_id=ids["conversation_id"],
                message_id=ids["message_id"],
                semantic_snapshot=snapshot,
                semantic_proposal=proposal,
            )
            quote = await get_current_quote_snapshot(connection, ids["offer_id"])
            assert quote is not None
            assert quote.request_item_id == ids["item_id"]
            assert quote.facts["price_amount"].value == Decimal("500.00")
            assert quote.facts["price_amount"].source_type == "supplier_message"
            assert quote.facts["price_unit"].value == "piece"
            assert quote.facts["available_qty"].value == Decimal("20")
            assert quote.facts["availability_status"].value == "in_stock"
            assert result.offer_id == ids["offer_id"]
            assert result.request_item_id == ids["item_id"]
            assert result.offer_observation_id is not None
            assert duplicate.newly_processed is False
            assert duplicate.offer_id == ids["offer_id"]
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM offer_observations WHERE offer_id=$1", ids["offer_id"]
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM workflow_events WHERE idempotency_key=$1",
                    stable_key("supplier-reply-processed", ids["message_id"]),
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT text_content FROM supplier_conversation_messages WHERE id=$1",
                    ids["message_id"],
                )
                == "[Дауыстық хабарлама]"
            )
            state = await connection.fetchval(
                "SELECT state FROM supplier_conversations WHERE id=$1",
                ids["conversation_id"],
            )
            assert state == ("complete" if dialogue_state == "waiting_price" else dialogue_state)
            assert provider.submit_calls == 1
            assert provider.poll_calls == 1
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM jobs WHERE idempotency_key=$1",
                    stable_key("supplier-media-reply", ids["message_id"]),
                )
                == 1
            )
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("dialogue_state", ["waiting_price", "closed_no_reply"])
async def test_photo_reading_requests_text_and_never_projects_price(
    tmp_path: Path, dialogue_state: str
) -> None:
    admin, pool, schema = await _integration_pool()
    media_path = tmp_path / "supplier.jpg"
    media_path.write_bytes(b"integration-media")
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                ids = await _seed_media_message(
                    connection,
                    kind="image",
                    dialogue_state=dialogue_state,
                    preferred_language="ru",
                    original="[Фотография поставщика]",
                )
                if dialogue_state == "waiting_price":
                    second_item_id = await connection.fetchval(
                        """INSERT INTO request_items(
                               case_id,line_number,name,specification_text,quantity,unit,status
                           ) VALUES ($1,2,'Маска','медицинская',10,'шт','researching')
                           RETURNING id""",
                        ids["case_id"],
                    )
                    second_offer = await upsert_offer(
                        connection,
                        OfferInput(
                            request_item_id=second_item_id,
                            supplier_id=ids["supplier_id"],
                            idempotency_key=f"media-test-second-offer:{uuid4()}",
                            supplier_product_name="Medical mask",
                            model_sku="MASK-MEDIA-TEST",
                            status="exact",
                        ),
                    )
                    second_target = await attach_conversation_target(
                        connection,
                        ConversationTargetInput(
                            conversation_id=ids["conversation_id"],
                            offer_id=second_offer.id,
                            request_item_id=second_item_id,
                            provenance_kind="operator",
                            provenance_ref="pending media clarification item turn",
                            idempotency_key=f"media-test-second-target:{uuid4()}",
                            source_evidence_id=ids["source_evidence_id"],
                        ),
                    )
                    await connection.execute(
                        """INSERT INTO supplier_conversation_item_turns(
                               conversation_id,target_id,turn_no,status,draft_text
                           ) VALUES ($1,$2,1,'pending_reply','Тауар туралы сұрақ')""",
                        ids["conversation_id"],
                        second_target.id,
                    )
                    ids["second_item_id"] = second_item_id
                    ids["second_offer_id"] = second_offer.id
                    ids["second_target_id"] = second_target.id

        class ImageReader:
            async def read(self, _path: Path):
                from procurement_bot.providers.supplier_image import SupplierImageReading

                return SupplierImageReading(
                    readable_text="Цена 500 тг за штуку",
                    observations="Текст на упаковке",
                    language="ru",
                    confidence=0.99,
                    uncertain_numbers=False,
                )

        payload = {
            "conversation_id": str(ids["conversation_id"]),
            "message_id": str(ids["message_id"]),
        }
        await process_supplier_media_payload(
            payload,
            pool=pool,
            storage=_Storage(media_path),
            waha=None,
            transcriber=None,
            image_reader=ImageReader(),
            audio_probe=None,
        )
        async with pool.acquire() as connection:
            semantic_snapshot = await load_semantic_reply_snapshot(
                connection,
                conversation_id=ids["conversation_id"],
                message_id=ids["message_id"],
            )
            result = await process_supplier_reply(
                connection,
                conversation_id=ids["conversation_id"],
                message_id=ids["message_id"],
                semantic_snapshot=semantic_snapshot,
                semantic_proposal_error="supplier media requires text confirmation",
            )
            if dialogue_state == "waiting_price":
                assert result.offer_id == ids["second_offer_id"]
                assert result.request_item_id == ids["second_item_id"]
                turn = await connection.fetchrow(
                    "SELECT status,inbound_message_id FROM supplier_conversation_item_turns "
                    "WHERE conversation_id=$1",
                    ids["conversation_id"],
                )
                assert turn["status"] == "ambiguous"
                assert turn["inbound_message_id"] == ids["message_id"]
            else:
                assert result.offer_id is None
            assert result.offer_observation_id is None
            if dialogue_state == "closed_no_reply":
                assert result.needs_human is True
                assert result.draft_outbound_text is None
            else:
                assert result.draft_outbound_text is not None
                assert "валюту" in result.draft_outbound_text
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM offer_observations WHERE offer_id=ANY($1::uuid[])",
                    [ids["offer_id"], ids.get("second_offer_id", ids["offer_id"])],
                )
                == 0
            )
            media_row = await connection.fetchrow(
                "SELECT state,needs_clarification,extracted_text FROM supplier_message_media "
                "WHERE message_id=$1",
                ids["message_id"],
            )
            assert media_row["state"] == "ready"
            assert media_row["needs_clarification"] is True
            assert media_row["extracted_text"] == "Цена 500 тг за штуку"
            assert (
                await connection.fetchval(
                    "SELECT text_content FROM supplier_conversation_messages WHERE id=$1",
                    ids["message_id"],
                )
                == "[Фотография поставщика]"
            )
            media_root = tmp_path / "media-root"
            object_path = media_root / f"media/{ids['message_id']}.png"
            object_path.parent.mkdir(parents=True)
            object_path.write_bytes(_PNG_FIXTURE)
            other_telegram_id = int(uuid4().int % 2_000_000_000) + 1
            await connection.execute(
                "INSERT INTO users(telegram_id,display_name) VALUES ($1,'Other owner')",
                other_telegram_id,
            )
        credential = uuid4().hex
        settings = Settings(
            telegram_bot_token=credential,
            telegram_allowed_user_ids=(f"{ids['owner_telegram_id']},{other_telegram_id}"),
            media_root=media_root,
        )
        app = create_mini_app(pool, settings)
        async with TestClient(TestServer(app)) as client:
            path = f"/api/supplier-messages/{ids['message_id']}/media"
            owner_response = await client.get(
                path,
                headers={
                    "Authorization": "tma "
                    + _signed_init_data(credential, ids["owner_telegram_id"])
                },
            )
            assert owner_response.status == 200
            assert owner_response.content_type == "image/png"
            assert await owner_response.read() == _PNG_FIXTURE
            other_response = await client.get(
                path,
                headers={
                    "Authorization": "tma " + _signed_init_data(credential, other_telegram_id)
                },
            )
            assert other_response.status == 404
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unknown_voice_fallback_projects_kazakh_opt_out_without_followup(
    tmp_path: Path,
) -> None:
    admin, pool, schema = await _integration_pool()
    media_path = tmp_path / "supplier.ogg"
    media_path.write_bytes(b"integration-media")
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                ids = await _seed_media_message(
                    connection,
                    kind="audio",
                    dialogue_state="waiting_price",
                    preferred_language="kk",
                    original="[Дауыстық хабарлама]",
                )

        class UnknownTranscriber(_ConfidentTranscriber):
            async def poll(self, request_id: str) -> Transcript:
                self.poll_calls += 1
                assert request_id == "assemblyai-job-1"
                return Transcript("", "unknown", None, "assemblyai:universal-2")

        class KazakhFallback:
            calls = 0

            async def transcribe(self, path: Path, language: str = "auto") -> Transcript:
                self.calls += 1
                assert path == media_path
                assert language == "auto"
                return Transcript(
                    "Маған енді хабарласпаңыз.",
                    "kk",
                    None,
                    "agy:gemini-3.8-flash-high",
                )

        primary = UnknownTranscriber(Transcript("", "unknown", None, "assemblyai"))
        fallback = KazakhFallback()
        payload = {
            "conversation_id": str(ids["conversation_id"]),
            "message_id": str(ids["message_id"]),
        }
        await process_supplier_media_payload(
            payload,
            pool=pool,
            storage=_Storage(media_path),
            waha=None,
            transcriber=primary,
            image_reader=None,
            audio_probe=None,
            kazakh_transcriber=fallback,
        )
        async with pool.acquire() as connection:
            media_row = await connection.fetchrow(
                "SELECT state,language,extracted_text,needs_clarification FROM "
                "supplier_message_media WHERE message_id=$1",
                ids["message_id"],
            )
            assert media_row["state"] == "ready"
            assert media_row["language"] == "kk"
            assert media_row["extracted_text"] == "Маған енді хабарласпаңыз."
            assert media_row["needs_clarification"] is True
            result = await process_supplier_reply(
                connection,
                conversation_id=ids["conversation_id"],
                message_id=ids["message_id"],
            )
            assert result.classification.value == "opt_out"
            assert (
                await connection.fetchval(
                    "SELECT opted_out FROM supplier_conversations WHERE id=$1",
                    ids["conversation_id"],
                )
                is True
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM outbox_events WHERE event_type='whatsapp.send_text'"
                )
                == 0
            )
            assert fallback.calls == 1
            assert primary.submit_calls == 1
            assert primary.poll_calls == 1
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
