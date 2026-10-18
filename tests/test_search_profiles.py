from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from types import TracebackType
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.providers.agy import FakeExtractor
from procurement_bot.research import ResearchPlan, ResearchSource, build_research_plan
from procurement_bot.research_repository import recompute_plan_sha256
from procurement_bot.search_profiles import (
    SearchProfile,
    SearchProfileConflict,
    _snapshot_digest,
    load_search_intent_plan,
    load_search_item_snapshot,
    persist_search_profile,
    propose_search_profile,
    with_citywide_search_breadth,
)


def _profile() -> SearchProfile:
    return SearchProfile.model_validate(
        {
            "product_name": "Кисточка 80 мм",
            "critical_parameters": ["ширина 80 мм"],
            "analogs_allowed": True,
            "categories": ["малярный инструмент", "строительный магазин"],
            "intents": [
                {
                    "source": "web",
                    "intent_type": "product",
                    "category": "малярный инструмент",
                    "query": "кисточка малярная 80 мм купить Алматы",
                    "rationale": "Товарный запрос сохраняет ширину.",
                    "uncertainty": 0.15,
                },
                {
                    "source": "2gis",
                    "intent_type": "business",
                    "category": "строительный магазин",
                    "query": "магазин малярного инструмента",
                    "rationale": "Категория помогает найти профильных продавцов.",
                    "uncertainty": 0.25,
                },
            ],
            "uncertainty": ["Уточнить материал щетины, если он критичен."],
        }
    )


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        plan = build_research_plan(
            ParsedProcurementRequest(
                city="Алматы",
                search_scope=SearchScope.CITYWIDE,
                items=[ProcurementItem(name="Кисточка 80 мм", quantity=10, unit="шт")],
            )
        )
        self.run_id = uuid4()
        self.case_id = uuid4()
        self.item_id = uuid4()
        self.requirements: list[dict[str, Any]] = [
            {
                "requirement_key": "ширина",
                "operator": "equals",
                "expected_value": {"value": "80 мм"},
                "is_hard": True,
                "source_pointer": "строка 1",
                "id": uuid4(),
            }
        ]
        self.item = {
            "research_run_id": self.run_id,
            "case_id": self.case_id,
            "search_scope": {
                "plan_sha256": plan.plan_sha256,
                "plan": plan.model_dump(mode="json", exclude={"plan_sha256"}),
            },
            "request_item_id": self.item_id,
            "line_number": 1,
            "name": "Кисточка 80 мм",
            "specification_text": "Кисточка малярная, ширина 80 мм",
            "quantity": 10,
            "unit": "шт",
            "analogs_allowed": True,
            "city": "Алматы",
            "search_area_text": "",
        }
        self.profiles: dict[str, dict[str, Any]] = {}
        self.intents: dict[str, dict[str, Any]] = {}
        self.search_intent_rows: list[dict[str, Any]] = []

    def transaction(self) -> AbstractAsyncContextManager[None]:
        return _Transaction()

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM research_scope_revisions" in sql:
            return None
        if "FROM research_runs run" in sql and "JOIN procurement_cases" in sql:
            if args == (self.run_id, self.item_id):
                return dict(self.item)
            return None
        if "SELECT item.id AS request_item_id,query.research_run_id" in sql:
            return {
                "request_item_id": self.item_id,
                "research_run_id": self.run_id,
            }
        if sql.startswith("SELECT id FROM research_runs"):
            return {"id": self.run_id} if args[0] == self.run_id else None
        if "FROM research_search_profiles WHERE idempotency_key" in sql:
            return self.profiles.get(args[0])
        if "INSERT INTO research_search_profiles" in sql:
            (
                profile_id,
                run_id,
                item_id,
                version,
                snapshot_hash,
                context_hash,
                plan_hash,
                model_version,
                prompt_version,
                payload,
                key,
            ) = args
            row = {
                "id": profile_id,
                "research_run_id": run_id,
                "request_item_id": item_id,
                "profile_version": version,
                "snapshot_sha256": snapshot_hash,
                "context_sha256": context_hash,
                "plan_sha256": plan_hash,
                "model_version": model_version,
                "prompt_version": prompt_version,
                "profile": json.loads(payload) if isinstance(payload, str) else payload,
                "idempotency_key": key,
            }
            self.profiles[key] = row
            return row
        if "INSERT INTO research_search_intents" in sql:
            (
                intent_id,
                profile_id,
                run_id,
                item_id,
                ordinal,
                provider,
                intent_type,
                category,
                query_text,
                rationale,
                uncertainty,
                scope_revision,
                intent_hash,
                limitations,
                cursor_state,
                results,
                stop_reason,
                key,
            ) = args
            row = {
                "id": intent_id,
                "profile_id": profile_id,
                "research_run_id": run_id,
                "request_item_id": item_id,
                "ordinal": ordinal,
                "provider": provider,
                "intent_type": intent_type,
                "category": category,
                "query_text": query_text,
                "rationale": rationale,
                "uncertainty": uncertainty,
                "scope_revision": scope_revision,
                "intent_sha256": intent_hash,
                "limitations": limitations,
                "cursor_state": cursor_state,
                "results": results,
                "stop_reason": stop_reason,
                "idempotency_key": key,
            }
            self.intents[key] = row
            return row
        if "FROM research_search_intents WHERE idempotency_key" in sql:
            return self.intents.get(args[0])
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM item_requirements" in sql:
            return list(self.requirements)
        if "FROM research_queries query" in sql:
            if len(args) == 3:
                assert "item.case_id=run.case_id" in sql
                return [
                    row
                    for row in self.search_intent_rows
                    if row["research_run_id"] == self.run_id
                    and row["request_item_id"] == self.item_id
                    and row["ordinal"] >= args[2]
                ]
            return []
        raise AssertionError(f"unexpected SQL: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> int:
        if "MAX(profile_version)" in sql:
            versions = [
                row["profile_version"]
                for row in self.profiles.values()
                if row["research_run_id"] == args[0] and row["request_item_id"] == args[1]
            ]
            return max(versions, default=0) + 1
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_model_context_is_price_free_and_preserves_immutable_source_plan() -> None:
    connection = _Connection()
    snapshot = await load_search_item_snapshot(
        connection,
        connection.run_id,
        connection.item_id,  # type: ignore[arg-type]
    )
    original_plan = connection.item["search_scope"]["plan"]
    extractor = FakeExtractor(_profile())

    profile = await propose_search_profile(extractor, snapshot)

    assert profile == _profile()
    context = extractor.calls[0]["context"]
    assert "tender_unit_price_private" not in json.dumps(context)
    assert "budget" not in json.dumps(context).casefold()
    assert connection.item["search_scope"]["plan"] == original_plan
    assert len(profile.intents) == 2
    assert profile.intents[0].source == ResearchSource.WEB
    assert profile.intents[1].source == ResearchSource.TWO_GIS


def _authorized_citywide_context() -> dict[str, Any]:
    return {
        "city": "Алматы",
        "locality": {"city": "Алматы", "scope": "citywide", "raw_area_text": None},
        "scope_revision": {
            "revision": 2,
            "revision_sha256": "a" * 64,
            "preferred_area_text": "Северное Кольцо, рынки и внутренние улицы",
        },
    }


def test_citywide_breadth_requires_revision_and_prioritizes_area_before_city() -> None:
    profile = _profile()
    unchanged = with_citywide_search_breadth(profile, {"city": "Алматы"})
    assert unchanged is profile

    revised = with_citywide_search_breadth(profile, _authorized_citywide_context())

    gis_intents = [intent for intent in revised.intents if intent.source == ResearchSource.TWO_GIS]
    assert [intent.intent_type for intent in gis_intents] == ["business", "business"]
    assert [intent.query for intent in gis_intents] == [
        "строительный магазин Северное Кольцо, рынки и внутренние улицы Алматы",
        "строительный магазин Алматы",
    ]
    assert [intent.category for intent in gis_intents] == [
        "строительный магазин",
        "строительный магазин",
    ]
    assert "without requiring seller presence" in gis_intents[0].rationale
    assert [intent for intent in revised.intents if intent.source == ResearchSource.WEB] == [
        profile.intents[0]
    ]


def test_citywide_breadth_orders_categories_then_caps_at_three() -> None:
    payload = _profile().model_dump(mode="json")
    extra_categories = ["сантехника", "электротовары", "крепеж"]
    payload["categories"].extend(extra_categories)
    for category in extra_categories:
        payload["intents"].append(
            {
                "source": "2gis",
                "intent_type": "business",
                "category": category,
                "query": f"магазин {category}",
                "rationale": "Поиск профильного продавца.",
                "uncertainty": 0.3,
            }
        )
    profile = SearchProfile.model_validate(payload)

    revised = with_citywide_search_breadth(profile, _authorized_citywide_context())

    gis_queries = [
        intent.query for intent in revised.intents if intent.source == ResearchSource.TWO_GIS
    ]
    area = "Северное Кольцо, рынки и внутренние улицы"
    assert gis_queries[:3] == [
        f"строительный магазин {area} Алматы",
        f"сантехника {area} Алматы",
        f"электротовары {area} Алматы",
    ]
    assert gis_queries[3:] == [
        "строительный магазин Алматы",
        "сантехника Алматы",
        "электротовары Алматы",
    ]
    assert len(gis_queries) == 6


@pytest.mark.asyncio
async def test_authorized_scope_revision_prompts_and_expands_2gis_business_intents() -> None:
    connection = _Connection()
    loaded = await load_search_item_snapshot(
        connection,
        connection.run_id,
        connection.item_id,  # type: ignore[arg-type]
    )
    context = {**loaded.context, **_authorized_citywide_context()}
    snapshot = replace(
        loaded,
        context=context,
        snapshot_sha256=_snapshot_digest(
            loaded.research_run_id,
            loaded.case_id,
            loaded.plan_sha256,
            context,
        ),
    )
    extractor = FakeExtractor(_profile())

    profile = await propose_search_profile(extractor, snapshot)

    gis_queries = [
        intent.query for intent in profile.intents if intent.source == ResearchSource.TWO_GIS
    ]
    assert gis_queries == [
        "строительный магазин Северное Кольцо, рынки и внутренние улицы Алматы",
        "строительный магазин Алматы",
    ]
    call = extractor.calls[0]
    assert "preferred-area queries first" in call["text"]
    assert call["context"]["prompt_version"].endswith("+authorized-citywide-breadth-v2")
    model_scope = call["context"]["item_snapshot"]["authorized_search_scope_revision"]
    assert model_scope == {
        "revision": 2,
        "revision_sha256": "a" * 64,
        "preferred_area_text": "Северное Кольцо, рынки и внутренние улицы",
    }
    assert "authorization_reference" not in json.dumps(call["context"])


def test_profile_rejects_price_markers_and_excess_per_source_intents() -> None:
    payload = _profile().model_dump(mode="json")
    payload["uncertainty"] = ["Tender price should not be searched"]
    with pytest.raises(ValueError, match="private price marker"):
        SearchProfile.model_validate(payload)

    intent = _profile().intents[0].model_dump(mode="json")
    too_many = _profile().model_dump(mode="json")
    too_many["intents"] = [intent] * 7
    with pytest.raises(ValidationError, match="at most six intents per source"):
        SearchProfile.model_validate(too_many)


@pytest.mark.asyncio
async def test_profiles_and_multiple_intents_persist_idempotently() -> None:
    connection = _Connection()
    snapshot = await load_search_item_snapshot(
        connection,
        connection.run_id,
        connection.item_id,  # type: ignore[arg-type]
    )
    result = await persist_search_profile(
        connection,  # type: ignore[arg-type]
        snapshot,
        _profile(),
        model_version="model-a",
        prompt_version="prompt-a",
    )
    retry = await persist_search_profile(
        connection,  # type: ignore[arg-type]
        snapshot,
        _profile(),
        model_version="model-a",
        prompt_version="prompt-a",
    )

    assert result.created is True
    assert retry.created is False
    assert result.id == retry.id
    assert len(result.intent_ids) == 2
    assert len(connection.intents) == 2
    assert {row["provider"] for row in connection.intents.values()} == {"web", "2gis_web"}
    assert {row["query_text"] for row in connection.intents.values()} == {
        "кисточка малярная 80 мм купить Алматы",
        "магазин малярного инструмента",
    }
    assert (
        recompute_plan_sha256(
            ResearchPlan.model_validate(
                {
                    **connection.item["search_scope"]["plan"],
                    "plan_sha256": connection.item["search_scope"]["plan_sha256"],
                }
            )
        )
        == connection.item["search_scope"]["plan_sha256"]
    )


@pytest.mark.asyncio
async def test_changed_item_snapshot_rejects_stale_proposal() -> None:
    connection = _Connection()
    snapshot = await load_search_item_snapshot(
        connection,
        connection.run_id,
        connection.item_id,  # type: ignore[arg-type]
    )
    connection.item["specification_text"] = "Кисточка с другой шириной"

    with pytest.raises(SearchProfileConflict, match="changed after proposal"):
        await persist_search_profile(  # type: ignore[arg-type]
            connection, snapshot, _profile()
        )
    assert connection.profiles == {}
    assert connection.intents == {}


@pytest.mark.asyncio
async def test_mutated_in_memory_snapshot_is_rejected_before_extractor_call() -> None:
    connection = _Connection()
    snapshot = await load_search_item_snapshot(
        connection,
        connection.run_id,
        connection.item_id,  # type: ignore[arg-type]
    )
    snapshot.context["city"] = "Астана"
    extractor = FakeExtractor(_profile())

    with pytest.raises(SearchProfileConflict, match="snapshot was modified"):
        await propose_search_profile(extractor, snapshot)
    assert extractor.calls == []


@pytest.mark.asyncio
async def test_legacy_query_without_profile_falls_back_without_mutating_plan() -> None:
    connection = _Connection()
    plan = ResearchPlan.model_validate(
        {
            **connection.item["search_scope"]["plan"],
            "plan_sha256": connection.item["search_scope"]["plan_sha256"],
        }
    )
    branch = next(branch for branch in plan.source_plans if branch.source == ResearchSource.WEB)
    original = branch.model_dump(mode="json")

    legacy = await load_search_intent_plan(
        connection,  # type: ignore[arg-type]
        branch,
        uuid4(),
        ["already-seen"],
        "https://example.test/page/2",
    )

    assert legacy is None
    assert branch.model_dump(mode="json") == original
    assert recompute_plan_sha256(plan) == plan.plan_sha256


@pytest.mark.asyncio
async def test_saved_intent_is_runtime_copy_with_query_and_provider_cursor() -> None:
    connection = _Connection()
    snapshot = await load_search_item_snapshot(
        connection,
        connection.run_id,
        connection.item_id,  # type: ignore[arg-type]
    )
    plan = ResearchPlan.model_validate(
        {
            **connection.item["search_scope"]["plan"],
            "plan_sha256": connection.item["search_scope"]["plan_sha256"],
        }
    )
    branch = next(branch for branch in plan.source_plans if branch.source == ResearchSource.WEB)
    original = branch.model_dump(mode="json")
    connection.search_intent_rows = [
        {
            "ordinal": 0,
            "research_run_id": connection.run_id,
            "request_item_id": connection.item_id,
            "category": "малярный инструмент",
            "query_text": "кисточка 80 мм",
            "provider": "web",
            "scope_revision": 1,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "profile_id": uuid4(),
        },
        {
            "ordinal": 1,
            "research_run_id": connection.run_id,
            "request_item_id": connection.item_id,
            "category": "стройматериалы",
            "query_text": "магазин кисточек Алматы",
            "provider": "web",
            "scope_revision": 1,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "profile_id": uuid4(),
        },
        {
            "ordinal": 1,
            "research_run_id": uuid4(),
            "request_item_id": uuid4(),
            "category": "unrelated case",
            "query_text": "should not be selected",
            "provider": "web",
            "scope_revision": 1,
            "snapshot_sha256": "0" * 64,
            "profile_id": uuid4(),
        },
    ]

    runtime_plan = await load_search_intent_plan(
        connection,  # type: ignore[arg-type]
        branch,
        uuid4(),
        ["already-seen"],
        "https://example.test/page/3",
        intent_ordinal=1,
    )

    assert runtime_plan is not None
    assert runtime_plan.rendered_query == "магазин кисточек Алматы"
    assert runtime_plan.query_terms == ["магазин кисточек Алматы"]
    assert runtime_plan.search_cursor == "https://example.test/page/3"
    assert runtime_plan.excluded_external_ids == ["already-seen"]
    assert branch.model_dump(mode="json") == original
    assert recompute_plan_sha256(plan) == plan.plan_sha256
