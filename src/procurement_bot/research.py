from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import AwareDatetime, Field, field_validator, model_validator

from procurement_bot.intake import (
    CleanText,
    ParsedProcurementRequest,
    SearchScope,
    StrictModel,
)


class ResearchSource(StrEnum):
    OWN_DATABASE = "own_database"
    TWO_GIS = "2gis"
    WEB = "web"


class ResearchPurpose(StrEnum):
    KNOWN_SUPPLIER_LOOKUP = "known_supplier_lookup"
    BUSINESS_DISCOVERY = "business_discovery"
    PRODUCT_EVIDENCE = "product_evidence"


class LocalityResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNRESOLVED = "unresolved"


class EvidenceType(StrEnum):
    BUSINESS_LISTING = "business_listing"
    PRODUCT_PAGE = "product_page"
    SUPPLIER_STATEMENT = "supplier_statement"


class EvidenceClaim(StrEnum):
    BUSINESS_IDENTITY = "business_identity"
    ADDRESS = "address"
    CONTACT = "contact"
    CATEGORY = "category"
    OPENING_HOURS = "opening_hours"
    REPUTATION = "reputation"
    PRODUCT_MATCH = "product_match"
    ADVERTISED_PRICE = "advertised_price"
    AVAILABILITY = "availability"


class AvailabilityStatus(StrEnum):
    UNKNOWN = "unknown"
    PUBLICLY_ADVERTISED = "publicly_advertised"
    CONFIRMED_IN_STOCK = "confirmed_in_stock"
    CONFIRMED_OUT_OF_STOCK = "confirmed_out_of_stock"


class ContactKind(StrEnum):
    PHONE = "phone"
    WHATSAPP = "whatsapp"
    WEBSITE = "website"
    INSTAGRAM = "instagram"


class PrivatePriceLeakError(ValueError):
    """A private budget/tender-price marker reached a research-facing payload."""


_PRIVATE_PRICE_PATTERN = re.compile(
    r"(?:\bбюджет\w*\b|\bтендерн\w*\s+цен\w*\b|"
    r"\bзакупочн\w*\s+цен\w*\b|\bмаксимальн\w*\s+цен\w*\b|"
    r"\bпредельн\w*\s+цен\w*\b|\btarget\s+price\b|\btender\s+price\b|"
    r"\bprivate\s+price\b|\bbudget\b)",
    flags=re.IGNORECASE,
)

_PRIVATE_PRICE_KEYS = {
    "budget",
    "budget_amount",
    "private_price",
    "private_tender_unit_price",
    "target_price",
    "tender_price",
}


def assert_no_private_price(value: Any, *, path: str = "payload") -> None:
    """Fail closed if a supplier-search payload contains a private price marker.

    Public prices observed during research are represented by ``price_amount`` on
    evidence. This guard applies to outbound search tasks, never to captured evidence.
    """

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key).strip().casefold()
            if normalized_key in _PRIVATE_PRICE_KEYS:
                raise PrivatePriceLeakError(f"private price field at {path}.{key}")
            assert_no_private_price(nested, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple, set)):
        for index, nested in enumerate(value):
            assert_no_private_price(nested, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and _PRIVATE_PRICE_PATTERN.search(value):
        raise PrivatePriceLeakError(f"private price marker at {path}")


def _validate_source_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("source URL must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("source URL must not contain credentials")
    return value


class GeoPoint(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LocalityCandidate(StrictModel):
    """A source-backed interpretation of a colloquial locality phrase."""

    canonical_name: CleanText = Field(max_length=300)
    place_type: CleanText = Field(max_length=100)
    city: CleanText = Field(max_length=160)
    source: ResearchSource
    provider_place_id: CleanText | None = Field(default=None, max_length=300)
    address: CleanText | None = Field(default=None, max_length=500)
    point: GeoPoint | None = None
    source_url: str = Field(max_length=2_000)
    raw_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confidence: float = Field(ge=0, le=1)

    _source_url = field_validator("source_url")(_validate_source_url)


class LocalityResolution(StrictModel):
    phrase: CleanText = Field(max_length=500)
    city: CleanText = Field(max_length=160)
    status: LocalityResolutionStatus
    candidates: list[LocalityCandidate] = Field(default_factory=list, max_length=5)
    selected: LocalityCandidate | None = None
    clarification_question: CleanText | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def coherent_status(self) -> LocalityResolution:
        if any(candidate.city.casefold() != self.city.casefold() for candidate in self.candidates):
            raise ValueError("all locality candidates must belong to the requested city")
        if self.status == LocalityResolutionStatus.RESOLVED:
            if self.selected is None:
                raise ValueError("resolved locality requires a selected candidate")
            if self.selected not in self.candidates:
                raise ValueError("selected locality must be present in candidates")
            if self.clarification_question is not None:
                raise ValueError("resolved locality cannot have a clarification question")
        else:
            if self.selected is not None:
                raise ValueError("unresolved locality cannot select a candidate")
            if self.clarification_question is None:
                raise ValueError("unresolved locality requires a clarification question")
        return self


def decide_locality_resolution(
    *,
    phrase: str,
    city: str,
    candidates: list[LocalityCandidate],
    auto_resolve_confidence: float = 0.90,
    minimum_lead: float = 0.15,
) -> LocalityResolution:
    """Apply an application-owned rule instead of trusting a model's confidence alone.

    Selecting a canonical place never invents a search radius. The downstream plan
    keeps the provider place identifier/point and must obtain an explicit area choice
    when candidates remain ambiguous.
    """

    if not 0 <= auto_resolve_confidence <= 1 or not 0 <= minimum_lead <= 1:
        raise ValueError("locality confidence thresholds must be between 0 and 1")
    normalized_city = city.strip().casefold()
    ordered = sorted(
        (candidate for candidate in candidates if candidate.city.casefold() == normalized_city),
        key=lambda candidate: (
            -candidate.confidence,
            candidate.canonical_name.casefold(),
            candidate.provider_place_id or "",
        ),
    )[:5]
    if not ordered:
        return LocalityResolution(
            phrase=phrase,
            city=city,
            status=LocalityResolutionStatus.UNRESOLVED,
            clarification_question=(
                f"Не удалось однозначно определить «{phrase}» в городе {city}. "
                "Пришлите геолокацию или ближайший ориентир."
            ),
        )
    runner_confidence = ordered[1].confidence if len(ordered) > 1 else 0.0
    first = ordered[0]
    if (
        first.confidence >= auto_resolve_confidence
        and first.confidence - runner_confidence >= minimum_lead
    ):
        return LocalityResolution(
            phrase=phrase,
            city=city,
            status=LocalityResolutionStatus.RESOLVED,
            candidates=ordered,
            selected=first,
        )
    choices = "; ".join(
        f"{candidate.canonical_name} ({candidate.place_type})" for candidate in ordered[:3]
    )
    return LocalityResolution(
        phrase=phrase,
        city=city,
        status=LocalityResolutionStatus.NEEDS_CLARIFICATION,
        candidates=ordered,
        clarification_question=f"Что вы имеете в виду под «{phrase}»: {choices}?",
    )


class LocalityConstraint(StrictModel):
    city: CleanText = Field(max_length=160)
    scope: SearchScope
    raw_area_text: CleanText | None = Field(default=None, max_length=500)
    canonical_place_id: CleanText | None = Field(default=None, max_length=300)
    canonical_place_name: CleanText | None = Field(default=None, max_length=300)
    point: GeoPoint | None = None

    @model_validator(mode="after")
    def specific_area_is_explicit(self) -> LocalityConstraint:
        if self.scope == SearchScope.SPECIFIC_AREA and self.raw_area_text is None:
            raise ValueError("specific-area search requires the user's raw area text")
        return self


class SourcePlan(StrictModel):
    item_index: int = Field(ge=0)
    source: ResearchSource
    purpose: ResearchPurpose
    query_terms: list[CleanText] = Field(min_length=1, max_length=20)
    rendered_query: CleanText = Field(max_length=1_000)
    locality: LocalityConstraint
    minimum_unique_candidates: int = Field(ge=1, le=100)
    result_ttl_hours: int = Field(ge=1, le=24 * 90)
    empty_page_limit: int = Field(ge=1, le=20)
    blocked_reason: CleanText | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def supplier_safe(self) -> SourcePlan:
        assert_no_private_price(
            {
                "query_terms": self.query_terms,
                "rendered_query": self.rendered_query,
            }
        )
        return self


class LocalityResearchTask(StrictModel):
    city: CleanText = Field(max_length=160)
    phrase: CleanText = Field(max_length=500)
    sources: list[ResearchSource] = Field(
        default_factory=lambda: [ResearchSource.TWO_GIS, ResearchSource.WEB],
        min_length=1,
        max_length=2,
    )
    maximum_candidates: int = Field(default=5, ge=1, le=5)

    @field_validator("sources")
    @classmethod
    def supported_sources(cls, value: list[ResearchSource]) -> list[ResearchSource]:
        if ResearchSource.OWN_DATABASE in value:
            raise ValueError("locality browser lookup supports only 2GIS and web")
        return list(dict.fromkeys(value))


class ResearchPlan(StrictModel):
    city: CleanText = Field(max_length=160)
    scope: SearchScope
    locality_resolution: LocalityResolution | None = None
    locality_task: LocalityResearchTask | None = None
    source_plans: list[SourcePlan] = Field(min_length=1, max_length=600)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _deduplicate_terms(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        stripped = value.strip()
        key = stripped.casefold()
        if stripped and key not in seen:
            assert_no_private_price(stripped, path="query_term")
            seen.add(key)
            result.append(stripped)
    return result


def _locality_constraint(
    request: ParsedProcurementRequest,
    locality_resolution: LocalityResolution | None,
) -> tuple[LocalityConstraint, str | None, LocalityResearchTask | None]:
    if request.city is None or request.search_scope is None:
        raise ValueError("city and search scope must be clarified before research planning")
    if request.search_scope != SearchScope.SPECIFIC_AREA:
        return (
            LocalityConstraint(city=request.city, scope=request.search_scope),
            None,
            None,
        )
    if request.search_area_text is None:
        raise ValueError("specific-area search requires search_area_text")
    task = LocalityResearchTask(city=request.city, phrase=request.search_area_text)
    if (
        locality_resolution is None
        or locality_resolution.status != LocalityResolutionStatus.RESOLVED
        or locality_resolution.phrase.casefold() != request.search_area_text.casefold()
        or locality_resolution.city.casefold() != request.city.casefold()
    ):
        return (
            LocalityConstraint(
                city=request.city,
                scope=request.search_scope,
                raw_area_text=request.search_area_text,
            ),
            "locality_requires_resolution",
            task,
        )
    selected = locality_resolution.selected
    assert selected is not None
    return (
        LocalityConstraint(
            city=request.city,
            scope=request.search_scope,
            raw_area_text=request.search_area_text,
            canonical_place_id=selected.provider_place_id,
            canonical_place_name=selected.canonical_name,
            point=selected.point,
        ),
        None,
        None,
    )


def build_research_plan(
    request: ParsedProcurementRequest,
    *,
    synonyms_by_item: dict[int, list[str]] | None = None,
    locality_resolution: LocalityResolution | None = None,
    minimum_unique_candidates: int = 10,
) -> ResearchPlan:
    """Build reproducible source branches from the approved intake projection.

    Technical requirements are intentionally not concatenated into search queries:
    they remain matching gates downstream. This both keeps queries compact and prevents
    a private price accidentally embedded in free-form specification text from leaking.
    """

    if request.city is None or request.search_scope is None:
        raise ValueError("request is not ready for research")
    if not request.items:
        raise ValueError("at least one procurement item is required")
    if not 1 <= minimum_unique_candidates <= 100:
        raise ValueError("minimum_unique_candidates must be between 1 and 100")
    synonyms_by_item = synonyms_by_item or {}
    unknown_indexes = set(synonyms_by_item) - set(range(len(request.items)))
    if unknown_indexes:
        raise ValueError(f"synonyms reference unknown item indexes: {sorted(unknown_indexes)}")
    locality, blocked_reason, locality_task = _locality_constraint(
        request, locality_resolution
    )
    branches: list[SourcePlan] = []
    source_settings = (
        (
            ResearchSource.OWN_DATABASE,
            ResearchPurpose.KNOWN_SUPPLIER_LOOKUP,
            24 * 7,
            1,
        ),
        (ResearchSource.TWO_GIS, ResearchPurpose.BUSINESS_DISCOVERY, 24, 2),
        (ResearchSource.WEB, ResearchPurpose.PRODUCT_EVIDENCE, 24, 3),
    )
    per_branch_minimum = max(1, (minimum_unique_candidates + 2) // 3)
    for index, item in enumerate(request.items):
        if item.name is None:
            raise ValueError(f"item {index} has no product name")
        terms = _deduplicate_terms([item.name, *synonyms_by_item.get(index, [])])
        for source, purpose, ttl_hours, empty_page_limit in source_settings:
            query_parts = [*terms, request.city]
            if locality.canonical_place_name is not None:
                query_parts.append(locality.canonical_place_name)
            elif request.search_scope == SearchScope.ONLINE_WITH_DELIVERY:
                query_parts.append("доставка")
            branches.append(
                SourcePlan(
                    item_index=index,
                    source=source,
                    purpose=purpose,
                    query_terms=terms,
                    rendered_query=" ".join(query_parts),
                    locality=locality,
                    minimum_unique_candidates=per_branch_minimum,
                    result_ttl_hours=ttl_hours,
                    empty_page_limit=empty_page_limit,
                    blocked_reason=blocked_reason,
                )
            )
    unsigned = {
        "city": request.city,
        "scope": request.search_scope.value,
        "locality_resolution": (
            locality_resolution.model_dump(mode="json") if locality_resolution else None
        ),
        "locality_task": locality_task.model_dump(mode="json") if locality_task else None,
        "source_plans": [branch.model_dump(mode="json") for branch in branches],
    }
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    plan_hash = hashlib.sha256(encoded.encode()).hexdigest()
    return ResearchPlan(
        **unsigned,
        plan_sha256=plan_hash,
    )


class SupplierContact(StrictModel):
    kind: ContactKind
    value: CleanText = Field(max_length=500)
    publicly_listed: bool = True
    confidence: float = Field(ge=0, le=1)


class SupplierEvidence(StrictModel):
    """One immutable observation; a business card is not product availability."""

    evidence_type: EvidenceType
    source: ResearchSource
    source_url: str = Field(max_length=2_000)
    observed_at: AwareDatetime
    raw_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    claims: list[EvidenceClaim] = Field(min_length=1, max_length=20)
    excerpt: str | None = Field(default=None, max_length=2_000)
    availability: AvailabilityStatus = AvailabilityStatus.UNKNOWN
    price_amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=4)
    currency: CleanText | None = Field(default=None, max_length=3)

    _source_url = field_validator("source_url")(_validate_source_url)

    @field_validator("claims")
    @classmethod
    def unique_claims(cls, value: list[EvidenceClaim]) -> list[EvidenceClaim]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def evidence_semantics(self) -> SupplierEvidence:
        if (self.price_amount is None) != (self.currency is None):
            raise ValueError("price amount and currency must be supplied together")
        if self.evidence_type == EvidenceType.BUSINESS_LISTING:
            product_claims = {
                EvidenceClaim.PRODUCT_MATCH,
                EvidenceClaim.ADVERTISED_PRICE,
                EvidenceClaim.AVAILABILITY,
            }
            if (
                product_claims.intersection(self.claims)
                or self.availability != AvailabilityStatus.UNKNOWN
                or self.price_amount is not None
            ):
                raise ValueError("business listing cannot prove stock or product price")
        if self.price_amount is not None and EvidenceClaim.ADVERTISED_PRICE not in self.claims:
            raise ValueError("captured price requires an advertised-price claim")
        if (
            self.availability != AvailabilityStatus.UNKNOWN
            and EvidenceClaim.AVAILABILITY not in self.claims
        ):
            raise ValueError("availability status requires an availability claim")
        if self.evidence_type == EvidenceType.PRODUCT_PAGE and self.availability in {
            AvailabilityStatus.CONFIRMED_IN_STOCK,
            AvailabilityStatus.CONFIRMED_OUT_OF_STOCK,
        }:
            raise ValueError("a public product page can advertise but not confirm current stock")
        if (
            self.evidence_type != EvidenceType.SUPPLIER_STATEMENT
            and self.availability
            in {
                AvailabilityStatus.CONFIRMED_IN_STOCK,
                AvailabilityStatus.CONFIRMED_OUT_OF_STOCK,
            }
        ):
            raise ValueError("confirmed stock requires a supplier statement")
        return self


class SupplierCandidate(StrictModel):
    source: ResearchSource
    external_id: CleanText | None = Field(default=None, max_length=300)
    name: CleanText = Field(max_length=500)
    categories: list[CleanText] = Field(default_factory=list, max_length=50)
    address: CleanText | None = Field(default=None, max_length=500)
    point: GeoPoint | None = None
    contacts: list[SupplierContact] = Field(default_factory=list, max_length=30)
    evidence: list[SupplierEvidence] = Field(min_length=1, max_length=100)
    product_availability: AvailabilityStatus = AvailabilityStatus.UNKNOWN
    field_confidence: dict[str, float] = Field(default_factory=dict, max_length=50)

    @field_validator("field_confidence")
    @classmethod
    def valid_confidences(cls, value: dict[str, float]) -> dict[str, float]:
        invalid = [key for key, confidence in value.items() if not 0 <= confidence <= 1]
        if invalid:
            raise ValueError(f"field confidence must be between 0 and 1: {invalid}")
        return value

    @model_validator(mode="after")
    def availability_is_evidenced(self) -> SupplierCandidate:
        supporting = [
            evidence
            for evidence in self.evidence
            if evidence.availability == self.product_availability
            and EvidenceClaim.AVAILABILITY in evidence.claims
        ]
        if self.product_availability != AvailabilityStatus.UNKNOWN and not supporting:
            raise ValueError("candidate availability must be supported by evidence")
        return self


def utc_observed_at(value: datetime) -> datetime:
    """Small explicit helper for adapters that need to reject naive timestamps early."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must contain a timezone")
    return value
