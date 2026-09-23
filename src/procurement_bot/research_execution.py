from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
from pydantic import ValidationError

from procurement_bot.providers.browser import (
    BROWSER_SPEC_SHA256,
    BrowserProviderError,
    BrowserResearchPort,
    SupplierBrowserResult,
)
from procurement_bot.research import (
    AvailabilityStatus,
    EvidenceType,
    PrivatePriceLeakError,
    ResearchPlan,
    ResearchSource,
    SourcePlan,
    SupplierCandidate,
    assert_no_private_price,
)
from procurement_bot.research_repository import recompute_plan_sha256
from procurement_bot.suppliers import (
    OfferInput,
    OfferObservationInput,
    PersistenceConflictError,
    SourceEvidenceInput,
    SupplierContactInput,
    SupplierInput,
    SupplierLocationInput,
    upsert_offer,
    upsert_offer_observation,
    upsert_source_evidence,
    upsert_supplier,
    upsert_supplier_contact,
    upsert_supplier_location,
)

TerminalStatus = Literal["succeeded", "partial", "failed"]

_PROVIDER_BY_SOURCE = {
    ResearchSource.OWN_DATABASE: "own_database",
    ResearchSource.TWO_GIS: "2gis_web",
    ResearchSource.WEB: "web",
}
_SOURCE_TYPE = {
    ResearchSource.TWO_GIS: "2gis",
    ResearchSource.WEB: "web",
}
_ADVANCED_CASE_STATUSES = {
    "evaluating",
    "report_ready",
    "selected",
    "closed",
}


class ResearchExecutionRejected(RuntimeError):
    """The durable run is not a safe, executable projection."""


class ResearchPlanDrift(ResearchExecutionRejected):
    """The run, its hash, or its materialized query rows disagree."""


@dataclass(frozen=True, slots=True)
class ResearchExecutionResult:
    run_id: UUID
    case_id: UUID
    status: TerminalStatus
    error_code: str
    branches_total: int
    branches_succeeded: int
    branches_failed: int
    branches_blocked: int
    candidates_persisted: int
    own_database_candidates: int
    newly_completed: bool


@dataclass(frozen=True, slots=True)
class _QueryBranch:
    id: UUID
    plan: SourcePlan
    request_item_id: UUID


@dataclass(frozen=True, slots=True)
class _Snapshot:
    run_id: UUID
    case_id: UUID
    plan: ResearchPlan
    branches: tuple[_QueryBranch, ...]


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ResearchPlanDrift("persisted research JSON is not lossless") from exc


def _mapping(value: object, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ResearchPlanDrift(f"{field} is not valid JSON") from exc
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ResearchPlanDrift(f"{field} must be a JSON object")
    return dict(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _private_price_preflight(plan: Mapping[str, Any]) -> None:
    branches = plan.get("source_plans")
    if not isinstance(branches, list):
        raise ResearchPlanDrift("persisted plan has no source_plans array")
    for ordinal, branch in enumerate(branches):
        if not isinstance(branch, Mapping):
            raise ResearchPlanDrift(f"source plan {ordinal} is not an object")
        assert_no_private_price(
            {
                "query_terms": branch.get("query_terms"),
                "rendered_query": branch.get("rendered_query"),
            },
            path=f"plan.source_plans[{ordinal}]",
        )


def _expected_parameters(branch: SourcePlan, ordinal: int) -> dict[str, Any]:
    return {
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
    }


def _expected_search_area(branch: SourcePlan) -> str:
    locality = branch.locality
    return locality.raw_area_text or locality.canonical_place_name or ""


def _coordinate(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _validate_query_row(row: Mapping[str, Any], branch: SourcePlan, ordinal: int) -> None:
    point = branch.locality.point
    expected: dict[str, Any] = {
        "provider": _PROVIDER_BY_SOURCE[branch.source],
        "query_text": branch.rendered_query,
        "city": branch.locality.city,
        "search_area_text": _expected_search_area(branch),
        "latitude": Decimal(str(point.latitude)) if point is not None else None,
        "longitude": Decimal(str(point.longitude)) if point is not None else None,
        "radius_meters": None,
        "parameters": _expected_parameters(branch, ordinal),
    }
    for field, expected_value in expected.items():
        if field not in row:
            raise ResearchPlanDrift(f"research query is missing {field}")
        actual = (
            _mapping(row[field], field="research query parameters")
            if field == "parameters"
            else row[field]
        )
        if field in {"latitude", "longitude"}:
            actual = _coordinate(actual)
        if actual != expected_value:
            raise ResearchPlanDrift(f"research query {field} differs from the plan")


def _parse_plan(search_scope: object) -> ResearchPlan:
    scope = _mapping(search_scope, field="research run search_scope")
    if set(scope) != {"contract_version", "plan_sha256", "plan"}:
        raise ResearchPlanDrift("research run search_scope has an unknown shape")
    if scope["contract_version"] != 1:
        raise ResearchPlanDrift("unsupported research plan contract version")
    raw_plan = _mapping(scope["plan"], field="research run plan")
    persisted_hash = scope["plan_sha256"]
    if not isinstance(persisted_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", persisted_hash):
        raise ResearchPlanDrift("research run has no valid plan_sha256")
    if _digest(raw_plan) != persisted_hash:
        raise ResearchPlanDrift("persisted research plan hash is invalid")
    _private_price_preflight(raw_plan)
    try:
        plan = ResearchPlan.model_validate({**raw_plan, "plan_sha256": persisted_hash})
    except ValidationError as exc:
        raise ResearchPlanDrift("persisted research plan does not match its schema") from exc
    if recompute_plan_sha256(plan) != persisted_hash:
        raise ResearchPlanDrift("validated research plan hash changed")
    return plan


def _request_hash(plan: SourcePlan) -> str:
    return hashlib.sha256(
        _canonical_json(plan.model_dump(mode="json")).encode()
    ).hexdigest()


def _validate_browser_result(
    plan: SourcePlan,
    value: SupplierBrowserResult,
) -> SupplierBrowserResult:
    result = SupplierBrowserResult.model_validate(value)
    if result.spec_sha256 != BROWSER_SPEC_SHA256:
        raise ResearchExecutionRejected("browser result uses a stale policy")
    if result.request_sha256 != _request_hash(plan):
        raise ResearchExecutionRejected("browser result belongs to another query")
    if len(result.candidates) > plan.minimum_unique_candidates * 5:
        raise ResearchExecutionRejected("browser returned too many candidates")
    identities: set[tuple[str, str]] = set()
    for candidate in result.candidates:
        if candidate.source != plan.source:
            raise ResearchExecutionRejected("candidate source differs from its branch")
        if plan.source == ResearchSource.TWO_GIS and not candidate.external_id:
            raise ResearchExecutionRejected("2GIS candidate has no firm id")
        for evidence in candidate.evidence:
            if evidence.source != plan.source:
                raise ResearchExecutionRejected("evidence source differs from its branch")
            host = (urlparse(evidence.source_url).hostname or "").casefold()
            if plan.source == ResearchSource.TWO_GIS and not (
                host == "2gis.kz" or host.endswith(".2gis.kz")
            ):
                raise ResearchExecutionRejected("2GIS evidence URL is not official")
            if evidence.evidence_type == EvidenceType.SUPPLIER_STATEMENT:
                raise ResearchExecutionRejected("browser cannot produce supplier statements")
            if evidence.currency is not None and not re.fullmatch(
                r"[A-Z]{3}", evidence.currency.upper()
            ):
                raise ResearchExecutionRejected("browser returned an invalid currency")
        if candidate.product_availability in {
            AvailabilityStatus.CONFIRMED_IN_STOCK,
            AvailabilityStatus.CONFIRMED_OUT_OF_STOCK,
        }:
            raise ResearchExecutionRejected("browser cannot confirm current stock")
        identity = _identity(candidate)
        if identity in identities:
            raise ResearchExecutionRejected("browser returned duplicate supplier identities")
        identities.add(identity)
    return result


def _identity(candidate: SupplierCandidate) -> tuple[str, str]:
    if candidate.source == ResearchSource.TWO_GIS:
        assert candidate.external_id is not None
        return "2gis_firm_id", candidate.external_id
    first_host = (urlparse(candidate.evidence[0].source_url).hostname or "").casefold()
    if candidate.external_id:
        return "source_record", f"web:{first_host}:{candidate.external_id}"
    return "domain", first_host


def _confidence(candidate: SupplierCandidate, field: str, default: str) -> Decimal:
    return Decimal(str(candidate.field_confidence.get(field, float(default))))


def _evidence_key(query_id: UUID, candidate: SupplierCandidate, evidence: Any) -> str:
    material = {
        "query_id": str(query_id),
        "candidate_source": candidate.source.value,
        "candidate_external_id": candidate.external_id,
        "candidate_name": candidate.name,
        "evidence": evidence.model_dump(mode="json"),
    }
    return f"browser-evidence:{_digest(material)}"


def _offer_key(query_id: UUID, supplier_id: UUID) -> str:
    return f"research-offer:{_digest({'query_id': str(query_id), 'supplier_id': str(supplier_id)})}"


def _observation_availability(value: AvailabilityStatus) -> str:
    if value == AvailabilityStatus.PUBLICLY_ADVERTISED:
        return "in_stock_unquantified"
    return "unknown"


@asynccontextmanager
async def _connection(
    database: asyncpg.Pool | asyncpg.Connection,
) -> AsyncIterator[asyncpg.Connection]:
    acquire = getattr(database, "acquire", None)
    if acquire is None:
        yield database  # type: ignore[misc]
        return
    async with acquire() as connection:
        yield connection


class ResearchRunExecutor:
    """Execute one approved research run without owning any messaging capability."""

    def __init__(
        self,
        database: asyncpg.Pool | asyncpg.Connection,
        browser: BrowserResearchPort,
    ) -> None:
        self.database = database
        self.browser = browser

    async def execute(self, run_id: UUID) -> ResearchExecutionResult:
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        async with _connection(self.database) as connection:
            run = await connection.fetchrow(
                """
                SELECT id,case_id,status,search_scope,error_code
                FROM research_runs
                WHERE id=$1
                """,
                run_id,
            )
            if run is None:
                raise ResearchExecutionRejected("research run does not exist")
            case_id: UUID = run["case_id"]
            if run["status"] in {"succeeded", "partial", "failed"}:
                return ResearchExecutionResult(
                    run_id=run_id,
                    case_id=case_id,
                    status=run["status"],
                    error_code=run["error_code"],
                    branches_total=0,
                    branches_succeeded=0,
                    branches_failed=0,
                    branches_blocked=0,
                    candidates_persisted=0,
                    own_database_candidates=0,
                    newly_completed=False,
                )
            if run["status"] != "running":
                raise ResearchExecutionRejected("research run is not running")

            try:
                snapshot = await self._load_snapshot(connection, run)
            except PrivatePriceLeakError:
                return await self._finish_rejected(
                    connection,
                    run_id=run_id,
                    case_id=case_id,
                    error_code="private_price_detected",
                )
            except ResearchPlanDrift:
                return await self._finish_rejected(
                    connection,
                    run_id=run_id,
                    case_id=case_id,
                    error_code="research_plan_drift",
                )

            succeeded = failed = blocked = persisted = known = incomplete = 0
            for branch in snapshot.branches:
                if branch.plan.blocked_reason is not None:
                    blocked += 1
                    continue
                if branch.plan.source == ResearchSource.OWN_DATABASE:
                    known += await self._count_known_candidates(connection, branch)
                    succeeded += 1
                    continue
                try:
                    raw_result = await self.browser.search_suppliers(branch.plan)
                    result = _validate_browser_result(branch.plan, raw_result)
                except (BrowserProviderError, ResearchExecutionRejected, ValidationError):
                    # Browser calls are the sole unreliable network boundary. Database
                    # persistence happens outside this catch and therefore remains retryable.
                    failed += 1
                    continue
                try:
                    async with connection.transaction():
                        persisted += await self._persist_branch(connection, branch, result)
                except (PersistenceConflictError, ValueError):
                    failed += 1
                    continue
                succeeded += 1
                if (
                    not result.exhausted
                    and len(result.candidates) < branch.plan.minimum_unique_candidates
                ):
                    incomplete += 1

            status, error_code = self._outcome(
                succeeded=succeeded,
                failed=failed,
                blocked=blocked,
                incomplete=incomplete,
            )
            await self._terminalize(
                connection,
                run_id=run_id,
                case_id=case_id,
                status=status,
                error_code=error_code,
            )
            return ResearchExecutionResult(
                run_id=run_id,
                case_id=case_id,
                status=status,
                error_code=error_code,
                branches_total=len(snapshot.branches),
                branches_succeeded=succeeded,
                branches_failed=failed,
                branches_blocked=blocked,
                candidates_persisted=persisted,
                own_database_candidates=known,
                newly_completed=True,
            )

    async def _load_snapshot(
        self,
        connection: asyncpg.Connection,
        run: Mapping[str, Any],
    ) -> _Snapshot:
        plan = _parse_plan(run["search_scope"])
        query_rows = await connection.fetch(
            """
            SELECT id,research_run_id,provider,query_text,city,search_area_text,
                latitude,longitude,radius_meters,parameters
            FROM research_queries
            WHERE research_run_id=$1
            ORDER BY (parameters->>'branch_ordinal')::integer,id
            """,
            run["id"],
        )
        if len(query_rows) != len(plan.source_plans):
            raise ResearchPlanDrift("research query count differs from the plan")
        item_rows = await connection.fetch(
            """
            SELECT id,line_number
            FROM request_items
            WHERE case_id=$1
            ORDER BY line_number,id
            """,
            run["case_id"],
        )
        items: dict[int, UUID] = {}
        for expected_line, row in enumerate(item_rows, start=1):
            if row["line_number"] != expected_line:
                raise ResearchPlanDrift("request item line numbers are not contiguous")
            items[expected_line - 1] = row["id"]
        branches: list[_QueryBranch] = []
        for ordinal, (row, branch) in enumerate(zip(query_rows, plan.source_plans, strict=True)):
            if row["research_run_id"] != run["id"]:
                raise ResearchPlanDrift("research query belongs to another run")
            _validate_query_row(row, branch, ordinal)
            item_id = items.get(branch.item_index)
            if item_id is None:
                raise ResearchPlanDrift("research branch references a missing request item")
            branches.append(_QueryBranch(id=row["id"], plan=branch, request_item_id=item_id))
        return _Snapshot(run["id"], run["case_id"], plan, tuple(branches))

    async def _count_known_candidates(
        self,
        connection: asyncpg.Connection,
        branch: _QueryBranch,
    ) -> int:
        value = await connection.fetchval(
            """
            SELECT count(DISTINCT o.supplier_id)
            FROM offers AS o
            JOIN suppliers AS s ON s.id=o.supplier_id
            LEFT JOIN supplier_locations AS l ON l.id=o.supplier_location_id
            WHERE o.request_item_id=$1
              AND s.status IN ('candidate','active')
              AND (l.id IS NULL OR l.city='' OR lower(l.city)=lower($2))
            """,
            branch.request_item_id,
            branch.plan.locality.city,
        )
        return int(value or 0)

    async def _persist_branch(
        self,
        connection: asyncpg.Connection,
        branch: _QueryBranch,
        result: SupplierBrowserResult,
    ) -> int:
        for candidate in result.candidates:
            evidence_ids: list[UUID] = []
            for evidence in candidate.evidence:
                raw_payload = evidence.model_dump(mode="json")
                stored = await upsert_source_evidence(
                    connection,
                    SourceEvidenceInput(
                        research_query_id=branch.id,
                        source_type=_SOURCE_TYPE[candidate.source],
                        source_locator=evidence.source_url,
                        source_url=evidence.source_url,
                        external_id=candidate.external_id or "",
                        title=candidate.name,
                        captured_at=evidence.observed_at,
                        # The adapter's raw_snapshot_sha256 is an untrusted claim until
                        # this service owns snapshot bytes. Hash exactly what we persist.
                        content_sha256=_digest(raw_payload),
                        raw_payload=raw_payload,
                        idempotency_key=_evidence_key(branch.id, candidate, evidence),
                    ),
                )
                evidence_ids.append(stored.id)
            primary_evidence_id = evidence_ids[0]
            identity_type, identity_value = _identity(candidate)
            supplier = await upsert_supplier(
                connection,
                SupplierInput(
                    display_name=candidate.name,
                    identity_type=identity_type,
                    identity_value=identity_value,
                    source_evidence_id=primary_evidence_id,
                    confidence=_confidence(candidate, "name", "0.7"),
                ),
            )
            for contact in candidate.contacts:
                await upsert_supplier_contact(
                    connection,
                    supplier.id,
                    SupplierContactInput(
                        contact_type=contact.kind.value,
                        display_value=contact.value,
                        source_evidence_id=primary_evidence_id,
                        confidence=Decimal(str(contact.confidence)),
                    ),
                )
            location_id: UUID | None = None
            if (
                candidate.address is not None
                or candidate.point is not None
                or candidate.external_id
            ):
                location = await upsert_supplier_location(
                    connection,
                    supplier.id,
                    SupplierLocationInput(
                        source_evidence_id=primary_evidence_id,
                        city=branch.plan.locality.city,
                        address_text=candidate.address or "",
                        latitude=(
                            Decimal(str(candidate.point.latitude))
                            if candidate.point is not None
                            else None
                        ),
                        longitude=(
                            Decimal(str(candidate.point.longitude))
                            if candidate.point is not None
                            else None
                        ),
                        source_provider=candidate.source.value,
                        provider_place_id=candidate.external_id or "",
                        confidence=_confidence(candidate, "address", "0.7"),
                    ),
                )
                location_id = location.id
            offer = await upsert_offer(
                connection,
                OfferInput(
                    request_item_id=branch.request_item_id,
                    supplier_id=supplier.id,
                    supplier_location_id=location_id,
                    idempotency_key=_offer_key(branch.id, supplier.id),
                    status="lead",
                ),
            )
            for evidence, evidence_id in zip(candidate.evidence, evidence_ids, strict=True):
                observation_material = {
                    "offer_id": str(offer.id),
                    "evidence_id": str(evidence_id),
                }
                observation_key = (
                    f"research-observation:{_digest(observation_material)}"
                )
                await upsert_offer_observation(
                    connection,
                    OfferObservationInput(
                        offer_id=offer.id,
                        source_evidence_id=evidence_id,
                        observed_at=evidence.observed_at,
                        observed_by="research-executor",
                        source_type=_SOURCE_TYPE[candidate.source],
                        price_amount=evidence.price_amount,
                        currency=evidence.currency.upper() if evidence.currency else None,
                        availability_status=_observation_availability(evidence.availability),
                        raw_payload=evidence.model_dump(mode="json"),
                        idempotency_key=observation_key,
                    ),
                )
        return len(result.candidates)

    @staticmethod
    def _outcome(
        *,
        succeeded: int,
        failed: int,
        blocked: int,
        incomplete: int,
    ) -> tuple[TerminalStatus, str]:
        if succeeded == 0:
            if blocked and not failed:
                return "failed", "all_research_branches_blocked"
            return "failed", "all_research_branches_failed"
        if failed or blocked or incomplete:
            reasons: list[str] = []
            if failed:
                reasons.append("source_failed")
            if blocked:
                reasons.append("source_blocked")
            if incomplete:
                reasons.append("source_incomplete")
            return "partial", "+".join(reasons)
        return "succeeded", ""

    async def _finish_rejected(
        self,
        connection: asyncpg.Connection,
        *,
        run_id: UUID,
        case_id: UUID,
        error_code: str,
    ) -> ResearchExecutionResult:
        await self._terminalize(
            connection,
            run_id=run_id,
            case_id=case_id,
            status="failed",
            error_code=error_code,
        )
        return ResearchExecutionResult(
            run_id=run_id,
            case_id=case_id,
            status="failed",
            error_code=error_code,
            branches_total=0,
            branches_succeeded=0,
            branches_failed=0,
            branches_blocked=0,
            candidates_persisted=0,
            own_database_candidates=0,
            newly_completed=True,
        )

    async def _terminalize(
        self,
        connection: asyncpg.Connection,
        *,
        run_id: UUID,
        case_id: UUID,
        status: TerminalStatus,
        error_code: str,
    ) -> None:
        target_case_status = "ready" if status == "failed" else "evaluating"
        async with connection.transaction():
            case = await connection.fetchrow(
                "SELECT id,status FROM procurement_cases WHERE id=$1 FOR UPDATE",
                case_id,
            )
            if case is None:
                raise ResearchExecutionRejected("procurement case does not exist")
            if case["status"] == "researching":
                updated = await connection.execute(
                    """
                    UPDATE procurement_cases
                    SET status=$2,updated_at=now()
                    WHERE id=$1 AND status='researching'
                    """,
                    case_id,
                    target_case_status,
                )
                if updated != "UPDATE 1":
                    raise ResearchExecutionRejected("procurement case changed during completion")
            elif not (
                (status == "failed" and case["status"] == "ready")
                or (status != "failed" and case["status"] in _ADVANCED_CASE_STATUSES)
            ):
                raise ResearchExecutionRejected("procurement case is not in an executable state")
            updated_run = await connection.execute(
                """
                UPDATE research_runs
                SET status=$2,completed_at=COALESCE(completed_at,now()),error_code=$3
                WHERE id=$1 AND status='running'
                """,
                run_id,
                status,
                error_code,
            )
            if updated_run != "UPDATE 1":
                raise ResearchExecutionRejected("research run changed during completion")
