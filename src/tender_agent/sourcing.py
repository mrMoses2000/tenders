from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class CandidateMaturity(StrEnum):
    LEAD = "lead"
    PRODUCT_OBSERVATION = "product_observation"
    SUPPLIER_QUOTE = "supplier_quote"
    PURCHASE_READY = "purchase_ready"


@dataclass(frozen=True)
class SourcingCandidate:
    candidate_id: str
    line_id: str
    supplier_id: str
    maturity: CandidateMaturity
    checked_at: datetime
    source_url: str = ""
    city: str = ""
    is_local: bool = False
    technical_match_proven: bool = False
    price_confirmed: bool = False
    stock_confirmed: bool = False
    contact_available: bool = False
    is_reserve: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "maturity", CandidateMaturity(self.maturity))
        if not self.candidate_id or not self.line_id or not self.supplier_id:
            raise ValueError("candidate_id, line_id and supplier_id are required")
        if self.checked_at.tzinfo is None:
            raise ValueError("checked_at must be timezone-aware")


@dataclass(frozen=True)
class FreshnessPolicy:
    lead_ttl: timedelta = timedelta(days=30)
    observation_ttl: timedelta = timedelta(days=7)
    quote_ttl: timedelta = timedelta(days=2)
    purchase_ready_ttl: timedelta = timedelta(hours=24)

    def ttl(self, maturity: CandidateMaturity) -> timedelta:
        return {
            CandidateMaturity.LEAD: self.lead_ttl,
            CandidateMaturity.PRODUCT_OBSERVATION: self.observation_ttl,
            CandidateMaturity.SUPPLIER_QUOTE: self.quote_ttl,
            CandidateMaturity.PURCHASE_READY: self.purchase_ready_ttl,
        }[maturity]


@dataclass(frozen=True)
class CoveragePolicy:
    minimum_leads: int = 5
    minimum_actionable: int = 2
    require_local: bool = True
    require_reserve: bool = True
    require_purchase_ready: bool = False


@dataclass(frozen=True)
class CoverageResult:
    sufficient: bool
    fresh_candidates: int
    fresh_actionable: int
    purchase_ready: int
    local_present: bool
    reserve_present: bool
    gaps: tuple[str, ...]
    stop_reason: str


_DEFAULT_EVALUATE_COVERAGE_POLICY = CoveragePolicy()
_DEFAULT_EVALUATE_FRESHNESS_POLICY = FreshnessPolicy()
_DEFAULT_DECIDE_COVERAGE_POLICY = CoveragePolicy()
_DEFAULT_DECIDE_FRESHNESS_POLICY = FreshnessPolicy()


def evaluate_coverage(
    candidates: Iterable[SourcingCandidate],
    policy: CoveragePolicy = _DEFAULT_EVALUATE_COVERAGE_POLICY,
    freshness: FreshnessPolicy = _DEFAULT_EVALUATE_FRESHNESS_POLICY,
    now: datetime | None = None,
) -> CoverageResult:
    checked_now = now or datetime.now(UTC)
    fresh = tuple(
        candidate for candidate in candidates
        if checked_now - candidate.checked_at <= freshness.ttl(candidate.maturity)
    )
    actionable = tuple(
        candidate for candidate in fresh
        if candidate.maturity in {CandidateMaturity.SUPPLIER_QUOTE, CandidateMaturity.PURCHASE_READY}
        and candidate.technical_match_proven
        and candidate.price_confirmed
        and candidate.contact_available
    )
    ready = tuple(
        candidate for candidate in actionable
        if candidate.maturity == CandidateMaturity.PURCHASE_READY and candidate.stock_confirmed
    )
    gaps: list[str] = []
    if len(fresh) < policy.minimum_leads:
        gaps.append(f"нужно ещё {policy.minimum_leads - len(fresh)} свежих кандидатов")
    if len(actionable) < policy.minimum_actionable:
        gaps.append(f"нужно ещё {policy.minimum_actionable - len(actionable)} подтверждённых котировок")
    local_present = any(candidate.is_local for candidate in fresh)
    reserve_present = any(candidate.is_reserve for candidate in fresh)
    if policy.require_local and not local_present:
        gaps.append("нет локального варианта")
    if policy.require_reserve and not reserve_present:
        gaps.append("нет резервного варианта")
    if policy.require_purchase_ready and not ready:
        gaps.append("нет purchase-ready варианта")
    return CoverageResult(
        sufficient=not gaps, fresh_candidates=len(fresh), fresh_actionable=len(actionable),
        purchase_ready=len(ready), local_present=local_present, reserve_present=reserve_present,
        gaps=tuple(gaps), stop_reason="quality_coverage_met" if not gaps else "continue_search",
    )


@dataclass(frozen=True)
class SearchRouteStep:
    route: str
    purpose: str


DEFAULT_SEARCH_ROUTE = (
    SearchRouteStep("history", "Проверить прошлые закупки и фактические цены"),
    SearchRouteStep("local_catalogs", "Найти локальный запас и контакты"),
    SearchRouteStep("product_pages", "Подтвердить техспек и цену на карточке"),
    SearchRouteStep("marketplaces", "Расширить ценовую выборку"),
    SearchRouteStep("nearby_cities", "Сравнить landed cost с межгородом"),
    SearchRouteStep("offline_markets", "Сформировать проверяемую гипотезу офлайн-поиска"),
    SearchRouteStep("direct_contact", "Получить котировку, остаток и условия"),
)


def remaining_search_routes(attempted_routes: Iterable[str]) -> tuple[SearchRouteStep, ...]:
    attempted = set(attempted_routes)
    return tuple(step for step in DEFAULT_SEARCH_ROUTE if step.route not in attempted)


@dataclass(frozen=True)
class SearchDecision:
    coverage: CoverageResult
    next_step: SearchRouteStep | None
    status: str


def decide_next_search(
    candidates: Iterable[SourcingCandidate],
    attempted_routes: Iterable[str],
    policy: CoveragePolicy = _DEFAULT_DECIDE_COVERAGE_POLICY,
    freshness: FreshnessPolicy = _DEFAULT_DECIDE_FRESHNESS_POLICY,
    now: datetime | None = None,
) -> SearchDecision:
    coverage = evaluate_coverage(candidates, policy, freshness, now)
    if coverage.sufficient:
        return SearchDecision(coverage, None, "quality_coverage_met")
    remaining = remaining_search_routes(attempted_routes)
    if not remaining:
        return SearchDecision(coverage, None, "routes_exhausted_with_gaps")
    return SearchDecision(coverage, remaining[0], "continue_search")
