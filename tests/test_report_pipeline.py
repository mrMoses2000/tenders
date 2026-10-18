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
        self.requirement_id = uuid4()
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
                "tender_unit_price_private": Decimal("1500"),
                "tender_currency": "KZT",
            }
        ]
        self.requirements = [
            {
                "id": self.requirement_id,
                "request_item_id": self.item_id,
                "requirement_key": "ГОСТ",
                "is_hard": True,
                "source_pointer": "p1",
            }
        ]
        self.technical_reviews = [
            {"id": uuid4(), "offer_id": self.offer_id, "requirement_id": None,
             "verdict": "match", "created_at": self.now},
            {"id": uuid4(), "offer_id": self.offer_id,
             "requirement_id": self.requirement_id,
             "verdict": "match", "created_at": self.now},
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
                "pack_qty": None,
                "moq": None,
                "order_multiple": None,
                "available_qty": None,
                "stock_source_type": None,
                "source_type": "supplier_message",
                "supplier_phone": "+77000000009",
                "is_whatsapp_confirmed": True,
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
        if "FROM research_scope_revisions" in sql:
            return getattr(self, "scope_revision", None)
        if "FROM research_runs" in sql:
            if "WHERE id=$1" in sql:
                return self.run if args[0] == self.run_id else None
            return self.run if args[0] == self.case_id else None
        if "AS public_prices" in sql:
            return {"leads": 1, "public_prices": 1, "accepted": 0, "delivery_unknown": 0}
        if "WITH case_suppliers AS" in sql:
            return {"contacted": 0, "whatsapp_unavailable": 1, "whatsapp_failed": 0}
        if "FROM workflow_events" in sql:
            return self.events.get(str(args[0]))
        if "FROM report_artifacts" in sql:
            return self.reports.get(args[0])  # type: ignore[arg-type]
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if "FROM request_items" in sql:
            return self.items
        if "FROM item_requirements r" in sql:
            assert args[0] == self.case_id
            return self.requirements
        if "FROM offer_technical_reviews review" in sql:
            assert args[0] == self.case_id
            return self.technical_reviews
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

    async def pipeline_health(_connection: object, _case_id: UUID) -> dict[str, Any]:
        return {
            "account": {},
            "ingress": {"accepted_7d": 0, "ignored_7d": 0, "rejected_7d": 0},
            "research": {
                "run_status": "running",
                "last_real_progress_at": "2026-09-23T03:00:00+00:00",
                "wait_reason": "queue_job_active_or_scheduled",
                "next_wake_at": None,
                "queued_without_runnable_job": 0,
                "queued_with_terminal_job": 0,
                "stalled": False,
            },
        }

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
    monkeypatch.setattr(pipeline, "load_pipeline_health", pipeline_health)
    return calls


@pytest.mark.asyncio
async def test_revised_report_labels_citywide_priority_without_changing_case_area(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    old_area = connection.case["search_area_text"]
    connection.scope_revision = {
        "city": "Алматы", "preferred_area_text": "Северное Кольцо",
        "revision_sha256": "f" * 64,
    }
    calls = await _install_artifact_fake(monkeypatch, connection)
    await build_case_report(
        connection, results_root=tmp_path, case_id=connection.case_id,
        research_run_id=connection.run_id,
    )
    assert "Весь город Алматы; приоритет: Северное Кольцо" in calls[0]["html"]
    assert connection.case["search_area_text"] == old_area


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
    assert "Состояние автоматизации" in html
    assert "Последний сохранённый прогресс поиска" in html
    assert "queue_job_active_or_scheduled" in html
    assert "ТЗ проверено оператором" in html
    assert '<span class="rank">' not in html
    assert "Уточнить единицу цены" in html
    assert "Уточнить НДС" not in html
    assert "ob.observed_at DESC" in "\n".join(connection.sql)
    offers_sql = next(sql for sql in connection.sql if "FROM offers AS o" in sql)
    assert "contact_evidence" in offers_sql
    assert "rr.case_id=ri.case_id" in offers_sql
    assert "offer_observations own_observation" in offers_sql
    assert "contact_evidence_ids" in offers_sql
    contact_metrics_sql = next(sql for sql in connection.sql if "WITH case_suppliers AS" in sql)
    assert "contact_evidence" in contact_metrics_sql
    assert "rr.case_id=$1" in contact_metrics_sql
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
    connection.technical_reviews = []
    calls = await _install_artifact_fake(monkeypatch, connection)

    result = await build_case_report(
        connection,  # type: ignore[arg-type]
        results_root=tmp_path,
        case_id=connection.case_id,
    )

    html = calls[0]["html"]
    assert result.exact_matches == 0
    assert "ТЗ не проверено" in html
    assert "Цена не подтверждена" in html
    assert "наличие не подтверждено" in html
    assert '<span class="rank">' not in html


@pytest.mark.asyncio
async def test_partial_research_report_is_explicit_and_keeps_case_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(run_status="running")
    connection.case["status"] = "researching"
    connection.offers[0]["supplier_phone"] = None
    calls = await _install_artifact_fake(monkeypatch, connection)
    kwargs = dict(
        results_root=tmp_path,
        case_id=connection.case_id,
        research_run_id=connection.run_id,
        refresh_key="pilot-three-items-v1",
        partial_research_snapshot=True,
    )
    first = await build_case_report(connection, **kwargs)  # type: ignore[arg-type]
    retry = await build_case_report(connection, **kwargs)  # type: ignore[arg-type]

    assert connection.case["status"] == "researching"
    assert "Промежуточный срез" in calls[0]["html"]
    assert "Сохранённые лиды заявки: по 1 из 1 позиций" in calls[0]["html"]
    assert "телефоны указаны у лидов по 0 позициям" in calls[0]["html"]
    assert "могут относиться к более ранним поискам" in calls[0]["html"]
    assert "Промежуточный" in connection.outbox[0]["payload"]["caption"]
    assert len(calls) == 1
    assert retry.artifact_id == first.artifact_id
    assert not retry.newly_built


@pytest.mark.asyncio
async def test_partial_report_with_55_items_and_no_offers_marks_each_item_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(run_status="running")
    connection.case["status"] = "researching"
    first = connection.items[0]
    connection.items = [
        {**first, "line_number": number, "name": f"Позиция {number}",
         "id": first["id"] if number == 1 else uuid4()}
        for number in range(1, 56)
    ]
    connection.offers = []
    calls = await _install_artifact_fake(monkeypatch, connection)

    await build_case_report(
        connection,  # type: ignore[arg-type]
        results_root=tmp_path,
        case_id=connection.case_id,
        research_run_id=connection.run_id,
        refresh_key="pilot-empty-55-v1",
        partial_research_snapshot=True,
    )

    html = calls[0]["html"]
    assert html.count('<section class="item-group">') == 55
    assert html.count("Предложений пока нет.") == 55
    assert "Позиция 55: Позиция 55" in html
    assert "Требуется: 20 рулон" in html
    assert "Техспек" in html
    assert "Сохранённые лиды заявки: по 0 из 55 позиций" in html
    assert "<article>" not in html


@pytest.mark.asyncio
async def test_partial_report_requires_distinct_refresh_identity(tmp_path: Path) -> None:
    connection = _Connection(run_status="running")
    with pytest.raises(ValueError, match="refresh key"):
        await build_case_report(
            connection,  # type: ignore[arg-type]
            results_root=tmp_path,
            case_id=connection.case_id,
            partial_research_snapshot=True,
        )
    assert connection.sql == []
