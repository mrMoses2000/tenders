from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.research import build_research_plan
from procurement_bot.research_repository import (
    InvalidResearchRunTransition,
    ResearchPersistenceConflict,
    complete_research_run,
    mark_research_running,
    persist_research_plan,
    recompute_plan_sha256,
    research_job_payload,
)


def _plan():
    request = ParsedProcurementRequest(
        city="Алматы",
        search_scope=SearchScope.CITYWIDE,
        items=[ProcurementItem(name="Марля", quantity=10, unit="рулон")],
    )
    return build_research_plan(request)


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
        self.runs_by_key: dict[str, dict[str, Any]] = {}
        self.runs_by_id: dict[UUID, dict[str, Any]] = {}
        self.queries_by_key: dict[str, dict[str, Any]] = {}

    def transaction(self) -> AbstractAsyncContextManager[None]:
        return _Transaction()

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO research_runs" in sql:
            proposed_id, case_id, requested_by, search_scope, key = args
            if key in self.runs_by_key:
                return None
            row = {
                "id": proposed_id,
                "case_id": case_id,
                "requested_by": requested_by,
                "search_scope": search_scope,
                "idempotency_key": key,
                "status": "planned",
                "error_code": "",
            }
            self.runs_by_key[key] = row
            self.runs_by_id[proposed_id] = row
            return row
        if sql.startswith("SELECT * FROM research_runs WHERE idempotency_key"):
            return self.runs_by_key.get(args[0])
        if "INSERT INTO research_queries" in sql:
            (
                query_id,
                run_id,
                provider,
                query_text,
                city,
                search_area_text,
                latitude,
                longitude,
                parameters,
                key,
            ) = args
            if key in self.queries_by_key:
                return None
            row = {
                "id": query_id,
                "research_run_id": run_id,
                "provider": provider,
                "query_text": query_text,
                "city": city,
                "search_area_text": search_area_text,
                "latitude": latitude,
                "longitude": longitude,
                "radius_meters": None,
                "parameters": parameters,
                "idempotency_key": key,
            }
            self.queries_by_key[key] = row
            return row
        if sql.startswith("SELECT * FROM research_queries WHERE idempotency_key"):
            return self.queries_by_key.get(args[0])
        if "SET status='running'" in sql:
            row = self.runs_by_id.get(args[0])
            if row is None or row["status"] not in {"planned", "running"}:
                return None
            row["status"] = "running"
            row["error_code"] = ""
            return row
        if "SET status=$2" in sql:
            run_id, status, error_code = args
            row = self.runs_by_id.get(run_id)
            if row is None or row["status"] != "running":
                return None
            row["status"] = status
            row["error_code"] = error_code
            return row
        if sql.startswith("SELECT id,status,error_code FROM research_runs"):
            return self.runs_by_id.get(args[0])
        raise AssertionError(f"unexpected SQL: {sql}")


def test_plan_hash_and_job_payload_are_storage_safe() -> None:
    plan = _plan()
    run_id = uuid4()

    assert recompute_plan_sha256(plan) == plan.plan_sha256
    assert research_job_payload(run_id) == {"research_run_id": str(run_id)}
    with pytest.raises(TypeError, match="UUID"):
        research_job_payload(str(run_id))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_plan_and_all_source_branches_are_persisted_idempotently() -> None:
    connection = _Connection()
    case_id = uuid4()
    requested_by = uuid4()
    plan = _plan()

    first = await persist_research_plan(
        connection,  # type: ignore[arg-type]
        case_id=case_id,
        plan=plan,
        requested_by=requested_by,
    )
    retry = await persist_research_plan(
        connection,  # type: ignore[arg-type]
        case_id=case_id,
        plan=plan,
        requested_by=requested_by,
    )

    assert first.created is True
    assert retry.created is False
    assert retry.id == first.id
    assert retry.query_ids == first.query_ids
    assert len(first.query_ids) == len(plan.source_plans) == 3
    assert [row["provider"] for row in connection.queries_by_key.values()] == [
        "own_database",
        "2gis_web",
        "web",
    ]
    assert all(
        row["parameters"]["branch_ordinal"] == index
        for index, row in enumerate(connection.queries_by_key.values())
    )


@pytest.mark.asyncio
async def test_payload_drift_is_rejected_for_run_and_query() -> None:
    connection = _Connection()
    plan = _plan()
    case_id = uuid4()
    persisted = await persist_research_plan(
        connection,  # type: ignore[arg-type]
        case_id=case_id,
        plan=plan,
    )

    run_row = connection.runs_by_id[persisted.id]
    run_row["search_scope"] = {**run_row["search_scope"], "contract_version": 99}
    with pytest.raises(ResearchPersistenceConflict, match="search_scope"):
        await persist_research_plan(
            connection,  # type: ignore[arg-type]
            case_id=case_id,
            plan=plan,
        )

    run_row["search_scope"] = {
        "contract_version": 1,
        "plan_sha256": plan.plan_sha256,
        "plan": plan.model_dump(mode="json", exclude={"plan_sha256"}),
    }
    first_query = next(iter(connection.queries_by_key.values()))
    first_query["provider"] = "web"
    with pytest.raises(ResearchPersistenceConflict, match="provider"):
        await persist_research_plan(
            connection,  # type: ignore[arg-type]
            case_id=case_id,
            plan=plan,
        )


@pytest.mark.asyncio
async def test_forged_plan_hash_is_rejected_before_database_write() -> None:
    connection = _Connection()
    forged = _plan().model_copy(update={"plan_sha256": "0" * 64})

    with pytest.raises(ResearchPersistenceConflict, match="plan_sha256"):
        await persist_research_plan(
            connection,  # type: ignore[arg-type]
            case_id=uuid4(),
            plan=forged,
        )
    assert not connection.runs_by_id


@pytest.mark.asyncio
async def test_lifecycle_transitions_are_idempotent_and_fail_closed() -> None:
    connection = _Connection()
    persisted = await persist_research_plan(
        connection,  # type: ignore[arg-type]
        case_id=uuid4(),
        plan=_plan(),
    )

    running = await mark_research_running(connection, persisted.id)  # type: ignore[arg-type]
    running_retry = await mark_research_running(
        connection, persisted.id  # type: ignore[arg-type]
    )
    assert running.status == running_retry.status == "running"

    completed = await complete_research_run(
        connection,  # type: ignore[arg-type]
        persisted.id,
        status="succeeded",
    )
    completed_retry = await complete_research_run(
        connection,  # type: ignore[arg-type]
        persisted.id,
        status="succeeded",
    )
    assert completed.status == completed_retry.status == "succeeded"
    with pytest.raises(InvalidResearchRunTransition):
        await complete_research_run(
            connection,  # type: ignore[arg-type]
            persisted.id,
            status="failed",
            error_code="provider_failed",
        )


@pytest.mark.asyncio
async def test_completion_requires_running_and_failure_code() -> None:
    connection = _Connection()
    persisted = await persist_research_plan(
        connection,  # type: ignore[arg-type]
        case_id=uuid4(),
        plan=_plan(),
    )

    with pytest.raises(ValueError, match="requires an error_code"):
        await complete_research_run(
            connection,  # type: ignore[arg-type]
            persisted.id,
            status="failed",
        )
    with pytest.raises(InvalidResearchRunTransition):
        await complete_research_run(
            connection,  # type: ignore[arg-type]
            persisted.id,
            status="partial",
            error_code="some_sources_failed",
        )
