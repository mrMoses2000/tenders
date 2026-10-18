from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.providers.browser import (
    BrowserProviderError,
)
from procurement_bot.providers.staged_research import StagedResearchProvider
from procurement_bot.research import (
    EvidenceClaim,
    EvidenceType,
    ResearchSource,
    SupplierCandidate,
    SupplierEvidence,
    build_research_plan,
)


class _QuotaError(BrowserProviderError):
    diagnostic_code = "cli_usage_limit"


class _Primary:
    async def execute(self, _call):
        raise _QuotaError("provider limit")


class _Fallback:
    def __init__(self):
        self.calls = 0

    async def execute(self, _call):
        self.calls += 1
        raise AssertionError("quota errors must not be retried through fallback")


class _Connection:
    async def fetchval(self, sql, *_args):
        if "SELECT research_run_id FROM research_queries" in sql:
            return uuid4()
        if "FROM case_pipeline_runtime" in sql:
            return True
        raise AssertionError(sql)


class _Pool:
    def __init__(self):
        self.connection = _Connection()

    def acquire(self):
        @asynccontextmanager
        async def manager():
            yield self.connection

        return manager()


def _plan():
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
    return branch.model_copy(update={"execution_query_id": uuid4(), "execution_generation": 7})


def _card_facts():
    return SupplierCandidate(
        source=ResearchSource.TWO_GIS,
        external_id="101",
        name="Store 101",
        address="Almaty",
        evidence=[
            SupplierEvidence(
                evidence_type=EvidenceType.BUSINESS_LISTING,
                source=ResearchSource.TWO_GIS,
                source_url="https://2gis.kz/almaty/firm/101",
                observed_at=datetime.now(UTC),
                raw_snapshot_sha256="a" * 64,
                claims=[EvidenceClaim.BUSINESS_IDENTITY, EvidenceClaim.ADDRESS],
            )
        ],
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_quota_during_contact_card_stage_propagates_without_attempt_or_fallback(
    monkeypatch,
):
    from procurement_bot import pipeline_runtime

    monkeypatch.setattr(pipeline_runtime, "autonomy_enabled", _enabled)
    plan = _plan()
    fallback = _Fallback()
    provider = StagedResearchProvider(
        _Pool(),
        legacy=object(),
        primary=_Primary(),
        fallback=fallback,
        separate_contacts=True,
    )

    async def preflight_noop(_plan):
        return None

    monkeypatch.setattr(provider, "_preflight_fence", preflight_noop)
    card = {
        "external_id": "101",
        "ordinal": 0,
        "card_facts_payload": _card_facts(),
        "contact_attempts": 0,
    }

    with pytest.raises(_QuotaError, match="provider limit") as caught:
        await provider._read_staged_card(
            plan,
            plan.model_copy(update={"rendered_query": "https://2gis.kz/almaty/firm/101"}),
            uuid4(),
            card,
        )

    assert caught.value.diagnostic_code == "cli_usage_limit"
    assert card["contact_attempts"] == 0
    assert fallback.calls == 0


async def _enabled(_connection, _run_id):
    return True
