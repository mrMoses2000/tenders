"""Pure opt-in sourcing-city admission; no runtime wiring or I/O.

The caller owns a fresh discovery snapshot, item/city fences and atomic enqueue.
Reply work is separate durable dialogue state. Delivery location, case/item
identities and requirements are outside this policy and are never rewritten.
"""

from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite


class City(StrEnum):
    TARAZ = "Taraz"
    SHYMKENT = "Shymkent"
    ALMATY = "Almaty"


CITY_ORDER = (City.TARAZ, City.SHYMKENT, City.ALMATY)


@dataclass(frozen=True, slots=True)
class DiscoveryBudget:
    """Explicit successful-work caps; reaching either configured cap can end a tier."""

    requests: int | None = None
    pages: int | None = None


@dataclass(frozen=True, slots=True)
class CityTierPlan:
    """Explicit immutable selection of the fixed route, with optional per-city caps."""

    budgets: tuple[DiscoveryBudget | None, ...] = (None, None, None)


class TierStatus(StrEnum):
    UNSTARTED = "unstarted"
    RUNNING = "running"
    EXHAUSTED = "exhausted"
    BUDGET_COMPLETE = "budget_complete"
    SATISFIED = "satisfied"
    UNCERTAIN = "uncertain"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"
    PAUSED = "paused"
    FAILED = "failed"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    AUTH_REQUIRED = "auth_required"
    CAPTCHA = "captcha"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class CityProgress:
    city: City
    status: TierStatus = TierStatus.UNCERTAIN
    open_discovery_work: int | None = None
    pending_replies: int | None = None
    completion_confirmed: bool = False
    successful_requests: int = 0
    successful_pages: int = 0
    failed_attempts: int = 0
    provider_failure: AttemptOutcome | None = None


class AdmissionAction(StrEnum):
    ADMIT = "admit"
    WAIT = "wait"
    HOLD = "hold"
    PAUSE = "pause"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class CityAdmission:
    action: AdmissionAction
    city: City | None
    reason: str


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _valid_time(value: object) -> bool:
    return (type(value) is int and value >= 0) or (
        type(value) is float and isfinite(value) and value >= 0
    )


def _valid_plan(plan: object) -> bool:
    if not isinstance(plan, CityTierPlan) or not isinstance(plan.budgets, tuple):
        return False
    if len(plan.budgets) != len(CITY_ORDER):
        return False
    for budget in plan.budgets:
        if budget is None:
            continue
        if not isinstance(budget, DiscoveryBudget) or (budget.requests, budget.pages) == (
            None,
            None,
        ):
            return False
        for limit in (budget.requests, budget.pages):
            if limit is not None and (not _count(limit) or limit == 0):
                return False
    return True


def _budget_reached(row: CityProgress, budget: DiscoveryBudget | None) -> bool:
    return budget is not None and (
        (budget.requests is not None and row.successful_requests >= budget.requests)
        or (budget.pages is not None and row.successful_pages >= budget.pages)
    )


def record_discovery_attempt(
    row: CityProgress, outcome: AttemptOutcome, *, completed_pages: int = 0
) -> CityProgress:
    """Account for a fully processed success or pause without consuming budget.

    Only the validating adapter may report SUCCEEDED. Partial/denied/failed
    attempts consume no request/page budget, even if they saw some pages.
    Clearing a provider hold requires an explicit reconciled resume snapshot.
    """
    if not isinstance(outcome, AttemptOutcome) or not _count(completed_pages):
        raise ValueError("invalid discovery attempt")
    if row.status is not TierStatus.RUNNING or row.provider_failure is not None:
        raise ValueError("discovery must be explicitly resumed before an attempt")
    if not all(
        _count(value)
        for value in (row.successful_requests, row.successful_pages, row.failed_attempts)
    ):
        raise ValueError("invalid discovery counters")
    if outcome is AttemptOutcome.SUCCEEDED:
        return replace(
            row,
            successful_requests=row.successful_requests + 1,
            successful_pages=row.successful_pages + completed_pages,
        )
    return replace(
        row,
        status=TierStatus.PAUSED,
        provider_failure=outcome,
        failed_attempts=row.failed_attempts + 1,
    )


def decide_city_tier(
    progress: tuple[CityProgress, ...],
    *,
    plan: CityTierPlan | None = None,
    now: float | None = None,
    deadline: float | None = None,
    held: bool = True,
    snapshot_complete: bool = False,
    active_discovery_items: int | None = None,
    native_concurrency_budget: int | None = None,
    item_active_cities: int | None = None,
) -> CityAdmission:
    """Admit one sourcing city before the execution deadline, or stop safely.

    now/deadline use the caller's same clock domain, normally monotonic seconds.
    The optional execution deadline is independent of report checkpoints.
    At/after an execution deadline, pause and retain state; time never proves exhaustion.
    A terminal tier requires confirmed discovery completion and zero discovery
    work. Delayed replies (even an unknown count) never prevent that transition.
    The caller fences one city per item and shared native discovery capacity;
    this pure selector neither dispatches jobs nor authenticates providers.
    """

    def stop(action: AdmissionAction, reason: str) -> CityAdmission:
        return CityAdmission(action, None, reason)

    if held is not False:
        return stop(AdmissionAction.HOLD, "existing_or_unknown_hold")
    if snapshot_complete is not True:
        return stop(AdmissionAction.HOLD, "incomplete_discovery_snapshot")
    if not _valid_plan(plan):
        return stop(AdmissionAction.HOLD, "explicit_valid_plan_required")
    if now is not None or deadline is not None:
        if not all(_valid_time(value) for value in (now, deadline)):
            return stop(AdmissionAction.HOLD, "explicit_valid_deadline_required")
        if now >= deadline:
            return stop(AdmissionAction.PAUSE, "execution_deadline")
    if (
        not _count(active_discovery_items)
        or not _count(native_concurrency_budget)
        or native_concurrency_budget < 1
        or active_discovery_items > native_concurrency_budget
    ):
        return stop(AdmissionAction.HOLD, "invalid_native_capacity")
    if (
        not _count(item_active_cities)
        or item_active_cities > 1
        or item_active_cities > active_discovery_items
    ):
        return stop(AdmissionAction.HOLD, "unknown_or_overlapping_item_cities")
    if not isinstance(progress, tuple) or len(progress) != len(CITY_ORDER):
        return stop(AdmissionAction.HOLD, "invalid_city_order")

    terminal = (TierStatus.EXHAUSTED, TierStatus.BUDGET_COMPLETE, TierStatus.SATISFIED)
    for index, (row, city) in enumerate(zip(progress, CITY_ORDER, strict=True)):
        if not isinstance(row, CityProgress) or row.city is not city:
            return stop(AdmissionAction.HOLD, "invalid_city_order")
        if not isinstance(row.status, TierStatus):
            return stop(AdmissionAction.HOLD, "unknown_status")
        if type(row.completion_confirmed) is not bool:
            return stop(AdmissionAction.HOLD, "invalid_confirmation")
        if not all(
            _count(value)
            for value in (
                row.open_discovery_work,
                row.successful_requests,
                row.successful_pages,
                row.failed_attempts,
            )
        ):
            return stop(AdmissionAction.HOLD, "unknown_or_invalid_discovery_work")
        if row.pending_replies is not None and not _count(row.pending_replies):
            return stop(AdmissionAction.HOLD, "invalid_reply_count")
        if row.provider_failure is not None:
            if not isinstance(row.provider_failure, AttemptOutcome):
                return stop(AdmissionAction.HOLD, "unknown_provider_state")
            if row.provider_failure is AttemptOutcome.SUCCEEDED:
                return stop(AdmissionAction.HOLD, "invalid_provider_failure")
            return stop(AdmissionAction.PAUSE, "provider_wait")
        if row.status in terminal:
            if not row.completion_confirmed or row.open_discovery_work != 0:
                return stop(AdmissionAction.HOLD, "unconfirmed_discovery_terminal")
            if row.status is TierStatus.BUDGET_COMPLETE and not _budget_reached(
                row, plan.budgets[index]
            ):
                return stop(AdmissionAction.HOLD, "successful_explicit_budget_not_complete")
        elif row.completion_confirmed:
            return stop(AdmissionAction.HOLD, "contradictory_confirmation")
        if row.status is TierStatus.UNSTARTED and (
            row.open_discovery_work or row.successful_requests or row.successful_pages
        ):
            return stop(AdmissionAction.HOLD, "unstarted_has_discovery_work")

    if item_active_cities and not any(row.status is TierStatus.RUNNING for row in progress):
        return stop(AdmissionAction.HOLD, "discovery_item_slot_not_reconciled")
    for index, row in enumerate(progress):
        if row.status in (TierStatus.EXHAUSTED, TierStatus.BUDGET_COMPLETE):
            continue
        if any(later.status is not TierStatus.UNSTARTED for later in progress[index + 1 :]):
            return stop(AdmissionAction.HOLD, "out_of_order_discovery")
        if row.status is TierStatus.UNSTARTED:
            if active_discovery_items >= native_concurrency_budget:
                return stop(AdmissionAction.WAIT, "native_capacity_reserved")
            return CityAdmission(AdmissionAction.ADMIT, row.city, "next_city")
        if row.status is TierStatus.RUNNING:
            return stop(AdmissionAction.WAIT, "discovery_still_running")
        if row.status is TierStatus.SATISFIED:
            return stop(AdmissionAction.COMPLETE, "requirements_satisfied")
        if row.status in (TierStatus.PAUSED, TierStatus.BLOCKED, TierStatus.FAILED):
            return stop(AdmissionAction.PAUSE, "discovery_paused")
        return stop(AdmissionAction.HOLD, "discovery_not_terminal")
    return stop(AdmissionAction.COMPLETE, "route_discovery_complete")
