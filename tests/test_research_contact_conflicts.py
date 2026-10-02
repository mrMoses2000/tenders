from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from procurement_bot.db import run_migrations
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.providers.browser import BROWSER_SPEC_SHA256, SupplierBrowserResult
from procurement_bot.research import (
    ContactKind,
    EvidenceClaim,
    EvidenceType,
    ResearchSource,
    SupplierCandidate,
    SupplierContact,
    SupplierEvidence,
    build_research_plan,
)
from procurement_bot.research_execution import ResearchRunExecutor, _QueryBranch, _request_hash
from procurement_bot.suppliers import (
    SourceEvidenceInput,
    SupplierContactInput,
    SupplierInput,
    normalize_contact,
    upsert_source_evidence,
    upsert_supplier,
    upsert_supplier_contact,
)


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("source", [ResearchSource.WEB, ResearchSource.TWO_GIS])
async def test_research_persists_cards_while_quarantining_conflicting_phone(
    source: ResearchSource,
) -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_research_contact_conflict_{uuid4().hex}"
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
        user_id = uuid4()
        case_id = uuid4()
        run_id = uuid4()
        item_id = uuid4()
        query_id = uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,'Contact test')",
            user_id,
            1_500_000_000 + int(user_id.hex[:8], 16),
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES ($1,$2,'Contact conflict test','Алматы','researching')",
            case_id,
            user_id,
        )
        await pool.execute(
            "INSERT INTO research_runs(id,case_id,status,idempotency_key) "
            "VALUES ($1,$2,'running',$3)",
            run_id,
            case_id,
            f"contact-conflict-run:{run_id}",
        )
        await pool.execute(
            "INSERT INTO request_items(id,case_id,line_number,name,quantity,unit) "
            "VALUES ($1,$2,1,'Тестовая позиция',1,'шт')",
            item_id,
            case_id,
        )

        plan = build_research_plan(
            ParsedProcurementRequest(
                city="Алматы",
                search_scope=SearchScope.CITYWIDE,
                items=[ProcurementItem(name="Тестовая позиция", quantity=1, unit="шт")],
            )
        )
        source_plan = next(branch for branch in plan.source_plans if branch.source == source)
        provider = "2gis_web" if source == ResearchSource.TWO_GIS else "web"
        await pool.execute(
            "INSERT INTO research_queries("
            "id,research_run_id,provider,query_text,city,idempotency_key) "
            "VALUES ($1,$2,$3,$4,'Алматы',$5)",
            query_id,
            run_id,
            provider,
            source_plan.rendered_query,
            f"contact-conflict-query:{query_id}",
        )

        async with pool.acquire() as connection, connection.transaction():
            owner_evidence = await upsert_source_evidence(
                connection,
                SourceEvidenceInput(
                    source_type="manual",
                    source_locator=f"manual:contact-owner:{uuid4()}",
                    captured_at=datetime.now(UTC),
                    content_sha256="a" * 64,
                    raw_payload={"test": "canonical owner seed"},
                    idempotency_key=f"contact-owner-evidence:{uuid4()}",
                ),
            )
            owner = await upsert_supplier(
                connection,
                SupplierInput(
                    display_name="Existing canonical owner",
                    identity_type="domain",
                    identity_value="existing-owner.example.kz",
                    source_evidence_id=owner_evidence.id,
                ),
            )
            await upsert_supplier_contact(
                connection,
                owner.id,
                SupplierContactInput(
                    contact_type="phone",
                    display_value="+77010000001",
                    source_evidence_id=owner_evidence.id,
                ),
            )

        now = datetime.now(UTC)

        def candidate(external_id: str, host: str, contacts: list[SupplierContact]):
            source_url = (
                f"https://2gis.kz/almaty/firm/{external_id}"
                if source == ResearchSource.TWO_GIS
                else f"https://{host}/supplier/{external_id}"
            )
            evidence = SupplierEvidence(
                evidence_type=EvidenceType.BUSINESS_LISTING,
                source=source,
                source_url=source_url,
                observed_at=now,
                raw_snapshot_sha256="b" * 64,
                claims=[EvidenceClaim.BUSINESS_IDENTITY, EvidenceClaim.CONTACT],
            )
            return SupplierCandidate(
                source=source,
                external_id=external_id,
                name=f"Research card {external_id}",
                contacts=contacts,
                evidence=[evidence],
            )

        contested_value = "+7 701 000 00 01"
        safe_value = "+7 702 000 00 02"
        other_safe_value = "+7 703 000 00 03"
        card_a_id = "700000000000001" if source == ResearchSource.TWO_GIS else "card-a"
        card_b_id = "700000000000002" if source == ResearchSource.TWO_GIS else "card-b"
        card_a_alias = (
            card_a_id
            if source == ResearchSource.TWO_GIS
            else f"web:vendor-a.example.kz:{card_a_id}"
        )
        card_b_alias = (
            card_b_id
            if source == ResearchSource.TWO_GIS
            else f"web:vendor-b.example.kz:{card_b_id}"
        )
        candidates = [
            candidate(
                card_a_id,
                "vendor-a.example.kz",
                [
                    SupplierContact(
                        kind=ContactKind.WHATSAPP,
                        value=contested_value,
                        confidence=0.9,
                    ),
                    SupplierContact(
                        kind=ContactKind.PHONE,
                        value=safe_value,
                        confidence=0.9,
                    ),
                ],
            ),
            candidate(
                card_b_id,
                "vendor-b.example.kz",
                [
                    SupplierContact(
                        kind=ContactKind.PHONE,
                        value=other_safe_value,
                        confidence=0.9,
                    ),
                ],
            ),
        ]
        result = SupplierBrowserResult(
            spec_sha256=BROWSER_SPEC_SHA256,
            request_sha256=_request_hash(source_plan),
            candidates=candidates,
            exhausted=True,
            pages_examined=1,
        )
        executor = ResearchRunExecutor(pool, object())  # type: ignore[arg-type]
        branch = _QueryBranch(query_id, source_plan, item_id)

        for _ in range(2):
            async with pool.acquire() as connection, connection.transaction():
                assert await executor._persist_branch(connection, branch, result) == 2

        contested_normalized = normalize_contact("phone", contested_value)
        assert (
            await pool.fetchval(
                "SELECT supplier_id FROM supplier_contacts WHERE normalized_value=$1 AND active",
                contested_normalized,
            )
            == owner.id
        )
        assert (
            await pool.fetchval(
                "SELECT count(*) FROM supplier_contacts WHERE normalized_value=$1 AND active",
                contested_normalized,
            )
            == 1
        )

        card_a_supplier = await pool.fetchval(
            "SELECT supplier_id FROM supplier_aliases WHERE normalized_value=$1 AND active",
            card_a_alias,
        )
        card_b_supplier = await pool.fetchval(
            "SELECT supplier_id FROM supplier_aliases WHERE normalized_value=$1 AND active",
            card_b_alias,
        )
        assert card_a_supplier and card_b_supplier
        for value, expected_supplier in (
            (safe_value, card_a_supplier),
            (other_safe_value, card_b_supplier),
        ):
            assert (
                await pool.fetchval(
                    "SELECT supplier_id FROM supplier_contacts "
                    "WHERE normalized_value=$1 AND active",
                    normalize_contact("phone", value),
                )
                == expected_supplier
            )
        assert (
            await pool.fetchval("SELECT count(*) FROM offers WHERE request_item_id=$1", item_id)
            == 2
        )

        events = await pool.fetch(
            "SELECT data FROM workflow_events WHERE case_id=$1 AND request_item_id=$2 "
            "AND event_type='supplier_contact_identity_conflict'",
            case_id,
            item_id,
        )
        assert len(events) == 1
        event_data = events[0]["data"]
        assert event_data["outcome"] == "contact_needs_review"
        assert event_data["contact_kind"] == "whatsapp"
        assert event_data["source_evidence_id"]
        assert event_data["contact_fingerprint"]
        serialized_event = json.dumps(event_data)
        assert contested_value not in serialized_event
        assert contested_normalized not in serialized_event
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_invalid_olx_contacts_are_reviewed_without_blocking_cards_or_valid_contacts() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_research_invalid_contact_{uuid4().hex}"
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
        user_id, case_id, run_id, item_id, query_id = (uuid4() for _ in range(5))
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES ($1,$2,'Invalid contact test')",
            user_id,
            1_600_000_000 + int(user_id.hex[:8], 16),
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES ($1,$2,'Invalid contact test','Алматы','researching')",
            case_id,
            user_id,
        )
        await pool.execute(
            "INSERT INTO research_runs(id,case_id,status,idempotency_key) "
            "VALUES ($1,$2,'running',$3)",
            run_id,
            case_id,
            f"invalid-contact-run:{run_id}",
        )
        await pool.execute(
            "INSERT INTO request_items(id,case_id,line_number,name,quantity,unit) "
            "VALUES ($1,$2,1,'Тестовая позиция',1,'шт')",
            item_id,
            case_id,
        )
        plan = build_research_plan(
            ParsedProcurementRequest(
                city="Алматы",
                search_scope=SearchScope.CITYWIDE,
                items=[ProcurementItem(name="Тестовая позиция", quantity=1, unit="шт")],
            ),
            include_olx=True,
        )
        source_plan = next(
            branch for branch in plan.source_plans if branch.source == ResearchSource.OLX
        )
        await pool.execute(
            "INSERT INTO research_queries("
            "id,research_run_id,provider,query_text,city,idempotency_key) "
            "VALUES ($1,$2,'marketplace',$3,'Алматы',$4)",
            query_id,
            run_id,
            source_plan.rendered_query,
            f"invalid-contact-query:{query_id}",
        )

        valid_owner_phone = "+77010000001"
        async with pool.acquire() as connection, connection.transaction():
            owner_evidence = await upsert_source_evidence(
                connection,
                SourceEvidenceInput(
                    source_type="manual",
                    source_locator=f"manual:valid-contact-owner:{uuid4()}",
                    captured_at=datetime.now(UTC),
                    content_sha256="c" * 64,
                    raw_payload={"test": "valid OLX identity seed"},
                    idempotency_key=f"valid-contact-owner-evidence:{uuid4()}",
                ),
            )
            owner = await upsert_supplier(
                connection,
                SupplierInput(
                    display_name="Existing OLX contact owner",
                    identity_type="domain",
                    identity_value="olx-contact-owner.example.kz",
                    source_evidence_id=owner_evidence.id,
                ),
            )
            await upsert_supplier_contact(
                connection,
                owner.id,
                SupplierContactInput(
                    contact_type="phone",
                    display_value=valid_owner_phone,
                    source_evidence_id=owner_evidence.id,
                ),
            )

        now = datetime.now(UTC)

        def olx_candidate(
            listing_id: str,
            seller_id: str,
            contacts: list[SupplierContact],
        ) -> SupplierCandidate:
            evidence = SupplierEvidence(
                evidence_type=EvidenceType.BUSINESS_LISTING,
                source=ResearchSource.OLX,
                source_url=f"https://www.olx.kz/d/obyavlenie/{listing_id}.html",
                observed_at=now,
                raw_snapshot_sha256="d" * 64,
                claims=[EvidenceClaim.BUSINESS_IDENTITY, EvidenceClaim.CONTACT],
            )
            return SupplierCandidate(
                source=ResearchSource.OLX,
                external_id=listing_id,
                seller_profile_id=seller_id,
                name=f"OLX card {listing_id}",
                contacts=contacts,
                evidence=[evidence],
            )

        invalid_phone = "+7 12"
        invalid_whatsapp = "not-a-phone"
        sibling_phone = "+77020000002"
        candidates = [
            olx_candidate(
                "listing-a",
                "sellerA",
                [
                    SupplierContact(
                        kind=ContactKind.PHONE,
                        value=invalid_phone,
                        confidence=0.9,
                    ),
                    SupplierContact(
                        kind=ContactKind.WHATSAPP,
                        value=invalid_whatsapp,
                        confidence=0.9,
                    ),
                    SupplierContact(
                        kind=ContactKind.PHONE,
                        value=valid_owner_phone,
                        confidence=0.9,
                    ),
                ],
            ),
            olx_candidate(
                "listing-b",
                "sellerB",
                [
                    SupplierContact(
                        kind=ContactKind.PHONE,
                        value=sibling_phone,
                        confidence=0.9,
                    ),
                ],
            ),
        ]
        result = SupplierBrowserResult(
            spec_sha256=BROWSER_SPEC_SHA256,
            request_sha256=_request_hash(source_plan),
            candidates=candidates,
            exhausted=True,
            pages_examined=1,
        )
        executor = ResearchRunExecutor(pool, object())  # type: ignore[arg-type]
        branch = _QueryBranch(query_id, source_plan, item_id)

        for _ in range(2):
            async with pool.acquire() as connection, connection.transaction():
                assert await executor._persist_branch(connection, branch, result) == 2

        assert (
            await pool.fetchval(
                "SELECT supplier_id FROM supplier_contacts WHERE normalized_value=$1 AND active",
                normalize_contact("phone", valid_owner_phone),
            )
            == owner.id
        )
        assert (
            await pool.fetchval(
                "SELECT supplier_id FROM supplier_aliases WHERE alias_type='source_record' "
                "AND normalized_value='olx_seller:sellera' AND active"
            )
            == owner.id
        )
        sibling_supplier = await pool.fetchval(
            "SELECT supplier_id FROM supplier_aliases WHERE alias_type='source_record' "
            "AND normalized_value='olx_seller:sellerb' AND active"
        )
        assert sibling_supplier is not None
        assert (
            await pool.fetchval(
                "SELECT supplier_id FROM supplier_contacts WHERE normalized_value=$1 AND active",
                normalize_contact("phone", sibling_phone),
            )
            == sibling_supplier
        )
        assert (
            await pool.fetchval("SELECT count(*) FROM offers WHERE request_item_id=$1", item_id)
            == 2
        )

        events = await pool.fetch(
            "SELECT data FROM workflow_events WHERE case_id=$1 AND request_item_id=$2 "
            "AND event_type='supplier_contact_invalid'",
            case_id,
            item_id,
        )
        assert len(events) == 2
        invalid_kinds = {row["data"]["contact_kind"] for row in events}
        assert invalid_kinds == {"phone", "whatsapp"}
        assert all(row["data"]["outcome"] == "contact_needs_review" for row in events)
        assert all(row["data"]["source_evidence_id"] for row in events)
        serialized_events = json.dumps([row["data"] for row in events])
        assert invalid_phone not in serialized_events
        assert invalid_whatsapp not in serialized_events
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
