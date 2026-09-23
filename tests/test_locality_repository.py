from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

import procurement_bot.locality_repository as repository_module
from procurement_bot.intake import ParsedProcurementRequest, ProcurementItem, SearchScope
from procurement_bot.locality_repository import (
    LocalityPersistenceConflict,
    PostgreSQLLocalityResolutionRepository,
    locality_lookup_sha256,
)
from procurement_bot.locality_workflow import (
    LocalityClarificationNotice,
    LocalityWorkflowDenied,
)
from procurement_bot.research import (
    LocalityCandidate,
    LocalityResolution,
    LocalityResolutionStatus,
    ResearchSource,
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


class _Acquire(AbstractAsyncContextManager["_Connection"]):
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Connection:
    def __init__(self, *, request: ParsedProcurementRequest) -> None:
        self.case_id = uuid4()
        self.owner_id = uuid4()
        self.parse_id = uuid4()
        self.request = request
        self.case_status = "needs_clarification"
        self.owner_active = True
        self.resolution: dict[str, Any] | None = None
        self.insert_args: tuple[Any, ...] | None = None

    def transaction(self) -> _Transaction:
        return _Transaction()

    def _context_row(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "owner_user_id": self.owner_id,
            "case_status": self.case_status,
            "telegram_chat_id": 778899,
            "owner_active": self.owner_active,
            "case_parse_version_id": self.parse_id,
            "intake_version": 3,
            "normalized_payload": self.request.model_dump(mode="json"),
        }

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM procurement_cases AS c" in sql:
            case_id, owner_id, version = args
            if case_id != self.case_id or owner_id != self.owner_id or version != 3:
                return None
            return self._context_row()
        if "FROM locality_resolutions" in sql:
            return self.resolution
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(self, sql: str, *args: Any) -> int:
        assert "COALESCE(MAX(version),0)+1" in sql
        return 1

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO locality_resolutions" in sql:
            self.insert_args = args
            self.resolution = {
                "id": args[0],
                "version": args[4],
                "status": args[8],
                "result_sha256": args[12],
                "context_token": args[16],
            }
            return "INSERT 0 1"
        if "UPDATE procurement_cases" in sql:
            if self.case_status != "needs_clarification":
                return "UPDATE 0"
            if "status='ready'" in sql:
                self.case_status = "ready"
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")


def _request() -> ParsedProcurementRequest:
    return ParsedProcurementRequest(
        city="Алматы",
        search_scope=SearchScope.SPECIFIC_AREA,
        search_area_text="на первой Алмате",
        items=[ProcurementItem(name="Марля", quantity=10, unit="рулон")],
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


def _resolved() -> LocalityResolution:
    selected = _candidate("Алматы-1", 0.97, "station")
    return LocalityResolution(
        phrase="на первой Алмате",
        city="Алматы",
        status=LocalityResolutionStatus.RESOLVED,
        candidates=[selected],
        selected=selected,
    )


@pytest.mark.asyncio
async def test_resolved_commit_is_atomic_storage_and_exact_prepare_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(request=_request())
    repository = PostgreSQLLocalityResolutionRepository(_Pool(connection))  # type: ignore[arg-type]
    context = await repository.load_context(
        case_id=connection.case_id,
        intake_version=3,
        owner_user_id=connection.owner_id,
    )
    jobs: list[dict[str, Any]] = []

    async def enqueue(_connection: Any, **kwargs: Any) -> UUID:
        jobs.append(kwargs)
        return uuid4()

    monkeypatch.setattr(repository_module, "enqueue_job", enqueue)
    outcome = await repository.commit_resolution(
        context=context,
        resolution=_resolved(),
        clarification=None,
    )

    assert outcome.newly_persisted is True
    assert outcome.status == LocalityResolutionStatus.RESOLVED
    assert connection.case_status == "ready"
    assert connection.insert_args is not None
    assert connection.insert_args[9] == [_resolved().candidates[0].model_dump(mode="json")]
    assert len(connection.insert_args[10]) == 64  # candidates_sha256
    assert len(connection.insert_args[12]) == 64  # result_sha256
    assert connection.insert_args[13] == _resolved().selected.model_dump(mode="json")
    assert len(connection.insert_args[14]) == 64  # selected candidate hash
    assert len(connection.insert_args[16]) == 64  # context token
    assert jobs[0]["kind"] == "prepare_research"
    assert jobs[0]["payload"] == {
        "case_id": str(connection.case_id),
        "intake_version": 3,
        "locality_resolution_id": str(outcome.resolution_id),
        "locality_resolution_version": 1,
    }

    retried = await repository.commit_resolution(
        context=context,
        resolution=_resolved(),
        clarification=None,
    )
    assert retried.resolution_id == outcome.resolution_id
    assert retried.newly_persisted is False
    assert len(jobs) == 1


@pytest.mark.asyncio
async def test_ambiguous_commit_keeps_case_blocked_and_enqueues_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(request=_request())
    repository = PostgreSQLLocalityResolutionRepository(_Pool(connection))  # type: ignore[arg-type]
    context = await repository.load_context(
        case_id=connection.case_id,
        intake_version=3,
        owner_user_id=connection.owner_id,
    )
    first = _candidate("Вокзал Алматы-1", 0.88, "station")
    second = _candidate("Район Алматы-1", 0.82, "district")
    resolution = LocalityResolution(
        phrase="на первой Алмате",
        city="Алматы",
        status=LocalityResolutionStatus.NEEDS_CLARIFICATION,
        candidates=[first, second],
        clarification_question="Какой из двух районов нужен?",
    )
    notice = LocalityClarificationNotice(
        chat_id=context.telegram_chat_id,
        question="Какой из двух районов нужен?",
        options=("Вокзал", "Район"),
        text="Какой из двух районов нужен?\n\n1. Вокзал\n2. Район",
    )
    outbox: list[dict[str, Any]] = []

    async def enqueue(_connection: Any, **kwargs: Any) -> UUID:
        outbox.append(kwargs)
        return uuid4()

    monkeypatch.setattr(repository_module, "enqueue_outbox", enqueue)
    outcome = await repository.commit_resolution(
        context=context,
        resolution=resolution,
        clarification=notice,
    )

    assert outcome.followup_kind == "telegram_clarification"
    assert connection.case_status == "needs_clarification"
    assert outbox[0]["event_type"] == "telegram.send_message"
    assert outbox[0]["payload"] == {
        "chat_id": context.telegram_chat_id,
        "text": notice.text,
    }


@pytest.mark.asyncio
async def test_commit_rechecks_exact_context_token_and_result_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(request=_request())
    repository = PostgreSQLLocalityResolutionRepository(_Pool(connection))  # type: ignore[arg-type]
    context = await repository.load_context(
        case_id=connection.case_id,
        intake_version=3,
        owner_user_id=connection.owner_id,
    )

    with pytest.raises(LocalityWorkflowDenied, match="token"):
        await repository.commit_resolution(
            context=replace(context, context_token="0" * 64),
            resolution=_resolved(),
            clarification=None,
        )
    assert connection.resolution is None

    jobs: list[dict[str, Any]] = []

    async def enqueue(_connection: Any, **kwargs: Any) -> UUID:
        jobs.append(kwargs)
        return uuid4()

    monkeypatch.setattr(repository_module, "enqueue_job", enqueue)
    await repository.commit_resolution(
        context=context,
        resolution=_resolved(),
        clarification=None,
    )
    changed = _resolved().model_copy(update={"candidates": [_candidate("Алматы-1", 0.99, "other")]})
    # Build a coherent but different immutable result.
    changed = changed.model_copy(update={"selected": changed.candidates[0]})
    with pytest.raises(LocalityPersistenceConflict, match="different immutable"):
        await repository.commit_resolution(
            context=context,
            resolution=changed,
            clarification=None,
        )


@pytest.mark.asyncio
async def test_load_enforces_owner_version_and_returns_completed_without_research() -> None:
    connection = _Connection(request=_request())
    repository = PostgreSQLLocalityResolutionRepository(_Pool(connection))  # type: ignore[arg-type]
    context = await repository.load_context(
        case_id=connection.case_id,
        intake_version=3,
        owner_user_id=connection.owner_id,
    )
    connection.resolution = {
        "id": uuid4(),
        "version": 2,
        "status": "resolved",
        "result_sha256": "b" * 64,
        "context_token": context.context_token,
    }
    connection.case_status = "ready"

    completed = await repository.load_context(
        case_id=connection.case_id,
        intake_version=3,
        owner_user_id=connection.owner_id,
    )
    assert completed.completed is not None
    assert completed.completed.resolution_version == 2
    assert completed.completed.newly_persisted is False

    with pytest.raises(LocalityWorkflowDenied, match="owner"):
        await repository.load_context(
            case_id=connection.case_id,
            intake_version=3,
            owner_user_id=uuid4(),
        )


def test_lookup_identity_normalizes_case_and_whitespace_without_exposing_phrase() -> None:
    first = locality_lookup_sha256(city=" Алматы ", phrase=" На Первой Алматы ")
    second = locality_lookup_sha256(city="алматы", phrase="на первой алматы")

    assert first == second
    assert len(first) == 64
    assert "алматы" not in first
