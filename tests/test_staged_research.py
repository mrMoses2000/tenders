from __future__ import annotations

import json
import os
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from pydantic import ValidationError

from procurement_bot.db import run_migrations
from procurement_bot.errors import RetryableProviderError
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.providers.browser import (
    BROWSER_SPEC_SHA256,
    BrowserProviderError,
    SupplierBrowserResult,
    UnsafeBrowserResult,
)
from procurement_bot.providers.staged_research import (
    DiscoveryPage,
    StagedResearchProvider,
    prepare_discovery,
    validate_discovery,
)
from procurement_bot.research import (
    ContactKind,
    EvidenceClaim,
    EvidenceType,
    ResearchPurpose,
    ResearchSource,
    SupplierCandidate,
    SupplierContact,
    SupplierEvidence,
    build_research_plan,
)
from procurement_bot.research_execution import _request_hash
from procurement_bot.research_repository import mark_research_running, persist_research_plan


def _plan(*, generation: int = 7, candidate_limit: int = 2):
    research_plan = build_research_plan(
        ParsedProcurementRequest(
            city="Алматы",
            search_scope=SearchScope.CITYWIDE,
            items=[ProcurementItem(name="Кисточка 80 мм", quantity=10, unit="шт")],
        )
    )
    branch = next(
        item for item in research_plan.source_plans if item.source == ResearchSource.TWO_GIS
    )
    return branch.model_copy(
        update={
            "execution_query_id": uuid4(),
            "execution_generation": generation,
            "batch_candidate_limit": candidate_limit,
        }
    )


def _candidate(external_id: str) -> SupplierCandidate:
    return SupplierCandidate(
        source=ResearchSource.TWO_GIS,
        external_id=external_id,
        name=f"Store {external_id}",
        address="Almaty",
        evidence=[
            SupplierEvidence(
                evidence_type=EvidenceType.BUSINESS_LISTING,
                source=ResearchSource.TWO_GIS,
                source_url=f"https://2gis.kz/almaty/firm/{external_id}",
                observed_at=datetime.now(UTC),
                raw_snapshot_sha256="a" * 64,
                claims=[EvidenceClaim.BUSINESS_IDENTITY, EvidenceClaim.ADDRESS],
            )
        ],
    )


def _candidate_with_contact(external_id: str) -> SupplierCandidate:
    candidate = _candidate(external_id)
    card_url = f"https://2gis.kz/almaty/firm/{external_id}"
    contact_evidence = SupplierEvidence(
        evidence_type=EvidenceType.BUSINESS_LISTING,
        source=ResearchSource.TWO_GIS,
        source_url=card_url,
        observed_at=datetime.now(UTC),
        raw_snapshot_sha256="e" * 64,
        claims=[EvidenceClaim.BUSINESS_IDENTITY, EvidenceClaim.CONTACT],
    )
    return candidate.model_copy(
        update={
            "contacts": [
                SupplierContact(
                    kind=ContactKind.PHONE,
                    value="+77000000019",
                    confidence=0.95,
                )
            ],
            "contact_discovery_reason": "phone_found",
            "evidence": [*candidate.evidence, contact_evidence],
        }
    )


class _SeparateStagePrimary:
    def __init__(
        self,
        *,
        contact_failures: int = 0,
        pause_on_contacts: bool = False,
        connection: _Connection | None = None,
    ) -> None:
        self.contact_failures = contact_failures
        self.pause_on_contacts = pause_on_contacts
        self.connection = connection
        self.stages: list[str] = []

    async def execute(self, call):
        request = json.loads(call.untrusted_request_json)
        stage = request["execution_stage"]
        self.stages.append(stage)
        if stage == "discover_page":
            return _page_payload(call)
        if stage == "read_card":
            candidate = _candidate("101")
        elif stage == "read_contacts":
            if self.pause_on_contacts:
                self.connection.group_status = "paused"
            if self.contact_failures:
                self.contact_failures -= 1
                raise BrowserProviderError("AGY browser CLI timeout")
            candidate = _candidate_with_contact("101")
        else:
            raise AssertionError(f"unexpected stage: {stage}")
        return {
            "spec_sha256": call.spec_sha256,
            "request_sha256": call.request_sha256,
            "candidates": [candidate.model_dump(mode="json")],
            "exhausted": True,
            "pages_examined": 1,
        }


def _page_payload(
    call,
    ids: tuple[str, ...] = ("101",),
    *,
    exhausted: bool = True,
    end_of_results_observed: bool | None = None,
):
    return {
        "spec_sha256": call.spec_sha256,
        "request_sha256": call.request_sha256,
        "cards": [
            {
                "external_id": external_id,
                "url": f"https://2gis.kz/almaty/firm/{external_id}",
                "name": f"Store {external_id}",
            }
            for external_id in ids
        ],
        "page_url": "https://2gis.kz/almaty/search/paint",
        "raw_snapshot_sha256": "b" * 64,
        "pages_examined": 1,
        "next_cursor": "https://2gis.kz/almaty/search?cursor=next-page" if not exhausted else None,
        "exhausted": exhausted,
        "end_of_results_observed": (
            exhausted if end_of_results_observed is None else end_of_results_observed
        ),
    }


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
    def __init__(self, query_id) -> None:
        self.query_id = query_id
        self.pages: dict[tuple[Any, str], dict[str, Any]] = {}
        self.query_ids = {query_id}
        self.directory_pages: dict[str, dict[str, Any]] = {}
        self.cards: dict[tuple[Any, int], dict[str, Any]] = {}
        self.fence_status = "researching"
        self.group_status = "researching"
        self.run_status = "running"
        self.owner_generation = 7

    def transaction(self) -> AbstractAsyncContextManager[None]:
        return _Transaction()

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM research_discovery_pages" in sql and "request_sha256=$2" in sql:
            return self.pages.get((args[0], args[1]))
        if "FROM supplier_directory_page_pool" in sql:
            return self.directory_pages.get(args[0])
        if "FROM research_branch_checkpoints cp" in sql:
            if args[0] not in self.query_ids:
                return None
            return {
                "status": self.fence_status,
                "group_status": self.group_status,
                "run_status": self.run_status,
                "owner_generation": self.owner_generation,
            }
        if "INSERT INTO research_discovery_pages" in sql:
            query_id, request_hash, payload = args
            key = (query_id, request_hash)
            page = self.pages.get(key)
            if page is None:
                page = {"id": uuid4(), "payload": payload}
                self.pages[key] = page
            return page
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM research_discovery_cards WHERE page_id=$1" in sql:
            return sorted(
                (row for (page_id, _), row in self.cards.items() if page_id == args[0]),
                key=lambda row: row["ordinal"],
            )
        raise AssertionError(f"unexpected fetch SQL: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if "INSERT INTO supplier_directory_page_pool" in sql:
            cache_key, _category, _locality, _cursor, _ttl, payload = args
            self.directory_pages[cache_key] = {"payload": payload}
            return "INSERT 0 1"
        if "SET card_facts_payload=$3" in sql:
            page_id, ordinal, payload = args
            self.cards[(page_id, ordinal)]["card_facts_payload"] = payload
            return "UPDATE 1"
        if "UPDATE research_discovery_cards SET status='extracted'" in sql:
            page_id, ordinal, payload = args
            card = self.cards[(page_id, ordinal)]
            card["status"] = "extracted"
            card["attempts"] += 1
            card["result_payload"] = payload
            card["failure_code"] = None
            return "UPDATE 1"
        if "INSERT INTO research_discovery_cards" in sql:
            page_id, ordinal, external_id, card_url, card_name = args
            self.cards.setdefault(
                (page_id, ordinal),
                {
                    "page_id": page_id,
                    "ordinal": ordinal,
                    "external_id": external_id,
                    "card_url": card_url,
                    "card_name": card_name,
                    "status": "pending",
                    "result_payload": None,
                    "card_facts_payload": None,
                    "contact_attempts": 0,
                    "contact_failure_code": None,
                    "attempts": 0,
                    "failure_code": None,
                },
            )
            return "INSERT 0 1"
        if "UPDATE research_discovery_cards SET attempts=attempts+1" in sql:
            page_id, ordinal, failure_code = args
            card = self.cards[(page_id, ordinal)]
            card["attempts"] += 1
            card["status"] = "failed" if card["attempts"] >= 2 else "pending"
            card["failure_code"] = failure_code
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> int:
        if "contact_attempts=contact_attempts+1" in sql:
            page_id, ordinal, failure_code = args
            card = self.cards[(page_id, ordinal)]
            card["contact_attempts"] += 1
            card["contact_failure_code"] = failure_code
            return card["contact_attempts"]
        raise AssertionError(f"unexpected fetchval SQL: {sql}")


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Primary:
    def __init__(self, ids: tuple[str, ...] = ("101",), *, exhausted: bool = True) -> None:
        self.ids = ids
        self.exhausted = exhausted
        self.calls = 0

    async def execute(self, call):
        self.calls += 1
        return _page_payload(call, self.ids, exhausted=self.exhausted)


class _MalformedPrimary:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, call):
        self.calls += 1
        return {"not": "a discovery page"}


class _Legacy:
    def __init__(self, failures: dict[str, int] | None = None) -> None:
        self.failures = dict(failures or {})
        self.calls: list[str] = []

    async def search_suppliers(self, plan):
        card_url = plan.rendered_query
        external_id = card_url.rsplit("/", 1)[-1]
        self.calls.append(external_id)
        if self.failures.get(external_id, 0):
            self.failures[external_id] -= 1
            raise BrowserProviderError(f"card timeout {external_id}")
        return SupplierBrowserResult(
            spec_sha256=BROWSER_SPEC_SHA256,
            request_sha256=_request_hash(plan),
            candidates=[_candidate(external_id)],
            exhausted=True,
            pages_examined=1,
        )


@pytest.mark.asyncio
async def test_page_is_durable_before_card_timeout_and_same_card_resumes() -> None:
    plan = _plan()
    connection = _Connection(plan.execution_query_id)
    primary = _Primary()
    legacy = _Legacy({"101": 1})
    provider = StagedResearchProvider(_Pool(connection), legacy, primary)

    first = await provider.search_suppliers(plan)

    assert len(connection.pages) == 1
    assert connection.cards[next(iter(connection.cards))]["attempts"] == 1
    assert connection.cards[next(iter(connection.cards))]["status"] == "pending"
    assert first.candidates == []
    assert first.progress_token is not None
    assert primary.calls == 1

    resumed = await provider.search_suppliers(plan)

    assert primary.calls == 1
    assert legacy.calls == ["101", "101"]
    assert [candidate.external_id for candidate in resumed.candidates] == ["101"]
    assert resumed.exhausted is True
    assert resumed.request_sha256 == _request_hash(plan)
    assert resumed.progress_token != first.progress_token


@pytest.mark.asyncio
async def test_cached_card_and_second_card_resume_without_reextracting_first() -> None:
    plan = _plan()
    connection = _Connection(plan.execution_query_id)
    primary = _Primary(("101", "202"))
    legacy = _Legacy({"202": 1})
    provider = StagedResearchProvider(_Pool(connection), legacy, primary)

    first = await provider.search_suppliers(plan)
    second = await provider.search_suppliers(plan)

    assert [candidate.external_id for candidate in first.candidates] == ["101"]
    assert first.exhausted is False
    assert first.progress_token is not None
    assert [candidate.external_id for candidate in second.candidates] == ["101", "202"]
    assert second.exhausted is True
    assert legacy.calls == ["101", "202", "202"]
    assert primary.calls == 1
    assert second.request_sha256 == _request_hash(plan)
    assert second.progress_token != first.progress_token


@pytest.mark.asyncio
async def test_shared_2gis_page_rebinds_hashes_but_reextracts_for_current_item() -> None:
    first_plan = _plan()
    second_plan = first_plan.model_copy(
        update={
            "execution_query_id": uuid4(),
            "item_index": first_plan.item_index + 1,
            "purpose": ResearchPurpose.PRODUCT_EVIDENCE,
            "rendered_query": "лак для дерева Алматы",
        }
    )
    connection = _Connection(first_plan.execution_query_id)
    connection.query_ids = {first_plan.execution_query_id, second_plan.execution_query_id}
    primary = _Primary()
    legacy = _Legacy()
    provider = StagedResearchProvider(_Pool(connection), legacy, primary)

    first = await provider.search_suppliers(first_plan)
    second = await provider.search_suppliers(second_plan)

    assert [candidate.external_id for candidate in first.candidates] == ["101"]
    assert [candidate.external_id for candidate in second.candidates] == ["101"]
    assert primary.calls == 1
    # Discovery descriptors are pooled; card facts/results remain per-query.
    assert legacy.calls == ["101", "101"]
    assert len(connection.pages) == 2
    first_call = prepare_discovery(first_plan)
    second_call = prepare_discovery(second_plan)
    first_page = connection.pages[(first_plan.execution_query_id, first_call.request_sha256)]
    second_page = connection.pages[(second_plan.execution_query_id, second_call.request_sha256)]
    assert first_page["payload"]["request_sha256"] == first_call.request_sha256
    assert second_page["payload"]["request_sha256"] == second_call.request_sha256
    assert first_page["payload"]["request_sha256"] != second_page["payload"]["request_sha256"]


@pytest.mark.asyncio
async def test_card_facts_are_durable_before_contact_timeout_and_reused_on_retry() -> None:
    plan = _plan()
    connection = _Connection(plan.execution_query_id)
    primary = _SeparateStagePrimary(contact_failures=1)
    provider = StagedResearchProvider(
        _Pool(connection),
        _Legacy(),
        primary,
        separate_contacts=True,
    )

    first = await provider.search_suppliers(plan)
    card = next(iter(connection.cards.values()))
    assert first.candidates == []
    assert card["card_facts_payload"]["external_id"] == "101"
    assert card["result_payload"] is None
    assert card["contact_attempts"] == 1
    assert primary.stages == ["discover_page", "read_card", "read_contacts"]

    resumed = await provider.search_suppliers(plan)

    saved = SupplierCandidate.model_validate(card["card_facts_payload"])
    assert [candidate.external_id for candidate in resumed.candidates] == ["101"]
    assert primary.stages == [
        "discover_page",
        "read_card",
        "read_contacts",
        "read_contacts",
    ]
    assert resumed.candidates[0].contact_discovery_reason == "phone_found"
    assert any(
        EvidenceClaim.CONTACT in evidence.claims for evidence in resumed.candidates[0].evidence
    )
    assert saved.contacts == []
    assert card["contact_attempts"] == 1


@pytest.mark.asyncio
async def test_invalid_card_facts_have_bounded_retries_without_accepting_evidence() -> None:
    plan = _plan()
    connection = _Connection(plan.execution_query_id)

    class InvalidFactsPrimary(_SeparateStagePrimary):
        async def execute(self, call):
            raw = await super().execute(call)
            if json.loads(call.untrusted_request_json)["execution_stage"] == "read_card":
                raw["pages_examined"] = "not-a-number"
            return raw

    primary = InvalidFactsPrimary()
    provider = StagedResearchProvider(_Pool(connection), _Legacy(), primary, separate_contacts=True)
    first = await provider.search_suppliers(plan)
    card = next(iter(connection.cards.values()))
    assert first.candidates == []
    assert first.exhausted is False
    assert card["status"] == "pending"
    assert card["attempts"] == 1
    second = await provider.search_suppliers(plan)
    assert second.candidates == []
    assert second.exhausted is False
    assert card["status"] == "failed"
    assert card["attempts"] == 2
    assert card["failure_code"] == "ValidationError"
    assert card["card_facts_payload"] is None
    assert "read_contacts" not in primary.stages


@pytest.mark.asyncio
async def test_exact_card_instructions_preserve_evidence_semantics_for_both_stages() -> None:
    class CapturePrimary:
        def __init__(self):
            self.calls = []

        async def execute(self, call):
            self.calls.append(call)
            return {
                "spec_sha256": call.spec_sha256,
                "request_sha256": call.request_sha256,
                "candidates": [],
                "exhausted": True,
                "pages_examined": 1,
            }

    primary = CapturePrimary()
    provider = StagedResearchProvider(None, _Legacy(), primary)
    plan = _plan()

    for stage in ("read_card", "read_contacts"):
        await provider._read_stage(plan, stage)

    assert len(primary.calls) == 2
    for call, stage in zip(primary.calls, ("read_card", "read_contacts"), strict=True):
        instruction = call.trusted_instruction
        assert stage in instruction
        assert "business_identity, address, contact, category, opening_hours, and reputation" \
            in instruction
        assert "keep availability unknown and price_amount and currency null" in instruction
        assert "A business category is not product_match" in instruction
        assert "separate product_page evidence" in instruction
        assert "Never claim confirmed stock" in instruction
        assert "supplier_statement only for a real supplier response" in instruction


def test_business_listing_rejects_product_price_claims_but_product_page_keeps_price() -> None:
    observed_at = datetime.now(UTC).isoformat()
    business_listing = {
        "evidence_type": "business_listing",
        "source": "2gis",
        "source_url": "https://2gis.kz/almaty/firm/101",
        "observed_at": observed_at,
        "raw_snapshot_sha256": "f" * 64,
        "claims": ["business_identity", "product_match", "advertised_price"],
        "price_amount": "1250",
        "currency": "KZT",
    }
    with pytest.raises(ValidationError, match="business listing cannot prove"):
        SupplierEvidence.model_validate(business_listing)

    product_page = SupplierEvidence.model_validate({
        **business_listing,
        "evidence_type": "product_page",
        "claims": ["product_match", "advertised_price"],
    })
    assert product_page.price_amount == 1250
    assert product_page.currency == "KZT"


@pytest.mark.asyncio
async def test_two_contact_timeouts_keep_card_as_lead_with_explicit_failure_reason() -> None:
    plan = _plan()
    connection = _Connection(plan.execution_query_id)
    primary = _SeparateStagePrimary(contact_failures=2)
    provider = StagedResearchProvider(
        _Pool(connection),
        _Legacy(),
        primary,
        separate_contacts=True,
    )

    assert (await provider.search_suppliers(plan)).candidates == []
    result = await provider.search_suppliers(plan)

    card = next(iter(connection.cards.values()))
    assert len(result.candidates) == 1
    assert result.candidates[0].contact_discovery_reason == "provider_timeout"
    assert result.candidates[0].contacts == []
    assert result.candidates[0].contact_discovery_reason != "contact_not_published"
    assert card["status"] == "extracted"
    assert card["contact_attempts"] == 2
    assert card["contact_failure_code"] == "cli_timeout"


@pytest.mark.asyncio
async def test_resume_after_second_contact_failure_does_not_make_third_call() -> None:
    plan = _plan()
    connection = _Connection(plan.execution_query_id)
    primary = _SeparateStagePrimary(contact_failures=1)
    provider = StagedResearchProvider(
        _Pool(connection),
        _Legacy(),
        primary,
        separate_contacts=True,
    )

    assert (await provider.search_suppliers(plan)).candidates == []
    card = next(iter(connection.cards.values()))
    assert card["card_facts_payload"] is not None

    # Model a restart after the second failure counter was committed but before
    # the caller persisted the resulting card candidate.
    card["contact_attempts"] = 2
    card["contact_failure_code"] = "cli_timeout"
    resumed = await provider.search_suppliers(plan)

    assert len(resumed.candidates) == 1
    assert resumed.candidates[0].contact_discovery_reason == "provider_timeout"
    assert resumed.candidates[0].contacts == []
    assert primary.stages == ["discover_page", "read_card", "read_contacts"]
    assert card["status"] == "extracted"


@pytest.mark.asyncio
async def test_separate_contact_stage_is_fenced_before_call_and_before_result_commit() -> None:
    plan = _plan()
    connection = _Connection(plan.execution_query_id)
    primary = _SeparateStagePrimary(connection=connection)
    provider = StagedResearchProvider(
        _Pool(connection),
        _Legacy(),
        primary,
        separate_contacts=True,
    )

    connection.group_status = "paused"
    with pytest.raises(RetryableProviderError, match="ownership changed"):
        await provider.search_suppliers(plan)
    assert primary.stages == []

    connection.group_status = "researching"
    primary.pause_on_contacts = True
    with pytest.raises(RetryableProviderError, match="ownership changed"):
        await provider.search_suppliers(plan)

    card = next(iter(connection.cards.values()))
    assert primary.stages == ["discover_page", "read_card", "read_contacts"]
    assert card["card_facts_payload"] is not None
    assert card["result_payload"] is None
    assert card["status"] == "pending"


def test_discovery_urls_id_pagination_and_exhaustion_are_validated() -> None:
    plan = _plan()
    call = prepare_discovery(plan)
    valid = _page_payload(call, exhausted=False)
    page = validate_discovery(call, plan, valid)

    assert page.cards[0].external_id == "101"
    assert page.next_cursor == "https://2gis.kz/almaty/search?cursor=next-page"
    assert page.exhausted is False

    wrong_id = _page_payload(call)
    wrong_id["cards"][0]["url"] = "https://2gis.kz/almaty/firm/999"
    with pytest.raises(UnsafeBrowserResult, match="ID differs"):
        validate_discovery(call, plan, wrong_id)

    invalid_cursor = _page_payload(call, exhausted=False)
    invalid_cursor["next_cursor"] = "https://example.com/search?page=2"
    with pytest.raises(UnsafeBrowserResult, match=r"official 2gis\.kz"):
        validate_discovery(call, plan, invalid_cursor)

    with pytest.raises(ValidationError, match="not source exhaustion"):
        DiscoveryPage.model_validate({**_page_payload(call), "access_issue": "captcha"})

    cursor_and_exhaustion = _page_payload(call)
    cursor_and_exhaustion["next_cursor"] = "https://2gis.kz/almaty/search?cursor=next"
    with pytest.raises(ValidationError, match="exhausted"):
        DiscoveryPage.model_validate(cursor_and_exhaustion)

    at_card_cap = _page_payload(call, ids=tuple(str(index + 1) for index in range(20)))
    assert DiscoveryPage.model_validate(at_card_cap).exhausted is False


@pytest.mark.parametrize("card_count", [10, 12])
def test_discovery_exhaustion_needs_explicit_end_proof(card_count: int) -> None:
    plan = _plan()
    call = prepare_discovery(plan)
    ids = tuple(str(index + 1) for index in range(card_count))

    proven = _page_payload(call, ids=ids, exhausted=True, end_of_results_observed=True)
    assert validate_discovery(call, plan, proven).exhausted is True

    unproven = _page_payload(call, ids=ids, exhausted=True, end_of_results_observed=False)
    assert validate_discovery(call, plan, unproven).exhausted is False

    legacy = _page_payload(call, ids=ids, exhausted=True)
    del legacy["end_of_results_observed"]
    assert validate_discovery(call, plan, legacy).exhausted is False


def test_olx_listing_url_id_is_bound_to_discovery_card() -> None:
    plan = build_research_plan(
        ParsedProcurementRequest(
            city="Almaty",
            search_scope=SearchScope.CITYWIDE,
            items=[ProcurementItem(name="Brush 80 mm", quantity=10, unit="each")],
        ),
        include_olx=True,
    )
    branch = next(item for item in plan.source_plans if item.source == ResearchSource.OLX)
    call = prepare_discovery(branch)
    raw = {
        "spec_sha256": call.spec_sha256,
        "request_sha256": call.request_sha256,
        "cards": [
            {
                "external_id": "12345",
                "url": "https://www.olx.kz/d/obyavlenie/brush-ID12345.html",
                "name": "Brush listing",
            }
        ],
        "page_url": "https://www.olx.kz/oferty/brush",
        "raw_snapshot_sha256": "c" * 64,
        "pages_examined": 1,
        "next_cursor": "https://www.olx.kz/oferty/brush?page=2",
        "exhausted": False,
    }

    assert validate_discovery(call, branch, raw).cards[0].external_id == "12345"
    raw["cards"][0]["external_id"] = "ID12345"
    assert validate_discovery(call, branch, raw).cards[0].external_id == "12345"
    raw["cards"][0]["external_id"] = "12345"
    raw["cards"][0]["url"] = "https://www.olx.kz/d/obyavlenie/brush-ID54321.html"
    with pytest.raises(UnsafeBrowserResult, match="ID differs"):
        validate_discovery(call, branch, raw)


@pytest.mark.asyncio
async def test_invalid_primary_structured_page_uses_fallback() -> None:
    plan = _plan()
    primary = _MalformedPrimary()
    fallback = _Primary(("303",))
    provider = StagedResearchProvider(None, _Legacy(), primary, fallback)

    page = await provider._discover(prepare_discovery(plan), plan)

    assert primary.calls == 1
    assert fallback.calls == 1
    assert [card.external_id for card in page.cards] == ["303"]


@pytest.mark.asyncio
async def test_observed_captcha_page_uses_fallback() -> None:
    plan = _plan()

    class CaptchaPrimary:
        async def execute(self, call):
            return {**_page_payload(call, ids=(), exhausted=False), "access_issue": "captcha"}

    fallback = _Primary(("303",))
    provider = StagedResearchProvider(None, _Legacy(), CaptchaPrimary(), fallback)
    page = await provider._discover(prepare_discovery(plan), plan)
    assert fallback.calls == 1
    assert [card.external_id for card in page.cards] == ["303"]


@pytest.mark.asyncio
async def test_blocked_primary_without_fallback_fails_closed() -> None:
    plan = _plan()

    class BlockedPrimary:
        async def execute(self, call):
            return {**_page_payload(call, ids=(), exhausted=False), "access_issue": "mcp_error"}

    provider = StagedResearchProvider(None, _Legacy(), BlockedPrimary())
    with pytest.raises(UnsafeBrowserResult, match="could not access") as error:
        await provider._discover(prepare_discovery(plan), plan)
    assert error.value.access_issue == "mcp_error"


def test_observed_no_results_remains_valid() -> None:
    plan = _plan()
    call = prepare_discovery(plan)
    raw = {**_page_payload(call, ids=()), "access_issue": "no_results", "exhausted": False}
    assert validate_discovery(call, plan, raw).cards == []


@pytest.mark.asyncio
async def test_generation_fence_rejects_commit_after_pause() -> None:
    plan = _plan()
    connection = _Connection(plan.execution_query_id)
    primary = _Primary()
    legacy = _Legacy()
    provider = StagedResearchProvider(_Pool(connection), legacy, primary)
    original_search = legacy.search_suppliers

    async def pause_during_card_read(card_plan):
        result = await original_search(card_plan)
        connection.group_status = "paused"
        return result

    legacy.search_suppliers = pause_during_card_read
    with pytest.raises(RetryableProviderError, match="ownership changed"):
        await provider.search_suppliers(plan)

    assert len(connection.pages) == 1
    card = next(iter(connection.cards.values()))
    assert card["status"] == "pending"
    assert card["result_payload"] is None


@pytest.mark.asyncio
async def test_stale_generation_is_fenced_before_discovery_provider_call() -> None:
    plan = _plan()
    connection = _Connection(plan.execution_query_id)
    connection.group_status = "paused"
    primary = _Primary()
    legacy = _Legacy()
    provider = StagedResearchProvider(_Pool(connection), legacy, primary)

    with pytest.raises(RetryableProviderError, match="ownership changed"):
        await provider.search_suppliers(plan)

    assert primary.calls == 0
    assert connection.pages == {}
    assert legacy.calls == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_discovery_page_and_card_are_resumable() -> None:
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not set")

    schema = f"test_staged_research_{uuid4().hex}"
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
        user_id = uuid4()
        case_id = uuid4()
        item_id = uuid4()
        await pool.execute(
            "INSERT INTO users(id,telegram_id,display_name) VALUES($1,$2,$3)",
            user_id,
            800_000_000 + int(user_id.hex[:8], 16),
            "Staged test",
        )
        await pool.execute(
            "INSERT INTO procurement_cases(id,owner_user_id,title,city,status) "
            "VALUES($1,$2,'staged test','Almaty','researching')",
            case_id,
            user_id,
        )
        await pool.execute(
            "INSERT INTO request_items(id,case_id,line_number,name,quantity,unit,status) "
            "VALUES($1,$2,1,'Test brush 80 mm',10,'each','researching')",
            item_id,
            case_id,
        )
        research_plan = build_research_plan(
            ParsedProcurementRequest(
                city="Almaty",
                search_scope=SearchScope.CITYWIDE,
                items=[ProcurementItem(name="Test brush 80 mm", quantity=10, unit="each")],
            )
        )
        async with pool.acquire() as connection:
            persisted = await persist_research_plan(
                connection, case_id=case_id, plan=research_plan, requested_by=user_id
            )
            await mark_research_running(connection, persisted.id)
        query_id = next(
            query_id
            for query_id, branch in zip(
                persisted.query_ids, research_plan.source_plans, strict=True
            )
            if branch.source == ResearchSource.TWO_GIS
        )
        group_id = uuid4()
        lane_id = uuid4()
        job_id = uuid4()
        worker_id = "staged-test-worker"
        await pool.execute(
            "INSERT INTO research_item_groups(id,research_run_id,request_item_id,status) "
            "VALUES($1,$2,$3,'researching')",
            group_id,
            persisted.id,
            item_id,
        )
        await pool.execute(
            "INSERT INTO research_lanes("
            "id,group_id,lane_no,status,generation,owner_worker,lease_until) "
            "VALUES($1,$2,0,'researching',1,$3,now()+interval '5 minutes')",
            lane_id,
            group_id,
            worker_id,
        )
        await pool.execute(
            """INSERT INTO jobs(id,kind,payload,status,attempts,max_attempts,locked_at,locked_by,
               lease_expires_at,idempotency_key)
               VALUES($1,'parallel_research','{}'::jsonb,'running',1,5,now(),$2,
                      now()+interval '5 minutes','staged-test-job')""",
            job_id,
            worker_id,
        )
        await pool.execute(
            """INSERT INTO research_branch_checkpoints(
                research_run_id,group_id,lane_id,query_id,lane_no,status,owner_generation,
                owner_worker,lease_until,current_job_id
            ) VALUES($1,$2,$3,$4,0,'researching',1,$5,now()+interval '5 minutes',$6)""",
            persisted.id,
            group_id,
            lane_id,
            query_id,
            worker_id,
            job_id,
        )

        branch = next(
            item for item in research_plan.source_plans if item.source == ResearchSource.TWO_GIS
        )
        runtime_plan = branch.model_copy(
            update={
                "execution_query_id": query_id,
                "execution_generation": 1,
                "batch_candidate_limit": 2,
            }
        )
        primary = _Primary()
        legacy = _Legacy({"101": 1})
        provider = StagedResearchProvider(pool, legacy, primary)

        first = await provider.search_suppliers(runtime_plan)
        assert first.candidates == []
        assert first.progress_token is not None
        assert await pool.fetchval("SELECT count(*) FROM research_discovery_pages") == 1
        card = await pool.fetchrow("SELECT status,attempts FROM research_discovery_cards")
        assert (card["status"], card["attempts"]) == ("pending", 1)

        resumed = await provider.search_suppliers(runtime_plan)
        assert [candidate.external_id for candidate in resumed.candidates] == ["101"]
        assert resumed.exhausted is True
        assert resumed.request_sha256 == _request_hash(runtime_plan)
        assert resumed.progress_token != first.progress_token
        assert primary.calls == 1
        assert legacy.calls == ["101", "101"]
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
