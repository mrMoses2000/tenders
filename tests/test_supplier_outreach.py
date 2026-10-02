import hashlib
import json
import os
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.auto_send_policy import set_case_auto_send_policy
from procurement_bot.conversation_targets import ConversationTargetInput, attach_conversation_target
from procurement_bot.db import run_migrations
from procurement_bot.intake import SearchScope
from procurement_bot.queue import ClaimedOutbox
from procurement_bot.research_scope import append_citywide_scope_revision
from procurement_bot.supplier_dialogue import DEFAULT_SEND_POLICY
from procurement_bot.supplier_outreach import (
    _quantity,
    prepare_supplier_outreach,
    product_label,
    request_next_supplier_turn_approval,
)
from procurement_bot.whatsapp_approval_workflow import start_approved_whatsapp_send
from procurement_bot.whatsapp_delivery import assert_whatsapp_delivery_allowed


def test_supplier_quantity_is_readable() -> None:
    assert _quantity(Decimal("40.000000"), "Штука") == "40 шт."
    assert _quantity(Decimal("1.000000"), "кг") == "1 кг"


def test_generic_catalog_name_uses_short_product_identity_from_spec() -> None:
    spec = (
        "Плита для потолка АРМСТРОНГ. Потолочная панель декоративная "
        "тепло- и звукоизоляционная, 60 x 60 см. По согласованию с заказчиком"
    )
    assert product_label("Плитка", spec) == "потолочная панель Armstrong"


def test_pilot_messages_name_actual_products_from_spec() -> None:
    assert product_label("Пакля", "Лён сантехнический, коса 1кг.") == "Лён сантехнический"
    assert product_label("Доводчик дверной", "Петля с доводчиком для ДСП, серебряный") == (
        "Петля с доводчиком для ДСП"
    )
    assert (
        product_label(
            "Машина стационарная для сварки", "Аппарат для сварки пластиковых труб 2000 Вт."
        )
        == "Аппарат для сварки пластиковых труб 2000 Вт"
    )


def test_product_label_never_copies_a_long_specification() -> None:
    label = product_label(
        "Кабель",
        "Кабель силовой медный для внутренней прокладки, сечение 3х2.5 мм2, "
        "изоляция ПВХ, цвет белый, напряжение 220В, сертификат соответствия",
    )
    assert len(label) <= 72
    assert "сечение" not in label
    assert "сертификат" not in label


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _NoWhatsAppConnection:
    def __init__(self) -> None:
        self.owner_id = uuid4()
        self.events: list[tuple[object, ...]] = []
        self.operations: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, sql: str, *_args: object):
        if "FROM procurement_cases" in sql:
            self.operations.append("case")
            return {
                "owner_user_id": self.owner_id,
                "telegram_id": 123,
                "status": "evaluating",
                "city": "Алматы",
                "search_area_text": "",
                "latest_run_id": None,
            }
        raise AssertionError(sql)

    async def fetch(self, sql: str, *_args: object):
        if "FROM offers AS offer" in sql:
            self.operations.append("candidates")
        return []

        async def fetch(self, sql: str, *_args: object):
            assert "contact.contact_type='whatsapp'" in sql
            assert "source_evidence" in sql
            assert "research_runs" in sql
            assert "offer_observations" in sql
            assert "contact_evidence_ids" in sql
            return []

    async def fetchval(self, sql: str, *_args: object):
        assert "count(DISTINCT offer.supplier_id)" in sql
        return 3

    async def execute(self, sql: str, *args: object) -> str:
        if "pg_advisory_xact_lock" in sql:
            self.operations.append("case-lock")
            return "SELECT 1"
        assert "supplier_outreach_prepared" in sql
        self.operations.append("audit")
        self.events.append(args)
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_outreach_does_not_treat_plain_phone_as_whatsapp() -> None:
    connection = _NoWhatsAppConnection()
    case_id = uuid4()

    result = await prepare_supplier_outreach(
        connection,  # type: ignore[arg-type]
        case_id=case_id,
        session_name="default",
    )

    assert result.proposals_created == 0
    assert result.suppliers_without_whatsapp == 3
    assert len(connection.events) == 1
    assert connection.operations.index("case-lock") < connection.operations.index("candidates")


@pytest.mark.asyncio
async def test_specific_area_outreach_skips_supplier_without_address_match() -> None:
    class _UnknownAreaConnection(_NoWhatsAppConnection):
        async def fetchrow(self, sql: str, *_args: object):
            if "FROM procurement_cases" in sql:
                self.operations.append("case")
                return {
                    "owner_user_id": self.owner_id,
                    "telegram_id": 123,
                    "status": "evaluating",
                    "city": "Алматы",
                    "search_area_text": "Северное Кольцо",
                    "latest_run_id": None,
                }
            raise AssertionError(sql)

        async def fetch(self, sql: str, *_args: object):
            if "FROM offers AS offer" in sql:
                self.operations.append("candidates")
                return [{"location_city": "Алматы", "address_text": None}]
            return []

    connection = _UnknownAreaConnection()
    result = await prepare_supplier_outreach(
        connection,  # type: ignore[arg-type]
        case_id=uuid4(),
        session_name="default",
    )

    assert result.proposals_created == 0
    assert connection.operations.count("candidates") == 1


@pytest.mark.asyncio
async def test_outreach_preserves_existing_manual_conversation() -> None:
    class _ManualConversationConnection(_NoWhatsAppConnection):
        def __init__(self) -> None:
            super().__init__()
            self.supplier_id = uuid4()
            self.contact_id = uuid4()
            self.offer_id = uuid4()

        async def fetch(self, sql: str, *_args: object):
            assert "contact.contact_type='whatsapp'" in sql
            return [
                {
                    "supplier_id": self.supplier_id,
                    "contact_id": self.contact_id,
                    "offer_id": self.offer_id,
                    "normalized_value": "+77085766841",
                }
            ]

        async def fetchrow(self, sql: str, *_args: object):
            if "FROM supplier_conversations" in sql:
                return {"idempotency_key": "owner-test-dialogue"}
            return await super().fetchrow(sql, *_args)

        async def fetchval(self, sql: str, *_args: object):
            assert "count(DISTINCT offer.supplier_id)" in sql
            return 1

    connection = _ManualConversationConnection()
    result = await prepare_supplier_outreach(
        connection,  # type: ignore[arg-type]
        case_id=uuid4(),
        session_name="default",
    )

    assert result.proposals_created == 0
    assert result.skipped_existing == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_citywide_scope_revision_allows_outreach_outside_preferred_area() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_citywide_outreach_{uuid4().hex}"
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
        max_size=4,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        owner_id, case_id, supplier_id = uuid4(), uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,$3)",
            owner_id,
            720_000_000 + int(owner_id.hex[:8], 16),
            "Citywide outreach test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,search_area_text,status) "
            "VALUES ($1,$2,$3,'Алматы','Северное Кольцо','evaluating')",
            case_id,
            owner_id,
            "Citywide search with a preferred area",
        )
        plan = {
            "city": "Алматы",
            "scope": SearchScope.SPECIFIC_AREA.value,
            "locality_resolution": None,
            "locality_task": None,
            "source_plans": [
                {
                    "locality": {
                        "city": "Алматы",
                        "scope": SearchScope.SPECIFIC_AREA.value,
                        "raw_area_text": "Северное Кольцо",
                        "canonical_place_id": None,
                        "canonical_place_name": None,
                        "point": None,
                    },
                }
            ],
        }
        plan_sha256 = hashlib.sha256(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        run_id = await pool.fetchval(
            "INSERT INTO research_runs(case_id,search_scope,idempotency_key) "
            "VALUES ($1,$2,$3) RETURNING id",
            case_id,
            {"contract_version": 1, "plan_sha256": plan_sha256, "plan": plan},
            f"citywide-outreach-run:{case_id}",
        )
        item_id = await pool.fetchval(
            "INSERT INTO request_items(case_id,line_number,name,specification_text,quantity,unit) "
            "VALUES ($1,1,'Кисть','Кисть шириной 80 мм',10,'шт') RETURNING id",
            case_id,
        )
        group_id = await pool.fetchval(
            "INSERT INTO research_item_groups(research_run_id,request_item_id,status) "
            "VALUES ($1,$2,'paused') RETURNING id",
            run_id,
            item_id,
        )
        lanes = await pool.fetch(
            "INSERT INTO research_lanes(group_id,lane_no,status) "
            "SELECT $1,n,'paused' FROM generate_series(0,3) n RETURNING id",
            group_id,
        )
        async with pool.acquire() as connection:
            await append_citywide_scope_revision(
                connection,
                run_id,
                owner_id,
                f"owner-authorization:{case_id}",
                "Северное Кольцо в приоритете",
            )
        query_id = await pool.fetchval(
            "INSERT INTO research_queries(research_run_id,provider,query_text,idempotency_key) "
            "VALUES ($1,'manual','supplier contact',$2) RETURNING id",
            run_id,
            f"citywide-outreach-query:{case_id}",
        )
        evidence_id = await pool.fetchval(
            "INSERT INTO source_evidence(research_query_id,source_type,source_locator,"
            "captured_at,content_sha256,raw_payload,idempotency_key) "
            "VALUES ($1,'manual','citywide-outreach-contact',now(),$2,'{}'::jsonb,$3) "
            "RETURNING id",
            query_id,
            "c" * 64,
            f"citywide-outreach-evidence:{case_id}",
        )
        await pool.execute(
            "INSERT INTO suppliers(id,display_name) VALUES ($1,'Citywide supplier')",
            supplier_id,
        )
        contact_id = await pool.fetchval(
            "INSERT INTO supplier_contacts(supplier_id,contact_type,display_value,"
            "normalized_value,is_whatsapp_confirmed,source_evidence_id) "
            "VALUES ($1,'whatsapp','+77001239999','+77001239999',TRUE,$2) RETURNING id",
            supplier_id,
            evidence_id,
        )
        location_id = await pool.fetchval(
            "INSERT INTO supplier_locations(supplier_id,location_key,city,address_text,"
            "source_evidence_id) VALUES ($1,$2,'Алматы','ул. Тестовая 1',$3) RETURNING id",
            supplier_id,
            "d" * 64,
            evidence_id,
        )
        await pool.execute(
            "INSERT INTO offers(request_item_id,supplier_id,supplier_location_id,status,"
            "idempotency_key) VALUES ($1,$2,$3,'lead',$4)",
            item_id,
            supplier_id,
            location_id,
            f"citywide-outreach-offer:{case_id}",
        )
        assert contact_id is not None and len(lanes) == 4

        async with pool.acquire() as connection:
            result = await prepare_supplier_outreach(
                connection,
                case_id=case_id,
                session_name="default",
            )
        assert result.proposals_created == 1
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM action_approvals "
                "WHERE case_id=$1 AND action_type='send_whatsapp'",
                case_id,
            )
            == 1
        )
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM outbox_events WHERE event_type='whatsapp.send_text'",
            )
            == 0
        )
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_queued_item_turn_requires_new_exact_approval_and_never_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _QueuedTurnConnection:
        def __init__(self) -> None:
            self.conversation_id = uuid4()
            self.case_id = uuid4()
            self.owner_id = uuid4()
            self.turn_id = uuid4()
            self.approval_id = uuid4()
            self.outbox: list[tuple[object, ...]] = []
            self.executed: list[tuple[str, tuple[object, ...]]] = []

        def transaction(self) -> _Transaction:
            return _Transaction()

        async def fetchrow(self, sql: str, *_args: object):
            if "FROM supplier_conversations AS c" in sql:
                return {
                    "case_id": self.case_id,
                    "case_status": "contacting",
                    "state": "escalated",
                    "opted_out": False,
                    "outbound_count": 1,
                    "owner_user_id": self.owner_id,
                    "telegram_id": 123,
                    "normalized_value": "+77001234567",
                    "active": True,
                }
            if "FROM supplier_conversation_item_turns" in sql:
                return {"id": self.turn_id, "draft_text": "Уточните цену по марле."}
            raise AssertionError(sql)

        async def fetchval(self, sql: str, *_args: object):
            assert "supplier_conversation_item_turns" in sql
            return None

        async def execute(self, sql: str, *args: object):
            self.executed.append((sql, args))
            return "UPDATE 1"

    connection = _QueuedTurnConnection()
    requested: list[tuple[object, ...]] = []

    async def request(_connection, **kwargs):
        requested.append(tuple(kwargs["payload"].values()))
        return connection.approval_id

    async def enqueue(_connection, **kwargs):
        connection.outbox.append((kwargs["event_type"], kwargs["payload"]))

    async def manual_policy(*_args):
        return DEFAULT_SEND_POLICY

    async def manual_authorize(*_args, **_kwargs):
        return None

    monkeypatch.setattr("procurement_bot.supplier_outreach.get_case_send_policy", manual_policy)
    monkeypatch.setattr(
        "procurement_bot.supplier_outreach.authorize_and_enqueue_auto_whatsapp", manual_authorize
    )
    monkeypatch.setattr("procurement_bot.supplier_outreach.request_approval", request)
    monkeypatch.setattr("procurement_bot.supplier_outreach.enqueue_outbox", enqueue)

    approval_id = await request_next_supplier_turn_approval(
        connection,  # type: ignore[arg-type]
        conversation_id=connection.conversation_id,
        requested_by=connection.owner_id,
    )

    assert approval_id == connection.approval_id
    assert len(requested) == 1
    assert len(connection.outbox) == 1
    assert connection.outbox[0][0] == "telegram.send_message"
    assert "whatsapp.send_text" not in {event[0] for event in connection.outbox}
    assert any("SET status='awaiting_approval'" in sql for sql, _ in connection.executed)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("autonomous", [False, True])
async def test_postgres_multi_item_outreach_turns_are_retry_safe(
    autonomous: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_supplier_outreach_{uuid4().hex}"
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
        max_size=4,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        owner_id = uuid4()
        case_id = uuid4()
        supplier_id = uuid4()
        telegram_id = 700_000_000 + int(owner_id.hex[:8], 16)
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,$3)",
            owner_id,
            telegram_id,
            "Supplier outreach integration test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES ($1,$2,$3,$4,'evaluating')",
            case_id,
            owner_id,
            "Two items from one supplier",
            "Алматы",
        )
        run_id = await pool.fetchval(
            "INSERT INTO research_runs(case_id,idempotency_key) VALUES ($1,$2) RETURNING id",
            case_id,
            f"supplier-outreach-test-run:{case_id}",
        )
        query_id = await pool.fetchval(
            "INSERT INTO research_queries(research_run_id,provider,query_text,idempotency_key) "
            "VALUES ($1,'manual','approved supplier contact',$2) RETURNING id",
            run_id,
            f"supplier-outreach-test-query:{case_id}",
        )
        evidence_id = await pool.fetchval(
            """
            INSERT INTO source_evidence(
                research_query_id,source_type,source_locator,captured_at,
                content_sha256,raw_payload,idempotency_key
            ) VALUES ($1,'manual','test:supplier-contact',now(),$2,'{}'::jsonb,$3)
            RETURNING id
            """,
            query_id,
            "a" * 64,
            f"supplier-outreach-test-evidence:{case_id}",
        )
        await pool.execute(
            "INSERT INTO suppliers(id,display_name) VALUES ($1,$2)",
            supplier_id,
            "Test medical supplier",
        )
        await pool.fetchval(
            """
            INSERT INTO supplier_contacts(
                supplier_id,contact_type,display_value,normalized_value,
                is_whatsapp_confirmed,source_evidence_id
            ) VALUES ($1,'whatsapp','+77001239876','+77001239876',TRUE,$2)
            RETURNING id
            """,
            supplier_id,
            evidence_id,
        )
        item_ids = []
        for line_number, name, specification, quantity, unit in (
            (1, "Марля", "Марля медицинская, рулон 10 м", Decimal("20"), "рулон"),
            (2, "Бинт", "Бинт стерильный, 7 м", Decimal("40"), "шт"),
        ):
            item_id = await pool.fetchval(
                """
                INSERT INTO request_items(
                    case_id,line_number,name,specification_text,quantity,unit,status
                ) VALUES ($1,$2,$3,$4,$5,$6,'ready') RETURNING id
                """,
                case_id,
                line_number,
                name,
                specification,
                quantity,
                unit,
            )
            item_ids.append(item_id)
            await pool.execute(
                "INSERT INTO offers(request_item_id,supplier_id,status,idempotency_key) "
                "VALUES ($1,$2,'exact',$3)",
                item_id,
                supplier_id,
                f"supplier-outreach-test-offer:{case_id}:{line_number}",
            )
            group_id = await pool.fetchval(
                """
                INSERT INTO research_item_groups(research_run_id,request_item_id,status)
                VALUES ($1,$2,'paused') RETURNING id
                """,
                run_id,
                item_id,
            )
            for lane_no in range(4):
                await pool.execute(
                    "INSERT INTO research_lanes(group_id,lane_no,status) VALUES ($1,$2,'paused')",
                    group_id,
                    lane_no,
                )

        if autonomous:
            async with pool.acquire() as connection, connection.transaction():
                await set_case_auto_send_policy(
                    connection,
                    case_id=case_id,
                    owner_user_id=owner_id,
                    enabled=True,
                    consent_text="Automatically send supplier questions for this test case",
                    consent_reference="test:outreach-autonomous",
                )
        async with pool.acquire() as connection:
            first = await prepare_supplier_outreach(
                connection,
                case_id=case_id,
                session_name="default",
            )
        assert first.proposals_created == 1
        assert first.queued_turns == 1

        conversations = await pool.fetch(
            "SELECT id,state FROM supplier_conversations WHERE case_id=$1", case_id
        )
        assert len(conversations) == 1
        conversation_id = conversations[0]["id"]
        assert conversations[0]["state"] == "approved"
        targets = await pool.fetch(
            """
            SELECT offer.request_item_id
            FROM supplier_conversation_targets AS target
            JOIN offers AS offer ON offer.id=target.offer_id
            WHERE target.conversation_id=$1 ORDER BY offer.request_item_id
            """,
            conversation_id,
        )
        assert {row["request_item_id"] for row in targets} == set(item_ids)
        turns = await pool.fetch(
            """
            SELECT turn.target_id,turn.turn_no,turn.status,turn.approval_id
            FROM supplier_conversation_item_turns AS turn
            WHERE turn.conversation_id=$1 ORDER BY turn.turn_no
            """,
            conversation_id,
        )
        assert len(turns) == 2
        assert [turn["status"] for turn in turns] == ["awaiting_approval", "queued"]
        assert turns[0]["approval_id"] is not None
        assert turns[1]["approval_id"] is None
        assert (
            await pool.fetchval("SELECT count(*) FROM action_approvals WHERE case_id=$1", case_id)
            == 1
        )
        assert await pool.fetchval(
            "SELECT count(*) FROM outbox_events WHERE payload->>'chat_id'=$1",
            str(telegram_id),
        ) == (0 if autonomous else 1)
        assert await pool.fetchval(
            "SELECT count(*) FROM jobs WHERE kind='enqueue_whatsapp_send'"
        ) == (1 if autonomous else 0)
        assert await pool.fetchval(
            "SELECT status FROM action_approvals WHERE case_id=$1", case_id
        ) == ("approved" if autonomous else "requested")

        async with pool.acquire() as connection:
            retry = await prepare_supplier_outreach(
                connection,
                case_id=case_id,
                session_name="default",
            )
        assert retry.proposals_created == 0
        assert retry.queued_turns == 0
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM supplier_conversations WHERE case_id=$1", case_id
            )
            == 1
        )
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM supplier_conversation_item_turns WHERE conversation_id=$1",
                conversation_id,
            )
            == 2
        )
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM supplier_dialogue_assignments WHERE conversation_id=$1",
                conversation_id,
            )
            == 2
        )
        assert (
            await pool.fetchval("SELECT count(*) FROM action_approvals WHERE case_id=$1", case_id)
            == 1
        )

        if autonomous:
            test_now = datetime(2026, 10, 1, 12, tzinfo=UTC)
            monkeypatch.setattr(
                "procurement_bot.whatsapp_approval_workflow._now_utc",
                lambda: test_now,
            )
            approval_id = await pool.fetchval(
                "SELECT id FROM action_approvals WHERE case_id=$1", case_id
            )
            assert await start_approved_whatsapp_send(pool, approval_id) is not None
            send_event = await pool.fetchrow(
                "SELECT * FROM outbox_events WHERE approval_id=$1", approval_id
            )
            assert send_event is not None
            assert send_event["status"] == "pending"
            assert send_event["provider_request_id"] is None
            claimed = ClaimedOutbox(
                id=send_event["id"],
                event_type=send_event["event_type"],
                payload=send_event["payload"],
                attempts=1,
                max_attempts=send_event["max_attempts"],
                idempotency_key=send_event["idempotency_key"],
                locked_by="integration-boundary-check",
                lease_expires_at=test_now + timedelta(minutes=1),
                approval_id=send_event["approval_id"],
                provider_request_id=send_event["provider_request_id"],
            )
            delivery_context = await assert_whatsapp_delivery_allowed(
                pool,
                claimed,
                now=test_now,
            )
            assert delivery_context.payload.case_id == case_id
            assert (
                await pool.fetchval("SELECT status FROM action_approvals WHERE id=$1", approval_id)
                == "consumed"
            )
            assert (
                await pool.fetchval(
                    "SELECT status FROM outbox_events WHERE id=$1", send_event["id"]
                )
                == "pending"
            )
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_outreach_skips_a_new_chat_for_a_paused_stable_assignment() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_supplier_paused_assignment_{uuid4().hex}"
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
        max_size=4,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        owner_id = uuid4()
        case_id = uuid4()
        supplier_id = uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,$3)",
            owner_id,
            710_000_000 + int(owner_id.hex[:8], 16),
            "Paused assignment integration test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES ($1,$2,$3,$4,'evaluating')",
            case_id,
            owner_id,
            "Paused assignment must keep its old chat",
            "Алматы",
        )
        run_id = await pool.fetchval(
            "INSERT INTO research_runs(case_id,idempotency_key) VALUES ($1,$2) RETURNING id",
            case_id,
            f"paused-assignment-run:{case_id}",
        )
        query_id = await pool.fetchval(
            "INSERT INTO research_queries(research_run_id,provider,query_text,idempotency_key) "
            "VALUES ($1,'manual','approved supplier contact',$2) RETURNING id",
            run_id,
            f"paused-assignment-query:{case_id}",
        )
        evidence_id = await pool.fetchval(
            """
            INSERT INTO source_evidence(
                research_query_id,source_type,source_locator,captured_at,
                content_sha256,raw_payload,idempotency_key
            ) VALUES ($1,'manual','test:paused-assignment-contact',now(),$2,'{}'::jsonb,$3)
            RETURNING id
            """,
            query_id,
            "b" * 64,
            f"paused-assignment-evidence:{case_id}",
        )
        await pool.execute(
            "INSERT INTO suppliers(id,display_name) VALUES ($1,$2)",
            supplier_id,
            "Paused assignment supplier",
        )
        contact_id = await pool.fetchval(
            """
            INSERT INTO supplier_contacts(
                supplier_id,contact_type,display_value,normalized_value,
                is_whatsapp_confirmed,source_evidence_id
            ) VALUES ($1,'whatsapp','+77001239877','+77001239877',TRUE,$2)
            RETURNING id
            """,
            supplier_id,
            evidence_id,
        )
        item_id = await pool.fetchval(
            """
            INSERT INTO request_items(
                case_id,line_number,name,specification_text,quantity,unit,status
            ) VALUES ($1,1,'Марля','Марля медицинская',20,'рулон','ready') RETURNING id
            """,
            case_id,
        )
        old_offer_id, candidate_offer_id = uuid4(), uuid4()
        for offer_id, key in (
            (old_offer_id, "paused-assignment-old-offer"),
            (candidate_offer_id, "paused-assignment-new-offer"),
        ):
            await pool.execute(
                """
                INSERT INTO offers(id,request_item_id,supplier_id,status,idempotency_key)
                VALUES ($1,$2,$3,'exact',$4)
                """,
                offer_id,
                item_id,
                supplier_id,
                f"{key}:{case_id}",
            )
        group_id = await pool.fetchval(
            """
            INSERT INTO research_item_groups(research_run_id,request_item_id,status)
            VALUES ($1,$2,'paused') RETURNING id
            """,
            run_id,
            item_id,
        )
        lanes = []
        for lane_no in range(4):
            lanes.append(
                await pool.fetchval(
                    "INSERT INTO research_lanes(group_id,lane_no,status) "
                    "VALUES ($1,$2,'paused') RETURNING id",
                    group_id,
                    lane_no,
                )
            )
        old_conversation_id = uuid4()
        await pool.execute(
            """
            INSERT INTO supplier_conversations(
                id,case_id,supplier_id,supplier_contact_id,session_name,external_chat_id,
                state,idempotency_key
            ) VALUES ($1,$2,$3,$4,'default','77001239877@c.us','closed_no_reply',$5)
            """,
            old_conversation_id,
            case_id,
            supplier_id,
            contact_id,
            f"paused-assignment-old-chat:{case_id}",
        )
        async with pool.acquire() as connection, connection.transaction():
            old_target = await attach_conversation_target(
                connection,
                ConversationTargetInput(
                    conversation_id=old_conversation_id,
                    offer_id=old_offer_id,
                    request_item_id=item_id,
                    provenance_kind="research",
                    provenance_ref=f"offer:{old_offer_id}",
                    source_evidence_id=evidence_id,
                    idempotency_key=f"paused-assignment-old-target:{case_id}",
                ),
            )
            await connection.execute(
                """
                INSERT INTO supplier_dialogue_assignments(
                    group_id,lane_id,request_item_id,supplier_id,conversation_id,status
                ) VALUES ($1,$2,$3,$4,$5,'assigned')
                """,
                group_id,
                lanes[2],
                item_id,
                supplier_id,
                old_conversation_id,
            )

        async with pool.acquire() as connection:
            result = await prepare_supplier_outreach(
                connection,
                case_id=case_id,
                session_name="isolated-restore-check",
            )

        assert result.proposals_created == 0
        assert result.queued_turns == 0
        assert result.skipped_existing == 1
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM supplier_conversations WHERE case_id=$1", case_id
            )
            == 1
        )
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM supplier_conversation_targets WHERE offer_id=$1 AND id=$2",
                old_offer_id,
                old_target.id,
            )
            == 1
        )
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM supplier_conversation_targets WHERE offer_id=$1",
                candidate_offer_id,
            )
            == 0
        )
        stable_assignment = await pool.fetchrow(
            """
            SELECT lane_id,conversation_id,status FROM supplier_dialogue_assignments
            WHERE group_id=$1 AND request_item_id=$2 AND supplier_id=$3
            """,
            group_id,
            item_id,
            supplier_id,
        )
        assert dict(stable_assignment) == {
            "lane_id": lanes[2],
            "conversation_id": old_conversation_id,
            "status": "assigned",
        }
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
