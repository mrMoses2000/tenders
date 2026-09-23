from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

import procurement_bot.report_pipeline as pipeline
from procurement_bot.queue import stable_key
from procurement_bot.report_artifacts import StoredReportArtifact
from procurement_bot.report_pipeline import ReportBuildRejected, build_case_report


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


class _Connection:
    def __init__(self, *, run_status: str = "succeeded") -> None:
        self.case_id = uuid4()
        self.run_id = uuid4()
        self.item_id = uuid4()
        self.offer_id = uuid4()
        self.supplier_id = uuid4()
        self.now = datetime(2026, 9, 23, 4, 0, tzinfo=UTC)
        self.case: dict[str, Any] = {
            "id": self.case_id,
            "status": "evaluating",
            "title": "Марля стерильная",
            "city": "Алматы",
            "search_area_text": "Первая Алматы",
            "created_at": self.now,
            "updated_at": self.now,
            "telegram_id": 123456,
            "owner_active": True,
        }
        self.run: dict[str, Any] = {
            "id": self.run_id,
            "case_id": self.case_id,
            "status": run_status,
            "started_at": self.now,
            "completed_at": self.now,
        }
        self.items = [
            {
                "id": self.item_id,
                "line_number": 1,
                "name": "Марля",
                "specification_text": "ГОСТ, 10 м",
                "quantity": Decimal("20"),
                "unit": "рулон",
                "analogs_allowed": False,
                "status": "offers_found",
            }
        ]
        self.requirements = [
            {
                "request_item_id": self.item_id,
                "requirement_key": "ГОСТ",
                "is_hard": True,
                "source_pointer": "p1",
            }
        ]
        self.offers = [
            {
                "offer_id": self.offer_id,
                "offer_status": "exact",
                "supplier_product_name": "Марля ГОСТ 10 м",
                "model_sku": "M-10",
                "supplier_id": self.supplier_id,
                "supplier_name": "Медснаб",
                "request_item_id": self.item_id,
                "line_number": 1,
                "item_name": "Марля",
                "specification_text": "ГОСТ, 10 м",
                "quantity": Decimal("20"),
                "unit": "рулон",
                "analogs_allowed": False,
                "location_city": "Алматы",
                "address_text": "ул. Райымбека, 1",
                "district": "Турксибский район",
                "market_name": "Первая Алматы",
                "pavilion": "A-12",
                "price_amount": Decimal("1250"),
                "currency": "KZT",
                "price_unit": None,
                "availability_status": "in_stock_unquantified",
                "vat_status": "unknown",
                "delivery_status": "unknown",
                "observed_at": self.now,
                "evidence_title": "Ответ поставщика",
                "source_locator": "whatsapp:default:chat:message",
                "source_url": None,
            }
        ]
        self.events: dict[str, dict[str, Any]] = {}
        self.reports: dict[UUID, dict[str, Any]] = {}
        self.outbox: list[dict[str, Any]] = []
        self.sql: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, sql: str, *args: object) -> str:
        self.sql.append(sql)
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if "UPDATE procurement_cases" in sql:
            if self.case["status"] != "evaluating":
                return "UPDATE 0"
            self.case["status"] = "report_ready"
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        self.sql.append(sql)
        if "FROM procurement_cases AS c" in sql:
            return self.case if args[0] == self.case_id else None
        if "FROM research_runs" in sql:
            if "WHERE id=$1" in sql:
                return self.run if args[0] == self.run_id else None
            return self.run if args[0] == self.case_id else None
        if "FROM workflow_events" in sql:
            return self.events.get(str(args[0]))
        if "FROM report_artifacts" in sql:
            return self.reports.get(args[0])  # type: ignore[arg-type]
        if "WITH case_suppliers AS" in sql:
            return {"contacted": 0, "whatsapp_unavailable": 1, "whatsapp_failed": 0}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if "FROM request_items" in sql:
            return self.items
        if "FROM item_requirements" in sql:
            assert args[0] == [self.item_id]
            return self.requirements
        if "FROM offers AS o" in sql:
            assert args[0] == self.case_id
            return self.offers
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> UUID | int | None:
        self.sql.append(sql)
        if "count(DISTINCT conversation.supplier_id)" in sql:
            return 1
        if "INSERT INTO workflow_events" in sql:
            case_id, event_type, event_key, data = args
            assert case_id == self.case_id
            if str(event_key) in self.events:
                return None
            self.events[str(event_key)] = {"event_type": event_type, "data": data}
            return 1
        if "INSERT INTO outbox_events" in sql:
            event_id, event_type, payload, key, max_attempts, approval_id = args
            assert approval_id is None
            self.outbox.append(
                {
                    "id": event_id,
                    "event_type": event_type,
                    "payload": payload,
                    "key": key,
                    "max_attempts": max_attempts,
                }
            )
            return event_id  # type: ignore[return-value]
        raise AssertionError(f"unexpected fetchval SQL: {sql}")


async def _install_artifact_fake(
    monkeypatch: pytest.MonkeyPatch,
    connection: _Connection,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def persist(
        _connection: object,
        **kwargs: Any,
    ) -> StoredReportArtifact:
        assert _connection is connection
        calls.append(kwargs)
        artifact = StoredReportArtifact(
            id=uuid4(),
            case_id=connection.case_id,
            version=1,
            storage_path="reports/aa/report.html",
            sha256="a" * 64,
            byte_size=len(kwargs["html"].encode()),
            created=True,
        )
        connection.reports[artifact.id] = {
            "id": artifact.id,
            "case_id": artifact.case_id,
            "research_run_id": kwargs["research_run_id"],
            "version": artifact.version,
            "storage_path": artifact.storage_path,
            "sha256": artifact.sha256,
            "byte_size": artifact.byte_size,
            "summary": kwargs["summary"],
        }
        return artifact

    monkeypatch.setattr(pipeline, "persist_report_artifact", persist)
    return calls


@pytest.mark.asyncio
async def test_builds_private_report_without_inventing_total_cost_rank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    calls = await _install_artifact_fake(monkeypatch, connection)

    result = await build_case_report(
        connection,  # type: ignore[arg-type]
        results_root=tmp_path,
        case_id=connection.case_id,
        research_run_id=connection.run_id,
    )

    assert result.newly_built is True
    assert result.suppliers_found == result.suppliers_responded == result.exact_matches == 1
    assert connection.case["status"] == "report_ready"
    assert len(calls) == 1
    html = calls[0]["html"]
    assert "1 250.00 KZT" in html
    assert "соответствие подтверждено" in html
    assert '<span class="rank">' not in html
    assert "Уточнить, за какую единицу указана цена" in html
    assert "Уточнить, включён ли НДС" in html
    assert "latest.observed_at DESC" in "\n".join(connection.sql)
    assert connection.outbox[0]["event_type"] == "telegram.send_document"
    assert connection.outbox[0]["payload"]["storage_path"] == "reports/aa/report.html"
    assert connection.outbox[0]["payload"]["filename"] == "otchet-po-zayavke.html"
    caption = connection.outbox[0]["payload"]["caption"]
    assert "SHA-256" not in caption
    assert str(connection.case_id)[:8] not in caption


@pytest.mark.asyncio
async def test_retry_returns_persisted_artifact_without_rebuilding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    calls = await _install_artifact_fake(monkeypatch, connection)
    first = await build_case_report(
        connection,  # type: ignore[arg-type]
        results_root=tmp_path,
        case_id=connection.case_id,
        research_run_id=connection.run_id,
    )
    first_outbox_count = len(connection.outbox)

    retry = await build_case_report(
        connection,  # type: ignore[arg-type]
        results_root=tmp_path,
        case_id=connection.case_id,
        research_run_id=connection.run_id,
    )

    assert retry.artifact_id == first.artifact_id
    assert retry.newly_built is False
    assert len(calls) == 1
    assert len(connection.outbox) == first_outbox_count
    event_key = stable_key(
        "workflow.report-built",
        connection.case_id,
        connection.run_id,
    )
    assert event_key in connection.events


@pytest.mark.asyncio
async def test_nonterminal_or_foreign_research_run_is_rejected_before_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(run_status="running")
    calls = await _install_artifact_fake(monkeypatch, connection)

    with pytest.raises(ReportBuildRejected, match="not complete enough"):
        await build_case_report(
            connection,  # type: ignore[arg-type]
            results_root=tmp_path,
            case_id=connection.case_id,
            research_run_id=connection.run_id,
        )

    assert calls == []
    assert connection.case["status"] == "evaluating"
    assert connection.outbox == []


@pytest.mark.asyncio
async def test_unknown_offer_remains_unconfirmed_and_unranked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    connection.offers[0].update(
        offer_status="lead",
        price_amount=None,
        currency=None,
        availability_status="unknown",
        observed_at=None,
        evidence_title=None,
        source_locator=None,
    )
    calls = await _install_artifact_fake(monkeypatch, connection)

    result = await build_case_report(
        connection,  # type: ignore[arg-type]
        results_root=tmp_path,
        case_id=connection.case_id,
    )

    html = calls[0]["html"]
    assert result.exact_matches == 0
    assert "требуется подтверждение" in html
    assert "Цена не подтверждена" in html
    assert "наличие не подтверждено" in html
    assert '<span class="rank">' not in html
