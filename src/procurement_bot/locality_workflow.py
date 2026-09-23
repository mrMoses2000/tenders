from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from procurement_bot.intake import ParsedProcurementRequest, SearchScope
from procurement_bot.providers.browser import BrowserResearchPort, LocalityBrowserResult
from procurement_bot.research import (
    LocalityResearchTask,
    LocalityResolution,
    LocalityResolutionStatus,
    decide_locality_resolution,
)


class LocalityWorkflowDenied(RuntimeError):
    """The requested case/version/owner tuple is no longer safe to process."""


class InvalidLocalityBrowserResult(RuntimeError):
    """The configured browser port violated its structured result contract."""


@dataclass(frozen=True, slots=True)
class PersistedLocalityOutcome:
    """Durable result returned by the repository's atomic commit.

    For a resolved locality, the repository must enqueue exactly one
    ``prepare_research`` job whose payload contains only ``case_id``,
    ``intake_version``, ``locality_resolution_id`` and
    ``locality_resolution_version``. For any other status it must keep the case
    in ``needs_clarification`` and enqueue the supplied Telegram notice instead.
    Both operations use repository-owned idempotency keys.
    """

    resolution_id: UUID
    resolution_version: int
    status: LocalityResolutionStatus
    newly_persisted: bool
    followup_kind: Literal["prepare_research", "telegram_clarification"]

    def __post_init__(self) -> None:
        if self.resolution_version < 1:
            raise ValueError("resolution_version must be positive")
        expected = (
            "prepare_research"
            if self.status == LocalityResolutionStatus.RESOLVED
            else "telegram_clarification"
        )
        if self.followup_kind != expected:
            raise ValueError("followup kind does not match locality status")


@dataclass(frozen=True, slots=True)
class LocalityResolutionContext:
    """Immutable, ownership-checked snapshot loaded before a browser call."""

    case_id: UUID
    owner_user_id: UUID
    telegram_chat_id: int
    intake_version: int
    case_status: str
    request: ParsedProcurementRequest
    context_token: str
    completed: PersistedLocalityOutcome | None = None

    def __post_init__(self) -> None:
        if self.intake_version < 1:
            raise ValueError("intake_version must be positive")
        if not self.context_token:
            raise ValueError("context_token must be non-empty")


@dataclass(frozen=True, slots=True)
class LocalityClarificationNotice:
    """A safe Telegram question; options are display text, never callbacks."""

    chat_id: int
    question: str
    options: tuple[str, ...]
    text: str

    def __post_init__(self) -> None:
        if not self.question.strip() or not self.text.strip():
            raise ValueError("clarification text must be non-empty")
        if not 2 <= len(self.options) <= 3:
            raise ValueError("locality clarification requires two or three options")
        if any(not option.strip() for option in self.options):
            raise ValueError("locality clarification options must be non-empty")


class LocalityResolutionRepository(Protocol):
    """Persistence boundary for one locality-resolution attempt.

    ``load_context`` must verify that the applied parse version belongs to the
    supplied case and owner. ``commit_resolution`` must re-check
    ``context_token`` under a database lock, persist the immutable result and
    enqueue the appropriate follow-up atomically. A changed case, owner, parse
    version, city, phrase or status must fail closed.
    """

    async def load_context(
        self,
        *,
        case_id: UUID,
        intake_version: int,
        owner_user_id: UUID,
    ) -> LocalityResolutionContext: ...

    async def commit_resolution(
        self,
        *,
        context: LocalityResolutionContext,
        resolution: LocalityResolution,
        clarification: LocalityClarificationNotice | None,
    ) -> PersistedLocalityOutcome: ...


def _validate_context(
    context: LocalityResolutionContext,
    *,
    case_id: UUID,
    intake_version: int,
    owner_user_id: UUID,
) -> None:
    if context.case_id != case_id:
        raise LocalityWorkflowDenied("repository returned a different case")
    if context.owner_user_id != owner_user_id:
        raise LocalityWorkflowDenied("case belongs to a different owner")
    if context.intake_version != intake_version:
        raise LocalityWorkflowDenied("applied intake version changed")
    if context.completed is None and context.case_status != "needs_clarification":
        raise LocalityWorkflowDenied("case is not awaiting locality clarification")
    request = context.request
    if request.search_scope != SearchScope.SPECIFIC_AREA:
        raise LocalityWorkflowDenied("request does not require a specific-area lookup")
    if request.city is None or request.search_area_text is None:
        raise LocalityWorkflowDenied("specific-area request has no city or area phrase")


def _clarification_notice(
    *,
    chat_id: int,
    resolution: LocalityResolution,
) -> LocalityClarificationNotice:
    question = resolution.clarification_question
    if question is None:
        raise ValueError("non-resolved locality has no clarification question")
    candidate_options = tuple(
        f"{candidate.canonical_name} ({candidate.place_type})"
        for candidate in resolution.candidates[:3]
    )
    if len(candidate_options) >= 2:
        options = candidate_options
    elif len(candidate_options) == 1:
        options = (
            candidate_options[0],
            "Другой ориентир — напишу его сообщением",
            "Отправлю геолокацию",
        )
    else:
        options = (
            "Отправлю геолокацию",
            "Укажу ближайший ориентир",
        )
    rendered_options = "\n".join(
        f"{index}. {option}" for index, option in enumerate(options, start=1)
    )
    return LocalityClarificationNotice(
        chat_id=chat_id,
        question=question,
        options=options,
        text=f"{question}\n\n{rendered_options}",
    )


async def resolve_case_locality(
    repository: LocalityResolutionRepository,
    browser: BrowserResearchPort,
    *,
    case_id: UUID,
    intake_version: int,
    owner_user_id: UUID,
) -> PersistedLocalityOutcome:
    """Resolve one colloquial area without allowing browser-owned decisions.

    The repository performs ownership/staleness checks both before and after
    the external browser call. The browser may supply source-backed candidates,
    but only ``decide_locality_resolution`` can select one.
    """

    if not isinstance(case_id, UUID) or not isinstance(owner_user_id, UUID):
        raise TypeError("case_id and owner_user_id must be UUIDs")
    if intake_version < 1:
        raise ValueError("intake_version must be positive")
    context = await repository.load_context(
        case_id=case_id,
        intake_version=intake_version,
        owner_user_id=owner_user_id,
    )
    _validate_context(
        context,
        case_id=case_id,
        intake_version=intake_version,
        owner_user_id=owner_user_id,
    )
    if context.completed is not None:
        return context.completed

    request = context.request
    assert request.city is not None
    assert request.search_area_text is not None
    task = LocalityResearchTask(city=request.city, phrase=request.search_area_text)
    browser_result = await browser.resolve_locality(task)
    if not isinstance(browser_result, LocalityBrowserResult):
        raise InvalidLocalityBrowserResult(
            "browser port did not return a validated LocalityBrowserResult"
        )
    resolution = decide_locality_resolution(
        phrase=task.phrase,
        city=task.city,
        candidates=browser_result.candidates,
    )
    clarification = (
        None
        if resolution.status == LocalityResolutionStatus.RESOLVED
        else _clarification_notice(
            chat_id=context.telegram_chat_id,
            resolution=resolution,
        )
    )
    return await repository.commit_resolution(
        context=context,
        resolution=resolution,
        clarification=clarification,
    )
