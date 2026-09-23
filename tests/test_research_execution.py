from __future__ import annotations

import hashlib
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

import procurement_bot.research_execution as execution_module
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.providers.browser import (
    BROWSER_SPEC_SHA256,
    BrowserProviderError,
    SupplierBrowserResult,
)
from procurement_bot.research import (
    AvailabilityStatus,
    ContactKind,
    EvidenceClaim,
    EvidenceType,
    ResearchSource,
    SupplierCandidate,
    SupplierContact,
    SupplierEvidence,
    build_research_plan,
)
from procurement_bot.research_execution import ResearchRunExecutor, _QueryBranch
from procurement_bot.suppliers import UpsertResult


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


def _plan():
    return build_research_plan(
        ParsedProcurementRequest(
            city="Алматы",
            search_scope=SearchScope.CITYWIDE,
            items=[ProcurementItem(name="Марля", quantity=10, unit="рулон")],
        )
    )


def _scope(plan: Any) -> dict[str, Any]:
    return {
        "contract_version": 1,
        "plan_sha256": plan.plan_sha256,
        "plan": plan.model_dump(mode="json", exclude={"plan_sha256"}),
    }


def _query_row(run_id: UUID, branch: Any, ordinal: int) -> dict[str, Any]:
    provider = {
        ResearchSource.OWN_DATABASE: "own_database",
        ResearchSource.TWO_GIS: "2gis_web",
        ResearchSource.WEB: "web",
    }[branch.source]
    return {
        "id": uuid4(),
        "research_run_id": run_id,
        "provider": provider,
        "query_text": branch.rendered_query,
        "city": branch.locality.city,
        "search_area_text": "",
        "latitude": None,
        "longitude": None,
        "radius_meters": None,
        "parameters": {
            "contract_version": 1,
            "branch_ordinal": ordinal,
            "item_index": branch.item_index,
            "source": branch.source.value,
            "purpose": branch.purpose.value,
            "query_terms": list(branch.query_terms),
            "locality": branch.locality.model_dump(mode="json"),
            "minimum_unique_candidates": branch.minimum_unique_candidates,
            "result_ttl_hours": branch.result_ttl_hours,
            "empty_page_limit": branch.empty_page_limit,
            "blocked_reason": branch.blocked_reason,
        },
    }


class _Connection:
    def __init__(self, *, mutate_query: bool = False, private_price: bool = False) -> None:
        plan = _plan()
        self.run_id = uuid4()
        self.case_id = uuid4()
        self.item_id = uuid4()
        search_scope = _scope(plan)
        if private_price:
            search_scope["plan"]["source_plans"][0]["rendered_query"] = (
                "Марля Алматы бюджет 100000"
            )
            encoded = json.dumps(
                search_scope["plan"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            search_scope["plan_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
        self.run = {
            "id": self.run_id,
            "case_id": self.case_id,
            "status": "running",
            "search_scope": search_scope,
            "error_code": "",
        }
        self.case = {"id": self.case_id, "status": "researching"}
        self.queries = [
            _query_row(self.run_id, branch, ordinal)
            for ordinal, branch in enumerate(plan.source_plans)
        ]
        if mutate_query:
            self.queries[1]["provider"] = "web"

    def transaction(self) -> AbstractAsyncContextManager[None]:
        return _Transaction()

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM research_runs" in sql and "WHERE id=$1" in sql:
            return self.run if args[0] == self.run_id else None
        if "FROM procurement_cases" in sql:
            return self.case if args[0] == self.case_id else None
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM research_queries" in sql:
            return self.queries
        if "FROM request_items" in sql:
            return [{"id": self.item_id, "line_number": 1}]
        raise AssertionError(f"unexpected fetch: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> int:
        if "count(DISTINCT o.supplier_id)" in sql:
            return 2
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        if "UPDATE procurement_cases" in sql:
            self.case["status"] = args[1]
            return "UPDATE 1"
        if "UPDATE research_runs" in sql:
            self.run["status"] = args[1]
            self.run["error_code"] = args[2]
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {sql}")


class _Browser:
    def __init__(self, *, failing_source: ResearchSource | None = None) -> None:
        self.failing_source = failing_source
        self.calls: list[Any] = []

    async def search_suppliers(self, plan: Any) -> SupplierBrowserResult:
        self.calls.append(plan)
        if plan.source == self.failing_source:
            raise BrowserProviderError("provider unavailable")
        return SupplierBrowserResult(
            spec_sha256=BROWSER_SPEC_SHA256,
            request_sha256=execution_module._request_hash(plan),
            candidates=[],
            exhausted=True,
            pages_examined=1,
        )

    async def resolve_locality(self, task: Any) -> Any:  # pragma: no cover - not used here
        raise AssertionError("locality resolution is not an execution operation")


@pytest.mark.asyncio
async def test_executor_verifies_queries_runs_read_only_branch_and_terminalizes() -> None:
    connection = _Connection()
    browser = _Browser()
    executor = ResearchRunExecutor(connection, browser)  # type: ignore[arg-type]

    result = await executor.execute(connection.run_id)

    assert result.status == "succeeded"
    assert result.branches_total == result.branches_succeeded == 3
    assert result.own_database_candidates == 2
    assert result.candidates_persisted == 0
    assert [call.source for call in browser.calls] == [ResearchSource.TWO_GIS, ResearchSource.WEB]
    assert connection.run["status"] == "succeeded"
    assert connection.case["status"] == "evaluating"


@pytest.mark.asyncio
async def test_plan_projection_drift_fails_closed_before_browser_call() -> None:
    connection = _Connection(mutate_query=True)
    browser = _Browser()

    result = await ResearchRunExecutor(  # type: ignore[arg-type]
        connection, browser
    ).execute(connection.run_id)

    assert result.status == "failed"
    assert result.error_code == "research_plan_drift"
    assert browser.calls == []
    assert connection.case["status"] == "ready"


@pytest.mark.asyncio
async def test_private_price_in_persisted_search_payload_fails_closed() -> None:
    connection = _Connection(private_price=True)
    browser = _Browser()

    result = await ResearchRunExecutor(  # type: ignore[arg-type]
        connection, browser
    ).execute(connection.run_id)

    assert result.status == "failed"
    assert result.error_code == "private_price_detected"
    assert browser.calls == []


@pytest.mark.asyncio
async def test_one_provider_failure_produces_partial_result() -> None:
    connection = _Connection()
    browser = _Browser(failing_source=ResearchSource.TWO_GIS)

    result = await ResearchRunExecutor(  # type: ignore[arg-type]
        connection, browser
    ).execute(connection.run_id)

    assert result.status == "partial"
    assert result.branches_succeeded == 2
    assert result.branches_failed == 1
    assert result.error_code == "source_failed"
    assert connection.case["status"] == "evaluating"


@pytest.mark.asyncio
async def test_browser_candidate_is_mapped_to_existing_supplier_apis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan().source_plans[2]
    query_id = uuid4()
    item_id = uuid4()
    evidence_id = uuid4()
    supplier_id = uuid4()
    location_id = uuid4()
    offer_id = uuid4()
    calls: dict[str, list[Any]] = {
        "evidence": [],
        "supplier": [],
        "contact": [],
        "location": [],
        "offer": [],
        "observation": [],
    }

    def capture(name: str, result_id: UUID):
        async def fake(*args: Any) -> UpsertResult:
            calls[name].append(args[-1])
            return UpsertResult(result_id, True)

        return fake

    monkeypatch.setattr(
        execution_module,
        "upsert_source_evidence",
        capture("evidence", evidence_id),
    )
    monkeypatch.setattr(execution_module, "upsert_supplier", capture("supplier", supplier_id))
    monkeypatch.setattr(execution_module, "upsert_supplier_contact", capture("contact", uuid4()))
    monkeypatch.setattr(
        execution_module,
        "upsert_supplier_location",
        capture("location", location_id),
    )
    monkeypatch.setattr(execution_module, "upsert_offer", capture("offer", offer_id))
    monkeypatch.setattr(
        execution_module,
        "upsert_offer_observation",
        capture("observation", uuid4()),
    )
    evidence = SupplierEvidence(
        evidence_type=EvidenceType.PRODUCT_PAGE,
        source=ResearchSource.WEB,
        source_url="https://med.example.kz/marlya",
        observed_at=datetime(2026, 9, 22, tzinfo=UTC),
        raw_snapshot_sha256="a" * 64,
        claims=[
            EvidenceClaim.PRODUCT_MATCH,
            EvidenceClaim.ADVERTISED_PRICE,
            EvidenceClaim.AVAILABILITY,
        ],
        availability=AvailabilityStatus.PUBLICLY_ADVERTISED,
        price_amount=Decimal("1200"),
        currency="KZT",
    )
    candidate = SupplierCandidate(
        source=ResearchSource.WEB,
        external_id="vendor-42",
        name="Медснаб",
        address="Алматы, ул. Абая, 1",
        contacts=[
            SupplierContact(
                kind=ContactKind.WHATSAPP,
                value="+7 777 123 45 67",
                confidence=0.9,
            )
        ],
        evidence=[evidence],
        product_availability=AvailabilityStatus.PUBLICLY_ADVERTISED,
    )
    result = SupplierBrowserResult(
        spec_sha256=BROWSER_SPEC_SHA256,
        request_sha256=execution_module._request_hash(plan),
        candidates=[candidate],
        exhausted=True,
        pages_examined=1,
    )
    executor = ResearchRunExecutor(object(), _Browser())  # type: ignore[arg-type]

    persisted = await executor._persist_branch(
        object(),  # type: ignore[arg-type]
        _QueryBranch(query_id, plan, item_id),
        result,
    )

    assert persisted == 1
    assert calls["evidence"][0].research_query_id == query_id
    assert calls["evidence"][0].content_sha256 == execution_module._digest(
        evidence.model_dump(mode="json")
    )
    assert calls["evidence"][0].content_sha256 != evidence.raw_snapshot_sha256
    assert calls["supplier"][0].identity_type == "source_record"
    assert calls["contact"][0].contact_type == "whatsapp"
    assert calls["location"][0].city == "Алматы"
    assert calls["offer"][0].status == "lead"
    observation = calls["observation"][0]
    assert observation.price_amount == Decimal("1200")
    assert observation.availability_status == "in_stock_unquantified"
