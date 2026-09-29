from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

import procurement_bot.contact_enrichment as enrichment
from procurement_bot.mini_app import supplier_detail
from procurement_bot.providers.browser import SupplierBrowserResult
from procurement_bot.research import (
    AvailabilityStatus,
    ContactKind,
    EvidenceClaim,
    EvidenceType,
    ResearchSource,
    SupplierCandidate,
    SupplierContact,
    SupplierEvidence,
)
from procurement_bot.supplier_contact_scope import case_owned_supplier_contact_evidence


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _SupplierDetailConnection:
    def __init__(self) -> None:
        self.contact_sql = ""
        self.contact_args: tuple[object, ...] = ()
        self.location_sql = ""
        self.location_args: tuple[object, ...] = ()
        self.referral_sql = ""
        self.referral_args: tuple[object, ...] = ()

    async def fetchrow(self, _sql: str, *_args: object) -> dict[str, object]:
        return {"id": SUPPLIER_ID, "display_name": "СтройСнаб"}

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        if "FROM supplier_contacts sc" in sql:
            self.contact_sql, self.contact_args = sql, args
        elif "FROM supplier_locations l" in sql:
            self.location_sql, self.location_args = sql, args
        elif "FROM supplier_contact_referrals r" in sql:
            self.referral_sql, self.referral_args = sql, args
        return []


class _Acquire:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    async def __aenter__(self) -> object:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Request:
    def __init__(self, owner_id: UUID, connection: _SupplierDetailConnection) -> None:
        self.match_info = {"supplier_id": str(SUPPLIER_ID)}
        self.app = {"pool": SimpleNamespace(acquire=lambda: _Acquire(connection))}
        self.owner_id = owner_id

    def __getitem__(self, key: str) -> UUID:
        assert key == "owner_id"
        return self.owner_id


SUPPLIER_ID = uuid4()


def test_contact_scope_helper_uses_fixed_owner_parameter_and_rejects_dynamic_aliases() -> None:
    predicate = case_owned_supplier_contact_evidence(
        contact_alias="contact", evidence_alias="evidence", scope="owner",
    )
    assert "scope_case.owner_user_id=$2" in predicate
    assert "scope_case.id=$1" not in predicate
    assert "contact_evidence_ids" in predicate
    assert "supplier_contact_enriched" in predicate
    assert "phone_count' ~ '^[1-9][0-9]*$'" in predicate
    with pytest.raises(ValueError, match="static identifiers"):
        case_owned_supplier_contact_evidence(
            contact_alias="contact; DROP TABLE suppliers", evidence_alias="evidence",
        )


@pytest.mark.asyncio
async def test_supplier_detail_scopes_contacts_and_locations_to_owner_evidence() -> None:
    owner_id = uuid4()
    connection = _SupplierDetailConnection()

    await supplier_detail(_Request(owner_id, connection))  # type: ignore[arg-type]

    assert connection.contact_args == (SUPPLIER_ID, owner_id)
    assert "research_runs" in connection.contact_sql
    assert "offer_observations" in connection.contact_sql
    assert "supplier_contact_enrichment_result" in connection.contact_sql
    assert "contact_evidence_ids" in connection.contact_sql
    assert "outcome'='contact_found'" in connection.contact_sql
    assert connection.location_args == (SUPPLIER_ID, owner_id)
    assert "research_runs" in connection.location_sql
    assert "offer_observations" in connection.location_sql
    assert connection.referral_args == (SUPPLIER_ID, owner_id)
    assert "c.owner_user_id=$2" in connection.referral_sql


class _EnrichmentConnection:
    def __init__(self, case_id: UUID, supplier_id: UUID, firm_id: str) -> None:
        self.case_id = case_id
        self.supplier_id = supplier_id
        self.firm_id = firm_id
        self.event_data: dict[str, Any] | None = None

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, sql: str, *_args: object) -> dict[str, object] | None:
        if "FROM offers o" in sql:
            return {
                "display_name": "СтройСнаб",
                "firm_id": self.firm_id,
                "city": "Алматы",
                "source_url": f"https://2gis.kz/almaty/firm/{self.firm_id}",
                "request_item_id": uuid4(),
            }
        raise AssertionError(sql)

    async def fetchval(self, _sql: str, *_args: object) -> bool:
        return False

    async def execute(self, sql: str, *args: object) -> str:
        assert "INSERT INTO workflow_events" in sql
        self.event_data = args[3]
        return "INSERT 0 1"


class _EnrichmentPool:
    def __init__(self, connection: _EnrichmentConnection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Browser:
    def __init__(self, firm_id: str) -> None:
        self.firm_id = firm_id

    async def search_suppliers(self, _plan: object) -> SupplierBrowserResult:
        card_url = f"https://2gis.kz/almaty/firm/{self.firm_id}"
        evidence = SupplierEvidence(
            evidence_type=EvidenceType.BUSINESS_LISTING,
            source=ResearchSource.TWO_GIS,
            source_url=card_url,
            observed_at=datetime(2026, 9, 28, tzinfo=UTC),
            raw_snapshot_sha256="a" * 64,
            claims=[EvidenceClaim.CONTACT],
            excerpt="Телефон магазина: +7 705 123 45 67",
        )
        candidate = SupplierCandidate(
            source=ResearchSource.TWO_GIS,
            external_id=self.firm_id,
            name="СтройСнаб",
            contacts=[SupplierContact(
                kind=ContactKind.PHONE,
                value="+7 705 123 45 67",
                confidence=0.9,
            )],
            evidence=[evidence],
            product_availability=AvailabilityStatus.UNKNOWN,
        )
        return SupplierBrowserResult(
            spec_sha256="b" * 64,
            request_sha256="c" * 64,
            candidates=[candidate],
            exhausted=True,
            pages_examined=1,
        )


@pytest.mark.asyncio
async def test_2gis_enrichment_event_persists_exact_contact_evidence_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_id, supplier_id = uuid4(), uuid4()
    firm_id = "9429940001734585"
    contact_evidence_id = uuid4()
    connection = _EnrichmentConnection(case_id, supplier_id, firm_id)

    async def store_evidence(_connection: object, _value: object) -> object:
        return SimpleNamespace(id=contact_evidence_id)

    async def store_contact(_connection: object, _supplier_id: UUID, _contact: object) -> object:
        return SimpleNamespace(id=uuid4())

    monkeypatch.setattr(enrichment, "upsert_source_evidence", store_evidence)
    monkeypatch.setattr(enrichment, "upsert_supplier_contact", store_contact)

    found = await enrichment.enrich_2gis_contact(
        _EnrichmentPool(connection), _Browser(firm_id),
        case_id=case_id, supplier_id=supplier_id,
    )  # type: ignore[arg-type]

    assert found == 1
    assert connection.event_data is not None
    assert connection.event_data["contact_evidence_ids"] == [str(contact_evidence_id)]
