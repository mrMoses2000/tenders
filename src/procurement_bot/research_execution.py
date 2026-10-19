from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
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

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.browser import (
    BROWSER_SPEC_SHA256,
    BrowserProviderError,
    BrowserResearchPort,
    SupplierBrowserResult,
)
from procurement_bot.queue import stable_key
from procurement_bot.research import (
    AvailabilityStatus,
    ContactKind,
    EvidenceClaim,
    EvidenceType,
    PrivatePriceLeakError,
    ResearchPlan,
    ResearchSource,
    SourcePlan,
    SupplierCandidate,
    assert_no_private_price,
    olx_listing_id_matches_url,
)
from procurement_bot.research_repository import recompute_plan_sha256
from procurement_bot.suppliers import (
    OfferInput,
    OfferObservationInput,
    PersistenceConflictError,
    SourceEvidenceInput,
    SupplierContactInput,
    SupplierIdentityConflictError,
    SupplierInput,
    SupplierLocationInput,
    normalize_contact,
    upsert_offer,
    upsert_offer_observation,
    upsert_olx_supplier_by_contact,
    upsert_source_evidence,
    upsert_supplier,
    upsert_supplier_contact,
    upsert_supplier_location,
)

TerminalStatus = Literal["succeeded", "partial", "failed"]
LOGGER = logging.getLogger(__name__)

_PROVIDER_BY_SOURCE = {
    ResearchSource.OWN_DATABASE: "own_database",
    ResearchSource.TWO_GIS: "2gis_web",
    ResearchSource.OLX: "marketplace",
    ResearchSource.WEB: "web",
}
_SOURCE_TYPE = {
    ResearchSource.TWO_GIS: "2gis",
    ResearchSource.OLX: "marketplace",
    ResearchSource.WEB: "web",
}
_ADVANCED_CASE_STATUSES = {
    "evaluating",
    "contacting",
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
    base_fields = {"contract_version", "plan_sha256", "plan"}
    fields = set(scope)
    valid_shapes = {
        frozenset(base_fields),
        frozenset(base_fields | {"sourcing_instruction_ids"}),
    }
    if fields not in valid_shapes:
        raise ResearchPlanDrift("research run search_scope has an unknown shape")
    if "sourcing_instruction_ids" in scope:
        instruction_ids = scope["sourcing_instruction_ids"]
        if not isinstance(instruction_ids, list) or any(
            not isinstance(value, str) for value in instruction_ids
        ):
            raise ResearchPlanDrift("sourcing instruction IDs must be a UUID string array")
        try:
            canonical_ids = [str(UUID(value)) for value in instruction_ids]
        except ValueError as exc:
            raise ResearchPlanDrift("sourcing instruction IDs must be canonical UUIDs") from exc
        if canonical_ids != instruction_ids or len(set(canonical_ids)) != len(canonical_ids):
            raise ResearchPlanDrift("sourcing instruction IDs must be unique canonical UUIDs")
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
        _canonical_json(plan.browser_payload()).encode()
    ).hexdigest()


def _catalog_links(candidates: list[SupplierCandidate]) -> list[str]:
    """Extract bounded public catalogue leads from accepted 2GIS firm cards."""

    links: list[str] = []
    for candidate in candidates:
        if candidate.source != ResearchSource.TWO_GIS:
            continue
        for contact in candidate.contacts:
            if contact.kind not in {ContactKind.WEBSITE, ContactKind.INSTAGRAM}:
                continue
            parsed = urlparse(contact.value)
            host = (parsed.hostname or "").casefold()
            try:
                ipaddress.ip_address(host)
            except ValueError:
                is_ip_address = False
            else:
                is_ip_address = True
            if (
                parsed.scheme != "https" or not host or parsed.username or parsed.password
                or is_ip_address or host == "localhost" or host.endswith(".local")
                or (contact.kind == ContactKind.INSTAGRAM
                    and host not in {"instagram.com", "www.instagram.com"})
            ):
                continue
            url = parsed._replace(query="", fragment="").geturl().rstrip("/")
            if url not in links:
                links.append(url)
            if len(links) == 4:
                return links
    return links


def _web_plan_with_catalog_links(plan: SourcePlan, links: list[str]) -> SourcePlan:
    if plan.source != ResearchSource.WEB or not links:
        return plan
    unique_links = [url for url in links if url not in plan.query_terms][:4]
    # A provider task is bounded; keep the product name and as many original clues as fit.
    terms = [*plan.query_terms[:20 - len(unique_links)], *unique_links]
    return SourcePlan.model_validate({**plan.browser_payload(), "query_terms": terms})


def _validate_browser_result(
    plan: SourcePlan,
    value: SupplierBrowserResult,
) -> SupplierBrowserResult:
    result = SupplierBrowserResult.model_validate(value)
    if result.spec_sha256 != BROWSER_SPEC_SHA256:
        raise ResearchExecutionRejected("browser result uses a stale policy")
    if result.request_sha256 != _request_hash(plan):
        raise ResearchExecutionRejected("browser result belongs to another query")
    if result.pages_examined == 0:
        raise ResearchExecutionRejected("browser search inspected no pages")
    if len(result.candidates) > plan.minimum_unique_candidates * 5:
        raise ResearchExecutionRejected("browser returned too many candidates")
    identities: set[tuple[str, str]] = set()
    for candidate in result.candidates:
        if candidate.source != plan.source:
            raise ResearchExecutionRejected("candidate source differs from its branch")
        if (plan.source in {ResearchSource.TWO_GIS, ResearchSource.OLX}
                and not candidate.external_id):
            raise ResearchExecutionRejected("source candidate has no stable id")
        for evidence in candidate.evidence:
            if evidence.source != plan.source:
                raise ResearchExecutionRejected("evidence source differs from its branch")
            host = (urlparse(evidence.source_url).hostname or "").casefold()
            if plan.source == ResearchSource.TWO_GIS and not (
                host == "2gis.kz" or host.endswith(".2gis.kz")
            ):
                raise ResearchExecutionRejected("2GIS evidence URL is not official")
            if plan.source == ResearchSource.OLX and (
                urlparse(evidence.source_url).scheme != "https"
                or host not in {"olx.kz", "www.olx.kz"}
            ):
                raise ResearchExecutionRejected("OLX evidence URL is not official")
            if evidence.evidence_type == EvidenceType.SUPPLIER_STATEMENT:
                raise ResearchExecutionRejected("browser cannot produce supplier statements")
            if evidence.currency is not None and not re.fullmatch(
                r"[A-Z]{3}", evidence.currency.upper()
            ):
                raise ResearchExecutionRejected("browser returned an invalid currency")
        if plan.source == ResearchSource.OLX and not any(
            olx_listing_id_matches_url(candidate.external_id or "", evidence.source_url)
            for evidence in candidate.evidence
        ):
            raise ResearchExecutionRejected(
                "OLX listing ID must match a cited listing evidence URL"
            )
        if candidate.product_availability in {
            AvailabilityStatus.CONFIRMED_IN_STOCK,
            AvailabilityStatus.CONFIRMED_OUT_OF_STOCK,
        }:
            raise ResearchExecutionRejected("browser cannot confirm current stock")
        identity = (_identity(candidate) if plan.source != ResearchSource.OLX
                    else ("olx_listing", candidate.external_id))
        if identity in identities:
            raise ResearchExecutionRejected("browser returned duplicate supplier identities")
        identities.add(identity)
    return result


def _identity(candidate: SupplierCandidate) -> tuple[str, str]:
    if candidate.source == ResearchSource.TWO_GIS:
        assert candidate.external_id is not None
        return "2gis_firm_id", candidate.external_id
    if candidate.source == ResearchSource.OLX:
        assert candidate.external_id is not None
        if candidate.seller_profile_id:
            return "source_record", f"olx_seller:{candidate.seller_profile_id}"
        return "source_record", f"olx:{candidate.external_id}"
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


def _offer_key(query_id: UUID, supplier_id: UUID, listing_id: str | None = None) -> str:
    material = {"query_id": str(query_id), "supplier_id": str(supplier_id)}
    if listing_id is not None:
        material["listing_id"] = listing_id
    return f"research-offer:{_digest(material)}"


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
                SELECT id,case_id,status,search_scope,error_code,
                    EXISTS (SELECT 1 FROM research_scope_revisions revision
                        WHERE revision.research_run_id=research_runs.id) AS has_scope_revision
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
            if run.get("has_scope_revision"):
                raise ResearchExecutionRejected("scope_revision_requires_parallel_execution")

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
            catalog_links_by_item: dict[int, list[str]] = {}
            for branch in snapshot.branches:
                if branch.plan.blocked_reason is not None:
                    blocked += 1
                    continue
                if branch.plan.source == ResearchSource.OWN_DATABASE:
                    found = await self._count_known_candidates(connection, branch)
                    known += found
                    LOGGER.info(
                        "research branch run=%s source=%s known=%s",
                        run_id, branch.plan.source.value, found,
                    )
                    succeeded += 1
                    continue
                try:
                    search_plan = _web_plan_with_catalog_links(
                        branch.plan, catalog_links_by_item.get(branch.plan.item_index, [])
                    )
                    raw_result = await self.browser.search_suppliers(search_plan)
                    result = _validate_browser_result(search_plan, raw_result)
                except (BrowserProviderError, PermanentProviderError,
                        RetryableProviderError, ResearchExecutionRejected,
                        ValidationError) as exc:
                    # Browser calls are the sole unreliable network boundary. Database
                    # persistence happens outside this catch and therefore remains retryable.
                    LOGGER.warning(
                        "research branch run=%s source=%s failed=%s detail=%s",
                        run_id, branch.plan.source.value, type(exc).__name__,
                        str(exc)[:240].replace("\n", " "),
                    )
                    failed += 1
                    continue
                try:
                    async with connection.transaction():
                        saved = await self._persist_branch(connection, branch, result)
                        persisted += saved
                except (PersistenceConflictError, ValueError) as exc:
                    LOGGER.warning(
                        "research branch run=%s source=%s persistence_failed=%s",
                        run_id, branch.plan.source.value, type(exc).__name__,
                    )
                    failed += 1
                    continue
                LOGGER.info(
                    "research branch run=%s source=%s pages=%s candidates=%s saved=%s",
                    run_id, branch.plan.source.value, result.pages_examined,
                    len(result.candidates), saved,
                )
                if branch.plan.source == ResearchSource.TWO_GIS:
                    catalog_links_by_item[branch.plan.item_index] = _catalog_links(
                        result.candidates
                    )
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
        items = {index: row["id"] for index, row in enumerate(item_rows)}
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
            contact_evidence_id = next(
                (evidence_id for evidence, evidence_id in zip(
                    candidate.evidence, evidence_ids, strict=True
                ) if EvidenceClaim.CONTACT in evidence.claims),
                primary_evidence_id,
            )
            valid_contacts = []
            invalid_contacts = []
            for contact in candidate.contacts:
                try:
                    normalize_contact(contact.kind.value, contact.value)
                except ValueError:
                    invalid_contacts.append(contact)
                else:
                    valid_contacts.append(contact)
            existing_offer = None
            if candidate.source == ResearchSource.OLX:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                    f"olx-offer:{branch.request_item_id}:{candidate.external_id}",
                )
                existing_offer = await connection.fetchrow(
                    """
                    SELECT o.id,o.supplier_id FROM offers o
                    JOIN offer_observations observation ON observation.offer_id=o.id
                    JOIN source_evidence evidence ON evidence.id=observation.source_evidence_id
                    WHERE o.request_item_id=$1 AND evidence.external_id=$2
                      AND evidence.source_type='marketplace'
                    ORDER BY o.created_at LIMIT 1
                    """,
                    branch.request_item_id, candidate.external_id,
                )
            elif candidate.source == ResearchSource.TWO_GIS:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                    f"2gis-offer:{branch.request_item_id}:{candidate.external_id}",
                )
                existing_offer = await connection.fetchrow(
                    """
                    SELECT o.id,o.supplier_id FROM offers o
                    WHERE o.request_item_id=$1 AND EXISTS (
                        SELECT 1 FROM offer_observations observation
                        JOIN source_evidence evidence
                          ON evidence.id=observation.source_evidence_id
                        WHERE observation.offer_id=o.id
                          AND evidence.external_id=$2
                          AND evidence.source_type='2gis'
                    )
                    ORDER BY o.created_at,o.id LIMIT 1
                    """,
                    branch.request_item_id,
                    candidate.external_id,
                )
            if existing_offer is not None:
                supplier_id = existing_offer["supplier_id"]
            else:
                identity_type, identity_value = _identity(candidate)
                supplier_input = SupplierInput(
                    display_name=candidate.name,
                    identity_type=identity_type,
                    identity_value=identity_value,
                    source_evidence_id=primary_evidence_id,
                    confidence=_confidence(candidate, "name", "0.7"),
                )
                if candidate.source == ResearchSource.OLX and valid_contacts:
                    supplier = await upsert_olx_supplier_by_contact(
                        connection,
                        supplier_input,
                        [(contact.kind.value, contact.value) for contact in valid_contacts],
                    )
                else:
                    supplier = await upsert_supplier(connection, supplier_input)
                supplier_id = supplier.id
            for contact in valid_contacts:
                try:
                    await upsert_supplier_contact(
                        connection,
                        supplier_id,
                        SupplierContactInput(
                            contact_type=contact.kind.value,
                            display_value=contact.value,
                            source_evidence_id=contact_evidence_id,
                            is_whatsapp_confirmed=(contact.kind == ContactKind.WHATSAPP),
                            confidence=Decimal(str(contact.confidence)),
                        ),
                    )
                except SupplierIdentityConflictError:
                    normalized_contact = normalize_contact(contact.kind.value, contact.value)
                    contact_fingerprint = _digest(
                        {"kind": contact.kind.value, "normalized": normalized_contact}
                    )
                    await connection.execute(
                        """INSERT INTO workflow_events(
                               case_id,request_item_id,event_type,actor_type,actor_id,
                               idempotency_key,data
                           ) SELECT item.case_id,item.id,
                               'supplier_contact_identity_conflict','worker',
                               'research-persistence',$2,$3::jsonb
                             FROM request_items item WHERE item.id=$1
                           ON CONFLICT (idempotency_key) DO NOTHING""",
                        branch.request_item_id,
                        stable_key(
                            "research-contact-identity-review",
                            branch.id,
                            candidate.source.value,
                            candidate.external_id or "",
                            supplier_id,
                            contact_evidence_id,
                            contact.kind.value,
                            contact_fingerprint,
                        ),
                        {
                            "outcome": "contact_needs_review",
                            "research_query_id": str(branch.id),
                            "supplier_id": str(supplier_id),
                            "source_evidence_id": str(contact_evidence_id),
                            "contact_kind": contact.kind.value,
                            "contact_fingerprint": contact_fingerprint,
                        },
                    )
            for contact in invalid_contacts:
                contact_fingerprint = _digest(
                    {"kind": contact.kind.value, "value": contact.value}
                )
                await connection.execute(
                    """INSERT INTO workflow_events(
                           case_id,request_item_id,event_type,actor_type,actor_id,
                           idempotency_key,data
                       ) SELECT item.case_id,item.id,
                           'supplier_contact_invalid','worker','research-persistence',$2,$3::jsonb
                         FROM request_items item WHERE item.id=$1
                       ON CONFLICT (idempotency_key) DO NOTHING""",
                    branch.request_item_id,
                    stable_key(
                        "research-contact-invalid-review",
                        branch.id,
                        candidate.source.value,
                        candidate.external_id or "",
                        supplier_id,
                        contact_evidence_id,
                        contact.kind.value,
                        contact_fingerprint,
                    ),
                    {
                        "outcome": "contact_needs_review",
                        "research_query_id": str(branch.id),
                        "supplier_id": str(supplier_id),
                        "source_evidence_id": str(contact_evidence_id),
                        "contact_kind": contact.kind.value,
                        "contact_fingerprint": contact_fingerprint,
                    },
                )
            location_id: UUID | None = None
            if (
                candidate.address is not None
                or candidate.point is not None
                or candidate.external_id
            ):
                location = await upsert_supplier_location(
                    connection,
                    supplier_id,
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
                        provider_place_id=("" if candidate.source == ResearchSource.OLX
                                           else candidate.external_id or ""),
                        confidence=_confidence(candidate, "address", "0.7"),
                    ),
                )
                location_id = location.id
            if existing_offer is None:
                offer = await upsert_offer(
                    connection,
                    OfferInput(
                        request_item_id=branch.request_item_id,
                        supplier_id=supplier_id,
                        supplier_location_id=location_id,
                        idempotency_key=_offer_key(
                            branch.id, supplier_id,
                            candidate.external_id if candidate.source == ResearchSource.OLX
                            else None,
                        ),
                        supplier_product_name=candidate.product_name or "",
                        status="lead",
                    ),
                )
                offer_id = offer.id
            else:
                offer_id = existing_offer["id"]
            for evidence, evidence_id in zip(candidate.evidence, evidence_ids, strict=True):
                observation_material = {
                    "offer_id": str(offer_id),
                    "evidence_id": str(evidence_id),
                }
                observation_key = (
                    f"research-observation:{_digest(observation_material)}"
                )
                await upsert_offer_observation(
                    connection,
                    OfferObservationInput(
                        offer_id=offer_id,
                        source_evidence_id=evidence_id,
                        observed_at=evidence.observed_at,
                        observed_by="research-executor",
                        source_type=_SOURCE_TYPE[candidate.source],
                        price_amount=evidence.price_amount,
                        currency=evidence.currency.upper() if evidence.currency else None,
                        price_unit=evidence.price_unit,
                        pack_qty=evidence.pack_qty,
                        moq=evidence.moq,
                        order_multiple=evidence.order_multiple,
                        available_qty=evidence.available_qty,
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
        async with connection.transaction():
            target_case_status = "evaluating"
            if status == "failed":
                has_report = await connection.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM report_artifacts
                       WHERE case_id=$1 AND is_current)""",
                    case_id,
                )
                target_case_status = "report_ready" if has_report else "ready"
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
                (status == "failed" and case["status"] == target_case_status)
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
