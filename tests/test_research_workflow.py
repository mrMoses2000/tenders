from __future__ import annotations

import copy
import hashlib
import json
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

from procurement_bot.approvals import payload_sha256
from procurement_bot.queue import stable_key
from procurement_bot.research_workflow import (
    ResearchStartDenied,
    start_approved_research,
)


def _plan_scope() -> tuple[dict[str, Any], str]:
    plan = {
        "city": "Алматы",
        "source_plans": [{"source": "2gis", "query_terms": ["марля"]}],
    }
    encoded = json.dumps(
        plan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    return {"contract_version": 1, "plan_sha256": digest, "plan": plan}, digest


class _Transaction(AbstractAsyncContextManager[None]):
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.snapshot: dict[str, Any] | None = None

    async def __aenter__(self) -> None:
        self.snapshot = copy.deepcopy(self.connection._state())
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None and self.snapshot is not None:
            self.connection._restore(self.snapshot)


class _Acquire(AbstractAsyncContextManager["_Connection"]):
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Connection:
    def __init__(self) -> None:
        self.approval_id = uuid4()
        self.case_id = uuid4()
        self.run_id = uuid4()
        self.approved_by = uuid4()
        self.scope, plan_hash = _plan_scope()
        payload = {
            "case_id": str(self.case_id),
            "research_run_id": str(self.run_id),
            "plan_sha256": plan_hash,
        }
        self.approval: dict[str, Any] = {
            "id": self.approval_id,
            "case_id": self.case_id,
            "action_type": "start_research",
            "payload": payload,
            "payload_sha256": payload_sha256(payload),
            "status": "approved",
            "approved_by": self.approved_by,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
            "consumed_at": None,
        }
        self.case: dict[str, Any] = {"id": self.case_id, "status": "ready"}
        self.run: dict[str, Any] = {
            "id": self.run_id,
            "case_id": self.case_id,
            "status": "planned",
            "search_scope": self.scope,
        }
        self.events: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}

    def _state(self) -> dict[str, Any]:
        return {
            "approval": self.approval,
            "case": self.case,
            "run": self.run,
            "events": self.events,
            "jobs": self.jobs,
        }

    def _restore(self, value: dict[str, Any]) -> None:
        self.approval = value["approval"]
        self.case = value["case"]
        self.run = value["run"]
        self.events = value["events"]
        self.jobs = value["jobs"]

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        if "FROM action_approvals" in sql:
            if args[0] != self.approval_id:
                return None
            now = datetime.now(UTC)
            expires_at = self.approval["expires_at"]
            consumed_at = self.approval["consumed_at"]
            return {
                **self.approval,
                "is_live": expires_at is None or expires_at > now,
                "consumed_when_live": expires_at is None
                or (consumed_at is not None and consumed_at < expires_at),
            }
        if "FROM research_runs" in sql:
            return self.run if args[0] == self.run_id else None
        if "FROM procurement_cases" in sql:
            return self.case if args[0] == self.case_id else None
        if "FROM workflow_events" in sql:
            return self.events.get(str(args[0]))
        if "FROM jobs" in sql:
            return self.jobs.get(str(args[0]))
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *args: object) -> str:
        if "UPDATE action_approvals" in sql:
            if self.approval["status"] != "approved":
                return "UPDATE 0"
            self.approval["status"] = "consumed"
            self.approval["consumed_at"] = datetime.now(UTC)
            return "UPDATE 1"
        if "UPDATE procurement_cases" in sql:
            if self.case["status"] != "ready":
                return "UPDATE 0"
            self.case["status"] = "researching"
            return "UPDATE 1"
        if "UPDATE research_runs" in sql:
            if self.run["status"] != "planned":
                return "UPDATE 0"
            self.run["status"] = "running"
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute SQL: {sql}")

    async def fetchval(self, sql: str, *args: object) -> UUID | int | None:
        if "INSERT INTO workflow_events" in sql:
            case_id, event_type, actor_id, key, data = args
            assert case_id == self.case_id
            assert actor_id == str(self.approved_by)
            if str(key) in self.events:
                return None
            self.events[str(key)] = {
                "event_type": event_type,
                "data": data,
            }
            return 1
        if "INSERT INTO jobs" in sql:
            job_id, kind, payload, key, max_attempts = args
            assert max_attempts == 5
            if str(key) in self.jobs:
                return None
            self.jobs[str(key)] = {
                "id": job_id,
                "kind": kind,
                "payload": payload,
            }
            return job_id  # type: ignore[return-value]
        raise AssertionError(f"unexpected fetchval SQL: {sql}")


@pytest.mark.asyncio
async def test_exact_approval_atomically_starts_and_enqueues_internal_job() -> None:
    connection = _Connection()

    result = await start_approved_research(
        connection,  # type: ignore[arg-type]
        approval_id=connection.approval_id,
    )

    assert result.newly_started is True
    assert result.case_id == connection.case_id
    assert result.research_run_id == connection.run_id
    assert result.case_status == connection.case["status"] == "researching"
    assert result.run_status == connection.run["status"] == "running"
    assert connection.approval["status"] == "consumed"
    job_key = stable_key("execute-research", connection.run_id)
    assert connection.jobs[job_key] == {
        "id": result.job_id,
        "kind": "execute_research",
        "payload": {"research_run_id": str(connection.run_id)},
    }


@pytest.mark.asyncio
async def test_consumed_retry_through_pool_returns_existing_started_state() -> None:
    connection = _Connection()
    pool = _Pool(connection)
    first = await start_approved_research(  # type: ignore[arg-type]
        pool,
        approval_id=connection.approval_id,
    )
    retry = await start_approved_research(  # type: ignore[arg-type]
        pool,
        approval_id=connection.approval_id,
    )

    assert retry == replace(first, newly_started=False)
    assert len(connection.jobs) == 1
    assert len(connection.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda db: db.approval.update(action_type="send_whatsapp"), "different action"),
        (
            lambda db: db.approval["payload"].update(extra="unexpected"),
            "must contain exactly",
        ),
        (
            lambda db: db.approval.update(payload_sha256="0" * 64),
            "payload hash is invalid",
        ),
        (
            lambda db: db.approval.update(expires_at=datetime.now(UTC) - timedelta(seconds=1)),
            "approved and unexpired",
        ),
        (
            lambda db: db.run.update(case_id=uuid4()),
            "belongs to a different case",
        ),
        (lambda db: db.case.update(status="needs_clarification"), "not ready"),
        (lambda db: db.run.update(status="running"), "not in planned"),
        (
            lambda db: db.scope.update(plan_sha256="0" * 64),
            "persisted research plan hash is invalid",
        ),
    ],
)
async def test_foreign_stale_or_mismatched_start_fails_closed(
    mutation: Any,
    match: str,
) -> None:
    connection = _Connection()
    mutation(connection)

    with pytest.raises(ResearchStartDenied, match=match):
        await start_approved_research(
            connection,  # type: ignore[arg-type]
            approval_id=connection.approval_id,
        )

    assert connection.approval["status"] == "approved"
    assert not connection.events
    assert not connection.jobs


@pytest.mark.asyncio
async def test_consumed_approval_without_exact_event_and_job_fails_closed() -> None:
    connection = _Connection()
    connection.approval["status"] = "consumed"
    connection.approval["consumed_at"] = datetime.now(UTC)
    connection.case["status"] = "researching"
    connection.run["status"] = "running"

    with pytest.raises(ResearchStartDenied, match="workflow event"):
        await start_approved_research(
            connection,  # type: ignore[arg-type]
            approval_id=connection.approval_id,
        )


@pytest.mark.asyncio
async def test_approval_id_must_be_uuid() -> None:
    with pytest.raises(TypeError, match="approval_id"):
        await start_approved_research(  # type: ignore[arg-type]
            _Connection(),
            approval_id="not-a-uuid",  # type: ignore[arg-type]
        )
