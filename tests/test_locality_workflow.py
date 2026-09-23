from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from procurement_bot.intake import (
    ParsedProcurementRequest,
    ProcurementItem,
    SearchScope,
)
from procurement_bot.locality_workflow import (
    InvalidLocalityBrowserResult,
    LocalityClarificationNotice,
    LocalityResolutionContext,
    LocalityWorkflowDenied,
    PersistedLocalityOutcome,
    resolve_case_locality,
)
from procurement_bot.providers.browser import LocalityBrowserResult
from procurement_bot.research import (
    LocalityCandidate,
    LocalityResolution,
    LocalityResolutionStatus,
    ResearchSource,
)


def _candidate(name: str, confidence: float, place_id: str) -> LocalityCandidate:
    return LocalityCandidate(
        canonical_name=name,
        place_type="район",
        city="Алматы",
        source=ResearchSource.TWO_GIS,
        provider_place_id=place_id,
        source_url=f"https://2gis.kz/almaty/geo/{place_id}",
        raw_snapshot_sha256="a" * 64,
        confidence=confidence,
    )


def _request(
    *, scope: SearchScope = SearchScope.SPECIFIC_AREA
) -> ParsedProcurementRequest:
    return ParsedProcurementRequest(
        city="Алматы",
        search_scope=scope,
        search_area_text="на первой Алмате",
        items=[ProcurementItem(name="Марля")],
    )


@dataclass
class _Browser:
    candidates: list[LocalityCandidate]
    calls: list[Any]
    invalid_result: bool = False

    async def resolve_locality(self, task: Any) -> Any:
        self.calls.append(task)
        if self.invalid_result:
            return {"candidates": []}
        return LocalityBrowserResult(
            spec_sha256="b" * 64,
            request_sha256="c" * 64,
            candidates=self.candidates,
        )

    async def search_suppliers(self, plan: Any) -> Any:  # pragma: no cover
        raise AssertionError(f"supplier search is forbidden: {plan}")


class _Repository:
    def __init__(self, context: LocalityResolutionContext) -> None:
        self.context = context
        self.load_calls: list[tuple[UUID, int, UUID]] = []
        self.commits: list[
            tuple[LocalityResolution, LocalityClarificationNotice | None]
        ] = []

    async def load_context(
        self, *, case_id: UUID, intake_version: int, owner_user_id: UUID
    ) -> LocalityResolutionContext:
        self.load_calls.append((case_id, intake_version, owner_user_id))
        return self.context

    async def commit_resolution(
        self,
        *,
        context: LocalityResolutionContext,
        resolution: LocalityResolution,
        clarification: LocalityClarificationNotice | None,
    ) -> PersistedLocalityOutcome:
        assert context is self.context
        self.commits.append((resolution, clarification))
        followup = (
            "prepare_research"
            if resolution.status == LocalityResolutionStatus.RESOLVED
            else "telegram_clarification"
        )
        return PersistedLocalityOutcome(
            resolution_id=uuid4(),
            resolution_version=1,
            status=resolution.status,
            newly_persisted=True,
            followup_kind=followup,
        )


def _context(
    *,
    request: ParsedProcurementRequest | None = None,
    case_status: str = "needs_clarification",
    completed: PersistedLocalityOutcome | None = None,
) -> LocalityResolutionContext:
    return LocalityResolutionContext(
        case_id=uuid4(),
        owner_user_id=uuid4(),
        telegram_chat_id=12345,
        intake_version=4,
        case_status=case_status,
        request=request or _request(),
        context_token=f"context-{uuid4()}",
        completed=completed,
    )


@pytest.mark.asyncio
async def test_high_confidence_result_is_persisted_and_schedules_prepare() -> None:
    context = _context()
    repository = _Repository(context)
    browser = _Browser([_candidate("Алматы-1", 0.97, "station")], [])

    outcome = await resolve_case_locality(
        repository,
        browser,  # type: ignore[arg-type]
        case_id=context.case_id,
        intake_version=context.intake_version,
        owner_user_id=context.owner_user_id,
    )

    assert outcome.status == LocalityResolutionStatus.RESOLVED
    assert outcome.followup_kind == "prepare_research"
    assert len(browser.calls) == 1
    assert browser.calls[0].city == "Алматы"
    assert browser.calls[0].phrase == "на первой Алмате"
    resolution, clarification = repository.commits[0]
    assert resolution.selected is not None
    assert resolution.selected.provider_place_id == "station"
    assert clarification is None


@pytest.mark.asyncio
async def test_ambiguous_result_produces_two_or_three_text_options() -> None:
    context = _context()
    repository = _Repository(context)
    browser = _Browser(
        [
            _candidate("Вокзал Алматы-1", 0.88, "station"),
            _candidate("Район Алматы-1", 0.82, "district"),
            _candidate("Рынок рядом с Алматы-1", 0.79, "market"),
            _candidate("Лишний вариант", 0.75, "extra"),
        ],
        [],
    )

    outcome = await resolve_case_locality(
        repository,
        browser,  # type: ignore[arg-type]
        case_id=context.case_id,
        intake_version=4,
        owner_user_id=context.owner_user_id,
    )

    assert outcome.status == LocalityResolutionStatus.NEEDS_CLARIFICATION
    _, notice = repository.commits[0]
    assert notice is not None
    assert notice.chat_id == context.telegram_chat_id
    assert len(notice.options) == 3
    assert "1. Вокзал Алматы-1" in notice.text
    assert "4." not in notice.text


@pytest.mark.asyncio
async def test_no_candidates_asks_for_geolocation_or_landmark() -> None:
    context = _context()
    repository = _Repository(context)

    outcome = await resolve_case_locality(
        repository,
        _Browser([], []),  # type: ignore[arg-type]
        case_id=context.case_id,
        intake_version=4,
        owner_user_id=context.owner_user_id,
    )

    assert outcome.status == LocalityResolutionStatus.UNRESOLVED
    _, notice = repository.commits[0]
    assert notice is not None
    assert notice.options == (
        "Отправлю геолокацию",
        "Укажу ближайший ориентир",
    )


@pytest.mark.asyncio
async def test_completed_context_is_idempotent_without_browser_call() -> None:
    completed = PersistedLocalityOutcome(
        resolution_id=uuid4(),
        resolution_version=2,
        status=LocalityResolutionStatus.RESOLVED,
        newly_persisted=False,
        followup_kind="prepare_research",
    )
    context = _context(case_status="ready", completed=completed)
    repository = _Repository(context)
    browser = _Browser([], [])

    result = await resolve_case_locality(
        repository,
        browser,  # type: ignore[arg-type]
        case_id=context.case_id,
        intake_version=4,
        owner_user_id=context.owner_user_id,
    )

    assert result is completed
    assert browser.calls == []
    assert repository.commits == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda value: replace(value, case_id=uuid4()), "different case"),
        (lambda value: replace(value, owner_user_id=uuid4()), "different owner"),
        (lambda value: replace(value, intake_version=5), "version changed"),
        (lambda value: replace(value, case_status="ready"), "not awaiting"),
        (
            lambda value: replace(value, request=_request(scope=SearchScope.CITYWIDE)),
            "does not require",
        ),
    ],
)
async def test_stale_or_unowned_context_fails_before_browser(
    mutation: Any,
    error: str,
) -> None:
    original = _context()
    context = mutation(original)
    repository = _Repository(context)
    browser = _Browser([], [])

    with pytest.raises(LocalityWorkflowDenied, match=error):
        await resolve_case_locality(
            repository,
            browser,  # type: ignore[arg-type]
            case_id=original.case_id,
            intake_version=4,
            owner_user_id=original.owner_user_id,
        )
    assert browser.calls == []


@pytest.mark.asyncio
async def test_invalid_browser_port_result_fails_closed() -> None:
    context = _context()
    repository = _Repository(context)
    browser = _Browser([], [], invalid_result=True)

    with pytest.raises(InvalidLocalityBrowserResult, match="validated"):
        await resolve_case_locality(
            repository,
            browser,  # type: ignore[arg-type]
            case_id=context.case_id,
            intake_version=4,
            owner_user_id=context.owner_user_id,
        )
    assert repository.commits == []
