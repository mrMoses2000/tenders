from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4

import asyncpg
import pytest
from aiohttp import web

from procurement_bot.db import run_migrations
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.mini_app import (
    _load_case_offer_rows,
    _load_research_progress,
    _public_quote_facts,
    add_item_requirement,
    authenticate,
    parse_item_requirement,
    refresh_outreach_drafts,
    supplier_message_media,
    validate_public_url,
    verify_init_data,
)
from procurement_bot.research import ResearchSource, build_research_plan
from procurement_bot.research_repository import mark_research_running, persist_research_plan
from procurement_bot.suppliers import (
    OfferInput,
    OfferObservationInput,
    SourceEvidenceInput,
    SupplierInput,
    upsert_offer,
    upsert_offer_observation,
    upsert_source_evidence,
    upsert_supplier,
)


def _signed_data(token: str, *, auth_date: int, user_id: int) -> str:
    values = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id})}
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_signed_fresh_telegram_init_data_is_accepted() -> None:
    assert (
        verify_init_data(
            _signed_data("test-token", auth_date=1000, user_id=42), "test-token", now=1200
        )
        == 42
    )


def test_tampered_or_expired_init_data_is_rejected() -> None:
    valid = _signed_data("test-token", auth_date=1000, user_id=42)
    assert verify_init_data(valid, "other-token", now=1200) is None
    assert verify_init_data(valid.replace("42", "43"), "test-token", now=1200) is None
    assert verify_init_data(valid, "test-token", now=1000 + 24 * 3600 + 1) is None
    assert verify_init_data(valid + "&hash=duplicate", "test-token", now=1200) is None
    assert verify_init_data("", "test-token", now=1200) is None


def test_public_url_requires_https_and_normalizes_path() -> None:
    assert validate_public_url("https://example.com/miniapp") == "https://example.com/miniapp/"
    with pytest.raises(ValueError):
        validate_public_url("http://example.com/miniapp")


def test_research_geography_text_labels_only_explicit_decisions() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the isolated Mini App helper test")

    app_source = (Path(__file__).parents[1] / "mini_app" / "app.js").read_text()
    helper = re.search(
        r"function researchGeographyText\(decision\) \{.*?\n\}",
        app_source,
        re.DOTALL,
    )
    assert helper is not None
    assert "researchGeographyText(attempt.geographic_decision)" in app_source
    node_script = helper.group(0) + "\nconsole.log(JSON.stringify(["
    node_script += (
        ",".join(
            [
                'researchGeographyText("city_verified")',
                'researchGeographyText("accepted_by_address")',
                'researchGeographyText("outside_area")',
                'researchGeographyText("unknown")',
                'researchGeographyText("not_applicable")',
                "researchGeographyText(undefined)",
                'researchGeographyText("legacy_unknown_code")',
            ]
        )
        + "]));"
    )
    result = subprocess.run(  # noqa: S603 - runs only the local, extracted pure helper
        [node, "-e", node_script], check=True, capture_output=True, text=True
    )

    assert json.loads(result.stdout) == [
        "Город соответствует заданной территории",
        "Адрес подтверждает соответствие заданной территории",
        "Адрес вне заданной территории",
        "Территория не подтверждена",
        "Проверка территории не применяется",
        None,
        None,
    ]
    with pytest.raises(ValueError):
        validate_public_url("https://example.com/miniapp?token=unsafe")


def test_detail_background_refresh_respects_focus_and_dirty_controls() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the isolated Mini App helper test")

    app_source = (Path(__file__).parents[1] / "mini_app" / "app.js").read_text()
    helper = re.search(
        r"function canRefreshDetailInPlace\(root, activeElement = document.activeElement\)"
        r" \{.*?\n\}",
        app_source,
        re.DOTALL,
    )
    assert helper is not None
    node_script = (
        helper.group(0)
        + """
const cleanControl = {dataset: {freshValue: "before", freshChecked: "false"},
  value: "before", checked: false};
const dirtyControl = {...cleanControl, value: "edited"};
const missingBaseline = {dataset: {}, value: "new", checked: false};
const focus = {tagName: "INPUT"};
const bodyFocus = {tagName: "BODY"};
const root = controls => ({contains: node => node === focus, querySelectorAll: () => controls});
console.log(JSON.stringify([
  canRefreshDetailInPlace(root([cleanControl]), bodyFocus),
  canRefreshDetailInPlace(root([cleanControl]), focus),
  canRefreshDetailInPlace(root([dirtyControl]), bodyFocus),
  canRefreshDetailInPlace(root([missingBaseline]), bodyFocus)
]));
"""
    )
    result = subprocess.run(  # noqa: S603 - runs only the local, extracted pure helper
        [node, "-e", node_script], check=True, capture_output=True, text=True
    )

    assert json.loads(result.stdout) == [True, False, False, False]


def test_item_requirement_requires_an_explicit_source_and_valid_fields() -> None:
    requirement = parse_item_requirement(
        {
            "requirement_key": "Диаметр трубы",
            "expected_value": "20",
            "unit": "мм",
            "operator": "minimum",
            "is_hard": True,
            "source_pointer": "ТЗ, пункт 3.2",
        }
    )
    assert requirement == {
        "requirement_key": "Диаметр трубы",
        "expected_value": {"value": "20", "unit": "мм"},
        "operator": "minimum",
        "is_hard": True,
        "source_pointer": "ТЗ, пункт 3.2",
    }
    with pytest.raises(ValueError, match="источник"):
        parse_item_requirement(
            {
                "requirement_key": "Диаметр трубы",
                "expected_value": "20",
            }
        )
    with pytest.raises(ValueError, match="оператор"):
        parse_item_requirement(
            {
                "requirement_key": "Диаметр трубы",
                "expected_value": "20",
                "source_pointer": "ТЗ, пункт 3.2",
                "operator": "delete",
            }
        )


class _RequirementRequest:
    def __init__(self, body, *, item_exists=True):
        self.match_info = {"case_id": str(uuid4()), "item_id": str(uuid4())}
        self.app = {"pool": _RequirementPool(item_exists=item_exists)}
        self.owner_id = uuid4()
        self.body = body

    def __getitem__(self, key):
        if key == "owner_id":
            return self.owner_id
        raise KeyError(key)

    async def json(self):
        return self.body


class _RequirementPool:
    def __init__(self, *, item_exists):
        self.connection = _RequirementConnection(item_exists=item_exists)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class _RequirementConnection:
    def __init__(self, *, item_exists):
        self.item_exists = item_exists
        self.inserted_requirement = False
        self.queries = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "FROM request_items i" in query:
            return {"id": args[0]} if self.item_exists else None
        if "FROM workflow_events" in query:
            return None
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "INSERT INTO item_requirements" in query:
            self.inserted_requirement = True
            return uuid4()
        raise AssertionError(f"unexpected fetchval query: {query}")

    async def execute(self, query, *args):
        self.queries.append(query)
        assert "INSERT INTO workflow_events" in query
        assert args[4]["requirement_id"]


@pytest.mark.asyncio
async def test_item_requirement_route_is_owner_scoped_and_additive(monkeypatch) -> None:
    import procurement_bot.mini_app as mini_app

    async def no_job(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mini_app, "enqueue_job", no_job)
    request = _RequirementRequest(
        {
            "idempotency_key": str(uuid4()),
            "requirement_key": "Диаметр",
            "expected_value": "20",
            "unit": "мм",
            "source_pointer": "ТЗ, пункт 3",
        }
    )
    response = await add_item_requirement(request)
    result = json.loads(response.text)
    assert response.status == 201
    assert UUID(result["id"])
    connection = request.app["pool"].connection
    assert connection.inserted_requirement
    assert all("UPDATE item_requirements" not in query for query in connection.queries)
    assert all("DELETE FROM item_requirements" not in query for query in connection.queries)

    foreign_request = _RequirementRequest(
        {
            "idempotency_key": str(uuid4()),
            "requirement_key": "Диаметр",
            "expected_value": "20",
            "source_pointer": "ТЗ, пункт 3",
        },
        item_exists=False,
    )
    foreign_response = await add_item_requirement(foreign_request)
    assert foreign_response.status == 400
    assert not foreign_request.app["pool"].connection.inserted_requirement


@pytest.mark.asyncio
async def test_refresh_outreach_route_returns_unapproved_replacement(monkeypatch) -> None:
    import procurement_bot.mini_app as mini_app
    from procurement_bot.outreach_batch import RefreshedOutreachDraft

    approval_id = uuid4()
    request = _RequirementRequest({"approval_ids": [str(approval_id)]})
    seen = {}

    async def refresh(_connection, **kwargs):
        seen.update(kwargs)
        return [
            RefreshedOutreachDraft(
                approval_id=uuid4(),
                request_item_id=uuid4(),
                item_name="Плитка",
                supplier_name="Магазин",
                recipient="+77000000009",
                text="Точный текст",
            )
        ]

    monkeypatch.setattr(mini_app, "refresh_initial_outreach_drafts", refresh)
    response = await refresh_outreach_drafts(request)
    payload = json.loads(response.text)
    assert response.status == 200
    assert seen["case_id"] == UUID(request.match_info["case_id"])
    assert seen["owner_user_id"] == request.owner_id
    assert seen["approval_ids"] == [approval_id]
    assert payload["drafts"][0]["text"] == "Точный текст"


class _MediaConnection:
    def __init__(self, row):
        self.row = row
        self.query = ""
        self.args = ()

    async def fetchrow(self, query, *args):
        self.query = query
        self.args = args
        return self.row


class _MediaPool:
    def __init__(self, row):
        self.connection = _MediaConnection(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class _MediaRequest:
    def __init__(self, message_id, owner_id, media_root, row):
        self.match_info = {"message_id": str(message_id)}
        self.app = {
            "pool": _MediaPool(row),
            "settings": type("Settings", (), {"media_root": media_root})(),
        }
        self.owner_id = owner_id

    def __getitem__(self, key):
        if key == "owner_id":
            return self.owner_id
        raise KeyError(key)


@pytest.mark.asyncio
async def test_supplier_media_file_route_is_owner_scoped_and_private(tmp_path) -> None:
    media_root = tmp_path / "media"
    media_path = media_root / "supplier-messages" / "ab" / "image.jpg"
    media_path.parent.mkdir(parents=True, mode=0o700)
    media_path.write_bytes(b"private image")
    message_id, owner_id = uuid4(), uuid4()
    request = _MediaRequest(
        message_id,
        owner_id,
        media_root,
        {
            "object_key": "supplier-messages/ab/image.jpg",
            "kind": "image",
            "mime_type": "image/jpeg",
        },
    )

    response = await supplier_message_media(request)

    connection = request.app["pool"].connection
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/jpeg"
    assert response.headers["Cache-Control"] == "no-store"
    assert "p.owner_user_id=$2" in connection.query
    assert connection.args == (message_id, owner_id)


@pytest.mark.asyncio
async def test_supplier_media_file_route_hides_foreign_or_escaping_objects(tmp_path) -> None:
    owner_id = uuid4()
    foreign_request = _MediaRequest(uuid4(), owner_id, tmp_path, None)
    with pytest.raises(web.HTTPNotFound) as not_found:
        await supplier_message_media(foreign_request)
    assert getattr(not_found.value, "status", None) == 404

    escaping_request = _MediaRequest(
        uuid4(),
        owner_id,
        tmp_path,
        {"object_key": "../../etc/passwd", "kind": "image", "mime_type": "image/jpeg"},
    )
    with pytest.raises(web.HTTPNotFound) as path_rejected:
        await supplier_message_media(escaping_request)
    assert getattr(path_rejected.value, "status", None) == 404


@pytest.mark.asyncio
async def test_supplier_media_api_requires_authenticated_owner() -> None:
    class Request:
        path = "/api/supplier-messages/00000000-0000-0000-0000-000000000001/media"
        headers = {}
        app = {"settings": object()}

    called = False

    async def handler(_request):
        nonlocal called
        called = True
        return web.Response()

    response = await authenticate(Request(), handler)

    assert response.status == 401
    assert not called


class _ProgressConnection:
    async def fetch(self, query, *args):
        self.query = query
        self.args = args
        return [
            {
                "request_item_id": uuid4(),
                "status": "researching",
                "target_suppliers": 20,
                "found_leads": 1,
                "qualified_price_suppliers": 0,
                "search_profile_categories": ["малярный инструмент"],
                "search_profile_intent_count": 2,
                "sources": [
                    {
                        "source": "2gis_web",
                        "discovered_cards": 2,
                        "pending_cards": 1,
                        "extracted_cards": 1,
                        "failed_cards": 0,
                        "failure_code": "cli_timeout",
                    }
                ],
            }
        ]


@pytest.mark.asyncio
async def test_progress_read_keeps_legacy_fields_and_adds_discovery_profile_counts() -> None:
    connection = _ProgressConnection()

    rows = await _load_research_progress(connection, uuid4())  # type: ignore[arg-type]

    row = rows[0]
    assert row["found_leads"] == 1
    assert row["qualified_price_suppliers"] == 0
    assert row["search_profile_categories"] == ["малярный инструмент"]
    assert row["search_profile_intent_count"] == 2
    source = row["sources"][0]
    assert source["discovered_cards"] == 2
    assert source["pending_cards"] == 1
    assert source["extracted_cards"] == 1
    assert source["failed_cards"] == 0
    assert source["failure_code"] == "cli_timeout"
    assert "research_discovery_cards" in connection.query
    assert "research_search_intents" in connection.query
    assert "private_tender_unit_price" not in connection.query


def test_public_quote_facts_omit_database_identifiers() -> None:
    source_id = str(uuid4())
    observation_id = str(uuid4())

    facts = _public_quote_facts(
        {
            "price_amount": {
                "value": "12.50",
                "source_evidence_id": source_id,
                "observation_id": observation_id,
                "source_type": "supplier_message",
                "observed_at": "2026-09-30T08:00:00+00:00",
            }
        }
    )

    assert facts == {
        "price_amount": {
            "value": "12.50",
            "source_type": "supplier_message",
            "observed_at": "2026-09-30T08:00:00+00:00",
        }
    }
    assert source_id not in json.dumps(facts)
    assert observation_id not in json.dumps(facts)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_mini_app_offer_query_does_not_fallback_when_current_snapshot_is_empty() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_mini_offer_snapshot_{uuid4().hex}"
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
        max_size=2,
        server_settings={"search_path": schema},
        init=init_connection,
    )
    try:
        await run_migrations(pool, Path(__file__).parents[1] / "migrations")
        async with pool.acquire() as connection, connection.transaction():
            user_id = await connection.fetchval(
                "INSERT INTO users(telegram_id) VALUES($1) RETURNING id",
                900_000_000 + int(uuid4().hex[:8], 16),
            )
            case_id = await connection.fetchval(
                "INSERT INTO procurement_cases(owner_user_id,title,city) "
                "VALUES($1,'Snapshot UI test','Almaty') RETURNING id",
                user_id,
            )
            item_id = await connection.fetchval(
                "INSERT INTO request_items(case_id,line_number,name,quantity,unit) "
                "VALUES($1,1,'Wire',10,'m') RETURNING id",
                case_id,
            )
            evidence = await upsert_source_evidence(
                connection,
                SourceEvidenceInput(
                    source_type="manual",
                    source_locator="manual:mini-offer-snapshot-test",
                    source_url="https://example.kz/wire",
                    captured_at=datetime.now(UTC),
                    content_sha256="a" * 64,
                    raw_payload={},
                    idempotency_key=f"mini-offer-evidence:{uuid4()}",
                ),
            )
            supplier = await upsert_supplier(
                connection,
                SupplierInput(
                    display_name="Snapshot UI supplier",
                    identity_type="source_record",
                    identity_value=f"mini-offer-supplier-{uuid4()}",
                    source_evidence_id=evidence.id,
                ),
            )
            offer = await upsert_offer(
                connection,
                OfferInput(
                    request_item_id=item_id,
                    supplier_id=supplier.id,
                    idempotency_key=f"mini-offer:{uuid4()}",
                    supplier_product_name="Wire",
                ),
            )
            await upsert_offer_observation(
                connection,
                OfferObservationInput(
                    offer_id=offer.id,
                    source_evidence_id=evidence.id,
                    observed_at=datetime.now(UTC),
                    observed_by="test",
                    source_type="supplier_message",
                    idempotency_key=f"mini-observation:{uuid4()}",
                    price_amount=Decimal("125.00"),
                    currency="KZT",
                    price_unit="m",
                    available_qty=Decimal("10"),
                    availability_status="in_stock",
                ),
            )
            version = await connection.fetchval(
                "SELECT max(version)+1 FROM quote_snapshot_versions WHERE offer_id=$1",
                offer.id,
            )
            empty_snapshot_id = await connection.fetchval(
                """INSERT INTO quote_snapshot_versions(
                    offer_id,request_item_id,version,variant_fingerprint,snapshot_hash,snapshot
                ) VALUES($1,$2,$3,$4,$5,$6::jsonb) RETURNING id""",
                offer.id,
                item_id,
                version,
                "b" * 64,
                "c" * 64,
                {"facts": {}},
            )
            await connection.execute(
                """UPDATE quote_snapshot_current SET snapshot_id=$2,updated_at=now()
                   WHERE offer_id=$1""",
                offer.id,
                empty_snapshot_id,
            )

        async with pool.acquire() as connection:
            rows = await _load_case_offer_rows(connection, case_id)

        assert len(rows) == 1
        row = rows[0]
        assert row["price_amount"] is None
        assert row["currency"] is None
        assert row["price_unit"] is None
        assert row["available_qty"] is None
        assert row["availability_status"] == "unknown"
        assert row["source_url"] is None
        assert row["quote_snapshot_version"] == version
        assert row["quote_snapshot_facts"] == {}
        assert "quote_snapshot_id" not in row
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_progress_counts_discoveries_separately_from_offers() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_mini_progress_{uuid4().hex}"
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
        user_id, case_id, item_id = uuid4(), uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,'Progress test')",
            user_id,
            700_000_000 + int(user_id.hex[:8], 16),
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES($1,$2,'Progress test','Almaty','researching')",
            case_id,
            user_id,
        )
        await pool.execute(
            "INSERT INTO request_items(id,case_id,line_number,name,quantity,unit,status) "
            "VALUES($1,$2,1,'Brush 80 mm',10,'each','researching')",
            item_id,
            case_id,
        )
        plan = build_research_plan(
            ParsedProcurementRequest(
                city="Almaty",
                search_scope=SearchScope.CITYWIDE,
                items=[ProcurementItem(name="Brush 80 mm", quantity=10, unit="each")],
            )
        )
        async with pool.acquire() as connection:
            persisted = await persist_research_plan(
                connection, case_id=case_id, plan=plan, requested_by=user_id
            )
            await mark_research_running(connection, persisted.id)
        group_id, lane_id = uuid4(), uuid4()
        await pool.execute(
            "INSERT INTO research_item_groups(id,research_run_id,request_item_id,status) "
            "VALUES($1,$2,$3,'researching')",
            group_id,
            persisted.id,
            item_id,
        )
        query_id = next(
            query_id
            for query_id, branch in zip(persisted.query_ids, plan.source_plans, strict=True)
            if branch.source == ResearchSource.TWO_GIS
        )
        await pool.execute(
            "INSERT INTO research_lanes(id,group_id,lane_no,status) VALUES($1,$2,0,'researching')",
            lane_id,
            group_id,
        )
        await pool.execute(
            """INSERT INTO research_branch_checkpoints(
                research_run_id,group_id,lane_id,query_id,lane_no
            ) VALUES($1,$2,$3,$4,0)""",
            persisted.id,
            group_id,
            lane_id,
            query_id,
        )
        profile_id = uuid4()
        await pool.execute(
            """INSERT INTO research_search_profiles(
                id,research_run_id,request_item_id,profile_version,snapshot_sha256,
                context_sha256,plan_sha256,model_version,prompt_version,profile,idempotency_key
            ) VALUES($1,$2,$3,1,$4,$5,$6,'test-model','test-prompt',$7::jsonb,$8)""",
            profile_id,
            persisted.id,
            item_id,
            "a" * 64,
            "b" * 64,
            plan.plan_sha256,
            {"categories": ["paint brush store", "building supplies"]},
            f"profile:{profile_id}",
        )
        await pool.execute(
            """INSERT INTO research_search_intents(
                profile_id,research_run_id,request_item_id,ordinal,provider,intent_type,
                category,query_text,rationale,uncertainty,scope_revision,intent_sha256,
                idempotency_key
            ) VALUES($1,$2,$3,0,'2gis_web','business','paint supplies',
                     'paint store Almaty','find retailers',0.1,1,$4,$5)""",
            profile_id,
            persisted.id,
            item_id,
            "c" * 64,
            f"intent:{profile_id}:0",
        )
        page_id = await pool.fetchval(
            """INSERT INTO research_discovery_pages(query_id,request_sha256,payload)
               VALUES($1,$2,$3::jsonb) RETURNING id""",
            query_id,
            "d" * 64,
            {"page": 1},
        )
        await pool.execute(
            """INSERT INTO research_discovery_cards(
                page_id,ordinal,external_id,card_url,card_name,status
            ) VALUES($1,0,'store-1','https://2gis.kz/almaty/firm/store-1','Store 1','extracted'),
                   ($1,1,'store-2','https://2gis.kz/almaty/firm/store-2','Store 2','pending')""",
            page_id,
        )

        async with pool.acquire() as connection:
            progress = await _load_research_progress(connection, case_id)

        item_progress = progress[0]
        source = item_progress["sources"][0]
        assert item_progress["search_profile_categories"] == [
            "paint brush store",
            "building supplies",
        ]
        assert item_progress["search_profile_intent_count"] == 1
        assert source["discovered_cards"] == 2
        assert source["extracted_cards"] == 1
        assert source["pending_cards"] == 1
        assert source["failed_cards"] == 0
        assert item_progress["found_leads"] == 0
        assert item_progress["qualified_price_suppliers"] == 0
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
