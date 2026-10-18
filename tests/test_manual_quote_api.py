from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_autonomous_pipeline import pipeline_db, seed_chat  # noqa: F401, F811
from test_manual_quotes import quote_input
from test_mini_app import _signed_data

from procurement_bot.config import Settings
from procurement_bot.mini_app import create_mini_app
from procurement_bot.suppliers import (
    SourceEvidenceInput,
    SupplierInput,
    upsert_source_evidence,
    upsert_supplier,
)


@pytest.mark.postgres
async def test_manual_quote_http_auth_validation_retry_and_visible_evidence(
    pipeline_db,  # noqa: F811
):
    pool, case_id, owner_id, run_id, _executor = pipeline_db
    _chat_id, supplier_id, _group_id, _lane_id, item_id = await seed_chat(
        pool, case_id, run_id, 1
    )
    offer_id = await pool.fetchval(
        "SELECT id FROM offers WHERE request_item_id=$1 AND supplier_id=$2",
        item_id,
        supplier_id,
    )
    foreign_owner = uuid4()
    await pool.execute(
        "INSERT INTO users(id,telegram_id,display_name) VALUES($1,54321,'Other owner')",
        foreign_owner,
    )
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",  # noqa: S106
        telegram_allowed_user_ids="12345,54321",
        waha_outbound_enabled=False,
    )
    route = f"/api/cases/{case_id}/offers/{offer_id}/manual-quotes"
    payload = quote_input(price_unit="рулон", available_qty="12").model_dump(mode="json")
    headers = {
        "Authorization": "tma " + _signed_data(
            "test-token", auth_date=int(time.time()), user_id=12345
        )
    }
    foreign_headers = {
        "Authorization": "tma " + _signed_data(
            "test-token", auth_date=int(time.time()), user_id=54321
        )
    }
    counts_sql = """
        SELECT (SELECT count(*) FROM jobs) AS jobs,
               (SELECT count(*) FROM outbox_events) AS outbox,
               (SELECT count(*) FROM purchase_items) AS purchases,
               (SELECT count(*) FROM whatsapp_send_acceptance_journal) AS acceptances
    """
    before = dict(await pool.fetchrow(counts_sql))
    async with TestClient(TestServer(create_mini_app(pool, settings))) as client:
        response = await client.post(route, json=payload)
        assert response.status == 401
        response = await client.post(route, json=payload, headers=foreign_headers)
        assert response.status == 404
        response = await client.post(
            f"/api/cases/{uuid4()}/offers/{offer_id}/manual-quotes",
            json=payload,
            headers=headers,
        )
        assert response.status == 404
        response = await client.post(
            route, json={**payload, "available_qty": None}, headers=headers
        )
        assert response.status == 400
        validation = await response.json()
        assert "source_text" not in validation["error"]
        assert payload["source_text"] not in str(validation)
        assert await pool.fetchval(
            "SELECT count(*) FROM offer_observations WHERE offer_id=$1", offer_id
        ) == 0

        response = await client.post(route, json=payload, headers=headers)
        assert response.status == 201
        assert response.headers["Cache-Control"] == "no-store"
        saved = await response.json()
        assert saved["created"] is True
        assert all(saved[name] for name in (
            "observation_id", "source_evidence_id", "quote_snapshot_id"
        ))
        response = await client.post(route, json=payload, headers=headers)
        assert response.status == 200
        assert await response.json() == {**saved, "created": False}
        response = await client.post(
            route, json={**payload, "price_amount": "1251.00"}, headers=headers
        )
        assert response.status == 409

        response = await client.get(f"/api/cases/{case_id}", headers=headers)
        assert response.status == 200
        detail = await response.json()
        offer = next(row for row in detail["offers"] if row["id"] == str(offer_id))
        assert offer["price_amount"] == payload["price_amount"]
        assert offer["price_unit"] == "рулон"
        assert offer["source_type"] == "manual"
        assert offer["source_evidence_id"] == saved["source_evidence_id"]
        assert offer["comparison"]["price_state"] == "operator_recorded"
        assert offer["comparison"]["rank"] is None
        assert detail["pipeline_metrics"]["comparable_quotes"] == 0

        await pool.execute("UPDATE procurement_cases SET status='closed' WHERE id=$1", case_id)
        response = await client.post(route, json=payload, headers=headers)
        assert response.status == 200
        assert await response.json() == {**saved, "created": False}
        response = await client.post(
            route, json={**payload, "idempotency_key": str(uuid4())}, headers=headers
        )
        assert response.status == 404
    assert dict(await pool.fetchrow(counts_sql)) == before
    assert await pool.fetchval(
        "SELECT observed_by FROM offer_observations WHERE offer_id=$1", offer_id
    ) == str(owner_id)
    assert await pool.fetchval(
        "SELECT count(*) FROM offer_observations WHERE offer_id=$1", offer_id
    ) == 1


@pytest.mark.postgres
async def test_case_detail_paginates_all_offers_without_side_effects(  # noqa: F811
    pipeline_db,  # noqa: F811
):
    pool, case_id, _owner_id, _run_id, _executor = pipeline_db
    item_ids = await pool.fetch(
        "SELECT id FROM request_items WHERE case_id=$1 ORDER BY line_number", case_id
    )
    assert len(item_ids) >= 4
    async with pool.acquire() as connection:
        evidence = await upsert_source_evidence(
            connection,
            SourceEvidenceInput(
                source_type="manual",
                source_locator=f"pagination-test:{uuid4()}",
                captured_at=datetime.now(UTC),
                content_sha256="b" * 64,
                raw_payload={"purpose": "pagination test"},
                idempotency_key=f"pagination-test:evidence:{uuid4()}",
            ),
        )
        supplier = await upsert_supplier(
            connection,
            SupplierInput(
                display_name="Pagination test supplier",
                identity_type="source_record",
                identity_value=f"pagination-test:{uuid4()}",
                source_evidence_id=evidence.id,
            ),
        )
    case_tag = uuid4()
    await pool.executemany(
        """INSERT INTO offers(
               request_item_id,supplier_id,status,supplier_product_name,model_sku,idempotency_key
           ) VALUES($1,$2,'lead',$3,$4,$5)""",
        [
            (
                item_ids[0]["id"],
                supplier.id,
                f"Pagination item {index}",
                f"P-{index}",
                f"pagination-test:{case_tag}:{index}",
            )
            for index in range(1001)
        ],
    )
    tail_offer_id = await pool.fetchval(
        """INSERT INTO offers(
               request_item_id,supplier_id,status,supplier_product_name,model_sku,idempotency_key
           ) VALUES($1,$2,'lead','Last item offer','TAIL',$3) RETURNING id""",
        item_ids[-1]["id"],
        supplier.id,
        f"pagination-test:{case_tag}:tail",
    )
    expected_ids = {
        str(row["id"])
        for row in await pool.fetch(
            """SELECT o.id FROM offers o JOIN request_items i ON i.id=o.request_item_id
               WHERE i.case_id=$1""",
            case_id,
        )
    }
    assert len(expected_ids) >= 1002

    foreign_owner = uuid4()
    await pool.execute(
        "INSERT INTO users(id,telegram_id,display_name) VALUES($1,54321,'Other owner')",
        foreign_owner,
    )
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",  # noqa: S106
        telegram_allowed_user_ids="12345,54321",
        waha_outbound_enabled=False,
    )
    headers = {
        "Authorization": "tma "
        + _signed_data("test-token", auth_date=int(time.time()), user_id=12345)
    }
    foreign_headers = {
        "Authorization": "tma "
        + _signed_data("test-token", auth_date=int(time.time()), user_id=54321)
    }
    counts_sql = """
        SELECT (SELECT count(*) FROM jobs) AS jobs,
               (SELECT count(*) FROM outbox_events) AS outbox,
               (SELECT count(*) FROM purchase_items) AS purchases,
               (SELECT count(*) FROM whatsapp_send_acceptance_journal) AS acceptances
    """
    before = dict(await pool.fetchrow(counts_sql))
    route = f"/api/cases/{case_id}"
    async with TestClient(TestServer(create_mini_app(pool, settings))) as client:
        first_response = await client.get(route, headers=headers)
        assert first_response.status == 200
        first = await first_response.json()
        assert first["offers_total"] == len(expected_ids)
        assert first["offers_offset"] == 0
        assert first["offers_next_offset"] == 1000
        assert first["offers_truncated"] is True
        assert len(first["offers"]) == 1000

        second_response = await client.get(f"{route}?offers_offset=1000", headers=headers)
        assert second_response.status == 200
        second = await second_response.json()
        assert second["offers_offset"] == 1000
        assert second["offers_next_offset"] is None
        assert second["offers_truncated"] is False
        all_returned_ids = {
            row["id"] for row in [*first["offers"], *second["offers"]]
        }
        assert all_returned_ids == expected_ids
        tail = next(row for row in second["offers"] if row["id"] == str(tail_offer_id))
        assert tail["request_item_id"] == str(item_ids[-1]["id"])

        invalid_response = await client.get(f"{route}?offers_offset=invalid", headers=headers)
        assert invalid_response.status == 400
        negative_offset = await client.get(f"{route}?offers_offset=-1", headers=headers)
        assert negative_offset.status == 400
        fractional_offset = await client.get(f"{route}?offers_offset=1.5", headers=headers)
        assert fractional_offset.status == 400
        huge_offset = await client.get(
            f"{route}?offers_offset={'9' * 5000}", headers=headers
        )
        assert huge_offset.status == 400
        out_of_range = await client.get(f"{route}?offers_offset=100001", headers=headers)
        assert out_of_range.status == 400
        foreign_response = await client.get(route, headers=foreign_headers)
        assert foreign_response.status == 404
    assert dict(await pool.fetchrow(counts_sql)) == before
