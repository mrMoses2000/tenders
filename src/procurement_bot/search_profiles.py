from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg
from pydantic import Field, field_validator, model_validator

from procurement_bot.intake import StrictModel
from procurement_bot.providers.agy import StructuredExtractor
from procurement_bot.research import (
    LocalityConstraint,
    ResearchSource,
    SourcePlan,
    assert_no_private_price,
)
from procurement_bot.research_scope import load_effective_scope

PROFILE_MODEL_VERSION = "search-profile-v1"
PROFILE_PROMPT_VERSION = "search-profile-prompt-v1"
_CITYWIDE_BREADTH_PROMPT_SUFFIX = "+authorized-citywide-breadth-v2"
MAX_INTENTS_PER_SOURCE = 6
MAX_PROFILE_CATEGORIES = 6

_SOURCE_PROVIDERS = {
    ResearchSource.OWN_DATABASE: "own_database",
    ResearchSource.TWO_GIS: "2gis_web",
    ResearchSource.OLX: "marketplace",
    ResearchSource.WEB: "web",
}


class SearchIntent(StrictModel):
    """One short semantic query variant for a provider and seller category."""

    source: ResearchSource
    intent_type: Literal["product", "business"]
    category: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)
    uncertainty: float = Field(ge=0, le=1)

    @field_validator("category", "query", "rationale")
    @classmethod
    def safe_search_text(cls, value: str) -> str:
        assert_no_private_price(value)
        return value


class SearchProfile(StrictModel):
    """Strict AGY proposal; it contains discovery guidance, never a tender price."""

    product_name: str = Field(min_length=1, max_length=300)
    critical_parameters: list[str] = Field(default_factory=list, max_length=12)
    analogs_allowed: bool | None = None
    categories: list[str] = Field(min_length=1, max_length=MAX_PROFILE_CATEGORIES)
    intents: list[SearchIntent] = Field(min_length=1, max_length=24)
    uncertainty: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("product_name")
    @classmethod
    def safe_product_name(cls, value: str) -> str:
        assert_no_private_price(value)
        return value

    @field_validator("critical_parameters", "categories", "uncertainty")
    @classmethod
    def safe_list_text(cls, values: list[str]) -> list[str]:
        for value in values:
            assert_no_private_price(value)
        return values

    @model_validator(mode="after")
    def bounded_unique_categories_and_sources(self) -> SearchProfile:
        category_keys = [category.casefold() for category in self.categories]
        if len(category_keys) != len(set(category_keys)):
            raise ValueError("categories must be unique")
        if len({intent.category.casefold() for intent in self.intents}) > MAX_PROFILE_CATEGORIES:
            raise ValueError("intents may reference at most six categories")
        source_counts = Counter(intent.source for intent in self.intents)
        if any(count > MAX_INTENTS_PER_SOURCE for count in source_counts.values()):
            raise ValueError("a search profile may contain at most six intents per source")
        if not {intent.category.casefold() for intent in self.intents}.issubset(set(category_keys)):
            raise ValueError("every intent category must be declared by the profile")
        return self


class SearchIntentState(StrictModel):
    """Durable execution state kept separate from the immutable semantic intent."""

    intent: SearchIntent
    ordinal: int = Field(ge=0, le=23)
    scope_revision: int = Field(ge=1)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    cursor_state: dict[str, Any] = Field(default_factory=lambda: {"current": None, "seen": []})
    results: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str = Field(default="", max_length=500)

    @field_validator("limitations", "stop_reason")
    @classmethod
    def safe_execution_text(cls, value: Any) -> Any:
        values = value if isinstance(value, list) else [value]
        for text in values:
            if text:
                assert_no_private_price(text)
        return value


@dataclass(frozen=True, slots=True)
class SearchItemSnapshot:
    research_run_id: UUID
    case_id: UUID
    request_item_id: UUID
    item_index: int
    snapshot_sha256: str
    plan_sha256: str
    context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PersistedSearchProfile:
    id: UUID
    research_run_id: UUID
    request_item_id: UUID
    profile_version: int
    snapshot_sha256: str
    intent_ids: tuple[UUID, ...]
    created: bool


@dataclass(frozen=True, slots=True)
class EnsuredSearchProfile:
    item_id: UUID
    profile: PersistedSearchProfile
    created: bool


class SearchProfileConflict(RuntimeError):
    """A frozen item changed or an idempotency key represents different content."""


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
        raise ValueError("search profile context must contain finite JSON values") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _snapshot_digest(
    research_run_id: UUID,
    case_id: UUID,
    plan_sha256: str,
    context: Mapping[str, Any],
) -> str:
    return _digest(
        {
            "run_id": str(research_run_id),
            "case_id": str(case_id),
            "plan_sha256": plan_sha256,
            "context": dict(context),
        }
    )


def _assert_snapshot_intact(snapshot: SearchItemSnapshot) -> None:
    if (
        _snapshot_digest(
            snapshot.research_run_id,
            snapshot.case_id,
            snapshot.plan_sha256,
            snapshot.context,
        )
        != snapshot.snapshot_sha256
    ):
        raise SearchProfileConflict("item snapshot was modified after it was loaded")


def _decode_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SearchProfileConflict(f"persisted {field} is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise SearchProfileConflict(f"persisted {field} must be an object")
    return dict(value)


def _plan_and_branches(search_scope: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scope = _decode_object(search_scope, "run search_scope")
    plan = _decode_object(scope.get("plan"), "run plan")
    branches = plan.get("source_plans")
    if not isinstance(branches, list) or not branches:
        raise SearchProfileConflict("frozen source plan has no source_plans")
    return plan, [_decode_object(branch, "source plan branch") for branch in branches]


async def load_search_item_snapshot(
    connection: asyncpg.Connection,
    research_run_id: UUID,
    request_item_id: UUID,
) -> SearchItemSnapshot:
    """Load current item facts with the immutable run locality used by its plan."""

    row = await connection.fetchrow(
        """
        SELECT run.id AS research_run_id, run.case_id, run.search_scope,
               item.id AS request_item_id, item.line_number, item.name,
               item.specification_text, item.quantity, item.unit,
               item.analogs_allowed, procurement_case.city,
               procurement_case.search_area_text
        FROM research_runs run
        JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
        JOIN research_item_groups grp ON grp.research_run_id=run.id
        JOIN request_items item ON item.id=grp.request_item_id
        WHERE run.id=$1 AND item.id=$2
        """,
        research_run_id,
        request_item_id,
    )
    if row is None:
        raise SearchProfileConflict("research run/item pair does not exist")
    requirement_rows = await connection.fetch(
        """
        SELECT requirement_key, operator, expected_value, is_hard, source_pointer
        FROM item_requirements WHERE request_item_id=$1
        ORDER BY requirement_key, source_pointer, id
        """,
        request_item_id,
    )
    requirements = [
        {
            "name": requirement["requirement_key"],
            "operator": requirement["operator"],
            "expected_value": requirement["expected_value"],
            "hard_requirement": requirement["is_hard"],
            "source_reference": requirement["source_pointer"],
        }
        for requirement in requirement_rows
    ]
    _, branches = _plan_and_branches(row["search_scope"])
    item_index = int(row["line_number"]) - 1
    item_branches = [branch for branch in branches if branch.get("item_index") == item_index]
    if not item_branches:
        raise SearchProfileConflict("item has no matching branch in the frozen source plan")
    # Use the frozen locality block. Case text is retained only as a check/input
    # for historical plans whose locality block contains an empty raw area.
    localities = [
        _decode_object(branch.get("locality"), "branch locality") for branch in item_branches
    ]
    locality = localities[0]
    if any(candidate != locality for candidate in localities[1:]):
        raise SearchProfileConflict("frozen source plan has inconsistent item locality")
    frozen_city = str(locality.get("city") or "").strip().casefold()
    case_city = str(row["city"] or "").strip().casefold()
    frozen_area = str(locality.get("raw_area_text") or "").strip().casefold()
    case_area = str(row["search_area_text"] or "").strip().casefold()
    if frozen_city and case_city and frozen_city != case_city:
        raise SearchProfileConflict("case city differs from the frozen source plan")
    if frozen_area != case_area:
        raise SearchProfileConflict("case locality differs from the frozen source plan")
    context: dict[str, Any] = {
        "item_id": str(row["request_item_id"]),
        "item_index": item_index,
        "name": row["name"],
        "quantity": str(row["quantity"]) if row["quantity"] is not None else None,
        "unit": row["unit"],
        "specification": row["specification_text"],
        "requirements": requirements,
        "analogs_allowed": row["analogs_allowed"],
        "city": locality.get("city") or row["city"],
        "case_city": row["city"],
        "case_search_area_text": row["search_area_text"],
        "locality": locality,
    }
    effective = await load_effective_scope(
        connection, research_run_id, LocalityConstraint.model_validate(locality)
    )
    if effective.revision is not None:
        context["locality"] = effective.locality.model_dump(mode="json")
        context["scope_revision"] = {
            "revision": effective.revision.revision,
            "revision_sha256": effective.revision.revision_sha256,
            "base_plan_sha256": effective.revision.base_plan_sha256,
            "preferred_area_text": effective.preferred_area_text,
        }
    # No customer/tender price column is selected. Apply the privacy guard to
    # the exact object handed to the model as a second boundary check.
    assert_no_private_price(context)
    scope = _decode_object(row["search_scope"], "run search_scope")
    plan_sha256 = str(scope.get("plan_sha256") or "")
    snapshot_sha256 = _snapshot_digest(research_run_id, row["case_id"], plan_sha256, context)
    return SearchItemSnapshot(
        research_run_id=research_run_id,
        case_id=row["case_id"],
        request_item_id=request_item_id,
        item_index=item_index,
        snapshot_sha256=snapshot_sha256,
        plan_sha256=plan_sha256,
        context=context,
    )


async def propose_search_profile(
    extractor: StructuredExtractor,
    snapshot: SearchItemSnapshot,
    *,
    model_version: str = PROFILE_MODEL_VERSION,
    prompt_version: str = PROFILE_PROMPT_VERSION,
) -> SearchProfile:
    """Call the structured model without a database transaction or tender prices."""

    _assert_snapshot_intact(snapshot)
    assert_no_private_price(snapshot.context)
    scope_revision = _authorized_scope_revision(snapshot.context)
    effective_prompt_version = _effective_prompt_version(prompt_version, scope_revision)
    prompt = (
        "Propose a semantic supplier-search profile for this product. Return concise "
        "categories and several source-specific product or business query variants. "
        "Use the supplied city/locality and item facts only. Mark uncertainty explicitly. "
        "Do not add prices, budgets, or copy the full specification into the profile."
    )
    model_context = dict(snapshot.context)
    if scope_revision is not None:
        # Pass only the fields needed to execute the authorized scope revision.
        # Authorization identities/references remain in the hashed DB snapshot,
        # but are not useful inputs for supplier-search generation.
        model_context.pop("scope_revision", None)
        model_context["authorized_search_scope_revision"] = {
            "revision": scope_revision["revision"],
            "revision_sha256": scope_revision["revision_sha256"],
            "preferred_area_text": scope_revision["preferred_area_text"],
        }
        prompt += (
            " An authorized scope revision allows citywide searching. Treat its "
            "preferred area as a search-order hint only: for 2GIS create up to three "
            "useful business-category intents in model order, with preferred-area "
            "queries first and whole-city queries after them. Search the whole city; "
            "do not require an address inside the preferred area or invent seller "
            "affiliation. Keep 2GIS intents business-only. Other providers may retain "
            "product queries; concise OLX product-detail queries are allowed without "
            "requiring an article or SKU code."
        )
    result = await extractor.extract(
        prompt,
        SearchProfile,
        context={
            "item_snapshot": model_context,
            "model_version": model_version,
            "prompt_version": effective_prompt_version,
            "frozen_snapshot_sha256": snapshot.snapshot_sha256,
        },
        spec_sha256=snapshot.snapshot_sha256,
    )
    proposal = SearchProfile.model_validate(result)
    if scope_revision is None:
        return proposal
    return with_citywide_search_breadth(proposal, snapshot.context)


def _authorized_scope_revision(context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a well-shaped authorized revision from the owner-loaded snapshot."""

    value = context.get("scope_revision")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("authorized scope revision must be an object")
    revision = value.get("revision")
    revision_hash = value.get("revision_sha256")
    preferred_area = value.get("preferred_area_text")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(revision_hash, str)
        or len(revision_hash) != 64
        or any(character not in "0123456789abcdef" for character in revision_hash)
        or not isinstance(preferred_area, str)
        or not preferred_area.strip()
    ):
        raise ValueError("authorized scope revision is incomplete")
    locality = context.get("locality")
    city = context.get("city")
    if isinstance(locality, Mapping):
        city = locality.get("city") or city
    if not isinstance(city, str) or not city.strip():
        raise ValueError("authorized citywide search requires a city")
    return {
        "revision": revision,
        "revision_sha256": revision_hash,
        "preferred_area_text": preferred_area.strip(),
        "city": city.strip(),
    }


def _effective_prompt_version(prompt_version: str, scope_revision: Mapping[str, Any] | None) -> str:
    if scope_revision is None or prompt_version.endswith(_CITYWIDE_BREADTH_PROMPT_SUFFIX):
        return prompt_version
    return f"{prompt_version}{_CITYWIDE_BREADTH_PROMPT_SUFFIX}"


def with_citywide_search_breadth(
    profile: SearchProfile,
    snapshot_context: Mapping[str, Any],
) -> SearchProfile:
    """Expand only an owner-authorized revision into priority then citywide 2GIS intents.

    Existing profiles and snapshots without ``scope_revision`` pass through
    unchanged. The area is a query preference, while all revised 2GIS intents
    remain citywide business searches.
    """

    revision = _authorized_scope_revision(snapshot_context)
    if revision is None:
        return profile

    categories_in_model_order: list[str] = []
    seen_categories: set[str] = set()
    for intent in profile.intents:
        if intent.source != ResearchSource.TWO_GIS or intent.intent_type != "business":
            continue
        key = intent.category.strip().casefold()
        if key and key not in seen_categories:
            seen_categories.add(key)
            categories_in_model_order.append(intent.category.strip())
        if len(categories_in_model_order) == 3:
            break

    retained = [intent for intent in profile.intents if intent.source != ResearchSource.TWO_GIS]
    priority_intents: list[SearchIntent] = []
    citywide_intents: list[SearchIntent] = []
    seen_queries: set[str] = set()
    for category in categories_in_model_order:
        base = next(
            intent
            for intent in profile.intents
            if intent.source == ResearchSource.TWO_GIS
            and intent.intent_type == "business"
            and intent.category.strip().casefold() == category.casefold()
        )
        for query, area_label, destination in (
            (
                f"{category} {revision['preferred_area_text']} {revision['city']}",
                revision["preferred_area_text"],
                priority_intents,
            ),
            (f"{category} {revision['city']}", revision["city"], citywide_intents),
        ):
            query = " ".join(query.split())[:500]
            key = query.casefold()
            if key in seen_queries:
                continue
            seen_queries.add(key)
            destination.append(
                SearchIntent(
                    source=ResearchSource.TWO_GIS,
                    intent_type="business",
                    category=base.category,
                    query=query,
                    rationale=(
                        f"Citywide supplier discovery; prioritize {area_label} as a "
                        "search hint without requiring seller presence there."
                    ),
                    uncertainty=base.uncertainty,
                )
            )
    intents = priority_intents + citywide_intents + retained
    if not intents:
        raise ValueError("authorized citywide profile has no usable supplier intents")
    return SearchProfile.model_validate(
        profile.model_copy(update={"intents": intents}).model_dump(mode="python")
    )


def _profile_idempotency_key(
    snapshot: SearchItemSnapshot,
    model_version: str,
    prompt_version: str,
) -> tuple[str, str]:
    context_hash = _digest(
        {
            "snapshot_sha256": snapshot.snapshot_sha256,
            "model_version": model_version,
            "prompt_version": prompt_version,
        }
    )
    key = f"search-profile:{snapshot.research_run_id}:{snapshot.request_item_id}:{context_hash}"
    return context_hash, key


def _intent_payload(intent: SearchIntent, ordinal: int, profile_version: int) -> dict[str, Any]:
    assert_no_private_price(intent.model_dump(mode="json"))
    return {
        "source": _SOURCE_PROVIDERS[intent.source],
        "intent_type": intent.intent_type,
        "category": intent.category,
        "query": intent.query,
        "rationale": intent.rationale,
        "uncertainty": intent.uncertainty,
        "ordinal": ordinal,
        "scope_revision": profile_version,
        "limitations": [],
        "cursor_state": {"current": None, "seen": []},
        "results": {},
        "stop_reason": "",
    }


async def persist_search_profile(
    connection: asyncpg.Connection,
    snapshot: SearchItemSnapshot,
    profile: SearchProfile,
    *,
    model_version: str = PROFILE_MODEL_VERSION,
    prompt_version: str = PROFILE_PROMPT_VERSION,
) -> PersistedSearchProfile:
    """Revalidate and persist the proposal in a short transaction."""

    _assert_snapshot_intact(snapshot)
    profile = SearchProfile.model_validate(profile)
    assert_no_private_price(profile.model_dump(mode="json"))
    prompt_version = _effective_prompt_version(
        prompt_version, _authorized_scope_revision(snapshot.context)
    )
    context_hash, idempotency_key = _profile_idempotency_key(
        snapshot, model_version, prompt_version
    )
    profile_payload = profile.model_dump(mode="json")
    async with connection.transaction():
        # Lock the run to serialize profile version allocation. The model call has
        # already completed before this transaction starts.
        run_row = await connection.fetchrow(
            "SELECT id FROM research_runs WHERE id=$1 FOR UPDATE",
            snapshot.research_run_id,
        )
        if run_row is None:
            raise SearchProfileConflict("research run no longer exists")
        current = await load_search_item_snapshot(
            connection, snapshot.research_run_id, snapshot.request_item_id
        )
        if current.snapshot_sha256 != snapshot.snapshot_sha256:
            raise SearchProfileConflict("item requirements or locality changed after proposal")
        existing = await connection.fetchrow(
            "SELECT * FROM research_search_profiles WHERE idempotency_key=$1",
            idempotency_key,
        )
        created = existing is None
        if existing is None:
            version = await connection.fetchval(
                """SELECT COALESCE(MAX(profile_version),0)+1
                FROM research_search_profiles
                WHERE research_run_id=$1 AND request_item_id=$2""",
                snapshot.research_run_id,
                snapshot.request_item_id,
            )
            profile_id = uuid4()
            existing = await connection.fetchrow(
                """
                INSERT INTO research_search_profiles(
                    id,research_run_id,request_item_id,profile_version,
                    snapshot_sha256,context_sha256,plan_sha256,model_version,
                    prompt_version,profile,idempotency_key
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11)
                ON CONFLICT (idempotency_key) DO NOTHING RETURNING *
                """,
                profile_id,
                snapshot.research_run_id,
                snapshot.request_item_id,
                version,
                snapshot.snapshot_sha256,
                context_hash,
                snapshot.plan_sha256,
                model_version,
                prompt_version,
                profile_payload,
                idempotency_key,
            )
            if existing is None:
                existing = await connection.fetchrow(
                    "SELECT * FROM research_search_profiles WHERE idempotency_key=$1",
                    idempotency_key,
                )
                created = False
        if existing is None:
            raise RuntimeError("search profile disappeared after idempotent insert")
        persisted_profile = _decode_object(existing["profile"], "profile")
        if (
            existing["snapshot_sha256"] != snapshot.snapshot_sha256
            or existing["context_sha256"] != context_hash
            or persisted_profile != profile_payload
        ):
            raise SearchProfileConflict("profile idempotency key has different immutable content")

        intent_ids: list[UUID] = []
        for ordinal, intent in enumerate(profile.intents):
            payload = _intent_payload(intent, ordinal, existing["profile_version"])
            intent_hash = _digest(payload)
            intent_key = f"search-intent:{existing['id']}:{ordinal}:{intent_hash}"
            row = await connection.fetchrow(
                """
                INSERT INTO research_search_intents(
                    id,profile_id,research_run_id,request_item_id,intent_version,
                    ordinal,provider,intent_type,category,query_text,rationale,
                    uncertainty,scope_revision,intent_sha256,limitations,cursor_state,
                    results,stop_reason,idempotency_key
                ) VALUES (
                    $1,$2,$3,$4,1,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                    $14::jsonb,$15::jsonb,$16::jsonb,$17,$18
                ) ON CONFLICT (idempotency_key) DO NOTHING RETURNING *
                """,
                uuid4(),
                existing["id"],
                snapshot.research_run_id,
                snapshot.request_item_id,
                ordinal,
                payload["source"],
                payload["intent_type"],
                payload["category"],
                payload["query"],
                payload["rationale"],
                payload["uncertainty"],
                payload["scope_revision"],
                intent_hash,
                payload["limitations"],
                payload["cursor_state"],
                payload["results"],
                payload["stop_reason"],
                intent_key,
            )
            if row is None:
                row = await connection.fetchrow(
                    "SELECT * FROM research_search_intents WHERE idempotency_key=$1",
                    intent_key,
                )
            if row is None:
                raise RuntimeError("search intent disappeared after idempotent insert")
            if row["intent_sha256"] != intent_hash:
                raise SearchProfileConflict("intent idempotency key has different content")
            intent_ids.append(row["id"])
    return PersistedSearchProfile(
        id=existing["id"],
        research_run_id=snapshot.research_run_id,
        request_item_id=snapshot.request_item_id,
        profile_version=existing["profile_version"],
        snapshot_sha256=snapshot.snapshot_sha256,
        intent_ids=tuple(intent_ids),
        created=created,
    )


async def ensure_search_profiles(
    pool: asyncpg.Pool,
    extractor: StructuredExtractor,
    research_run_id: UUID,
    request_item_ids: list[UUID],
    *,
    model_version: str = PROFILE_MODEL_VERSION,
    prompt_version: str = PROFILE_PROMPT_VERSION,
) -> list[EnsuredSearchProfile]:
    """Ensure profiles for selected items without holding a DB connection in AGY."""

    ensured: list[EnsuredSearchProfile] = []
    for item_id in dict.fromkeys(request_item_ids):
        async with pool.acquire() as connection:
            snapshot = await load_search_item_snapshot(connection, research_run_id, item_id)
            effective_prompt_version = _effective_prompt_version(
                prompt_version, _authorized_scope_revision(snapshot.context)
            )
            _, key = _profile_idempotency_key(
                snapshot, model_version, effective_prompt_version
            )
            existing = await connection.fetchrow(
                "SELECT id,profile_version,snapshot_sha256 FROM research_search_profiles "
                "WHERE idempotency_key=$1",
                key,
            )
            if existing is not None:
                intent_rows = await connection.fetch(
                    "SELECT id FROM research_search_intents WHERE profile_id=$1 ORDER BY ordinal",
                    existing["id"],
                )
                saved = PersistedSearchProfile(
                    id=existing["id"],
                    research_run_id=research_run_id,
                    request_item_id=item_id,
                    profile_version=existing["profile_version"],
                    snapshot_sha256=existing["snapshot_sha256"],
                    intent_ids=tuple(row["id"] for row in intent_rows),
                    created=False,
                )
                ensured.append(EnsuredSearchProfile(item_id, saved, False))
                continue

        # The pool connection has been released before the external model call.
        proposal = await propose_search_profile(
            extractor,
            snapshot,
            model_version=model_version,
            prompt_version=effective_prompt_version,
        )
        async with pool.acquire() as connection:
            saved = await persist_search_profile(
                connection,
                snapshot,
                proposal,
                model_version=model_version,
                prompt_version=effective_prompt_version,
            )
        ensured.append(EnsuredSearchProfile(item_id, saved, saved.created))
    return ensured


async def load_search_intent_plan(
    connection: asyncpg.Connection,
    plan: SourcePlan,
    query_id: UUID,
    excluded_ids: list[str],
    cursor: str | None,
    intent_ordinal: int = 0,
) -> SourcePlan | None:
    """Return one saved intent as a runtime copy; leave durable progression to caller.

    ``intent_ordinal`` is selected by durable caller state. ``cursor`` remains the
    provider pagination URL and is copied without being overloaded as profile state.
    A caller with no saved profile gets ``None`` and keeps using the legacy query.
    """
    if not isinstance(plan, SourcePlan):
        raise TypeError("plan must be a SourcePlan")
    if intent_ordinal < 0:
        raise ValueError("intent_ordinal must be non-negative")
    provider = _SOURCE_PROVIDERS[plan.source]
    rows = await connection.fetch(
        """
        SELECT intent.ordinal,intent.category,intent.query_text,intent.provider,
               intent.scope_revision,profile.snapshot_sha256,profile.id AS profile_id
        FROM research_queries query
        JOIN research_runs run ON run.id=query.research_run_id
        JOIN request_items item
          ON item.case_id=run.case_id
         AND item.line_number = ((query.parameters->>'item_index')::integer + 1)
        JOIN research_search_profiles profile
          ON profile.research_run_id=query.research_run_id
         AND profile.request_item_id=item.id
        JOIN LATERAL (
            SELECT newest.id
            FROM research_search_profiles newest
            WHERE newest.research_run_id=profile.research_run_id
              AND newest.request_item_id=profile.request_item_id
            ORDER BY newest.profile_version DESC
            LIMIT 1
        ) active_profile ON active_profile.id=profile.id
        JOIN research_search_intents intent ON intent.profile_id=profile.id
        WHERE query.id=$1 AND query.provider=$2
          AND intent.provider=$2 AND intent.ordinal >= $3
        ORDER BY intent.ordinal
        """,
        query_id,
        provider,
        intent_ordinal,
    )
    if not rows:
        return None
    # Ensure that a changed item does not silently reuse an old model proposal.
    query_context = await connection.fetchrow(
        """SELECT item.id AS request_item_id,query.research_run_id
        FROM research_queries query
        JOIN research_runs run ON run.id=query.research_run_id
        JOIN request_items item
          ON item.case_id=run.case_id
         AND item.line_number = ((query.parameters->>'item_index')::integer + 1)
        WHERE query.id=$1""",
        query_id,
    )
    if query_context is None:
        return None
    snapshot = await load_search_item_snapshot(
        connection, query_context["research_run_id"], query_context["request_item_id"]
    )
    row = rows[0]
    if row["snapshot_sha256"] != snapshot.snapshot_sha256:
        return None
    return plan.model_copy(
        update={
            "execution_intent_ordinal": int(row["ordinal"]),
            "query_terms": [
                row["category"] if plan.source == ResearchSource.TWO_GIS else row["query_text"]
            ],
            "rendered_query": row["query_text"],
            "excluded_external_ids": list(excluded_ids),
            "search_cursor": cursor,
        }
    )
