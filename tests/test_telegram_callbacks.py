from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import procurement_bot.telegram_callbacks as callback_module
from procurement_bot.queue import stable_key
from procurement_bot.telegram_callbacks import (
    ResearchApprovalCallbackIngress,
    parse_research_callback,
    parse_whatsapp_callback,
)


class FakeConnection:
    def __init__(self, *, approval_row: dict[str, object] | None = None) -> None:
        self.approval_row = approval_row
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.transactions = 0

    def transaction(self):
        @asynccontextmanager
        async def manager():
            self.transactions += 1
            yield

        return manager()

    async def fetchrow(self, sql: str, *args: object):
        self.fetchrow_calls.append((sql, args))
        return self.approval_row

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        if "SET status='rejected'" in sql:
            return "UPDATE 1"
        return "INSERT 0 1" if "INSERT INTO audit_log" in sql else "UPDATE 1"


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self):
        @asynccontextmanager
        async def manager():
            yield self.connection

        return manager()


def make_update(
    *,
    update_id: int,
    data: object,
    sender_id: int = 9,
    callback_id: str = "callback-1",
):
    callback = SimpleNamespace(
        id=callback_id,
        data=data,
        from_user=SimpleNamespace(id=sender_id),
    )
    return SimpleNamespace(
        update_id=update_id,
        callback_query=callback,
        model_dump=lambda **_: {
            "update_id": update_id,
            "callback_query": {"id": callback_id, "data": data},
        },
    )


@pytest.fixture
def queue_spy(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, object] = {
        "accepted": True,
        "accepts": [],
        "jobs": [],
        "outbox": [],
        "approvals": [],
    }

    async def accept(*args, **kwargs):
        state["accepts"].append((args, kwargs))  # type: ignore[union-attr]
        return state["accepted"]

    async def job(*args, **kwargs):
        state["jobs"].append(kwargs)  # type: ignore[union-attr]
        return uuid4()

    async def outbox(*args, **kwargs):
        state["outbox"].append(kwargs)  # type: ignore[union-attr]
        return uuid4()

    async def approve(*args, **kwargs):
        state["approvals"].append(kwargs)  # type: ignore[union-attr]
        return True

    monkeypatch.setattr(callback_module, "accept_update", accept)
    monkeypatch.setattr(callback_module, "enqueue_job", job)
    monkeypatch.setattr(callback_module, "enqueue_outbox", outbox)
    monkeypatch.setattr(callback_module, "approve_request", approve)
    return state


def test_parser_accepts_only_canonical_contract() -> None:
    approval_id = uuid4()

    approved = parse_research_callback(f"rs:a:{approval_id}")
    rejected = parse_research_callback(f"rs:r:{approval_id}")

    assert approved is not None
    assert approved.approval_id == approval_id
    assert approved.decision == "approve"
    assert rejected is not None and rejected.decision == "reject"
    invalid = [
        None,
        "",
        f"rs:x:{approval_id}",
        f"rs:a:{str(approval_id).upper()}",
        f"rs:a:{approval_id}:extra",
        f"prefix:rs:a:{approval_id}",
        "rs:a:not-a-uuid",
    ]
    assert all(parse_research_callback(value) is None for value in invalid)


def test_whatsapp_parser_has_a_separate_canonical_namespace() -> None:
    approval_id = uuid4()
    parsed = parse_whatsapp_callback(f"wa:a:{approval_id}")
    assert parsed is not None
    assert parsed.namespace == "whatsapp"
    assert parsed.decision == "approve"
    assert parse_whatsapp_callback(f"rs:a:{approval_id}") is None


@pytest.mark.asyncio
async def test_approve_verifies_owner_and_enqueues_opaque_job(queue_spy) -> None:
    approval_id = uuid4()
    actor_id = uuid4()
    case_id = uuid4()
    connection = FakeConnection(
        approval_row={
            "id": approval_id,
            "case_id": case_id,
            "status": "requested",
            "unexpired": True,
            "actor_user_id": actor_id,
        }
    )
    ingress = ResearchApprovalCallbackIngress(
        pool=FakePool(connection),  # type: ignore[arg-type]
        allowed_user_ids={9},
    )

    accepted = await ingress.accept(
        make_update(update_id=101, data=f"rs:a:{approval_id}")  # type: ignore[arg-type]
    )

    assert accepted is True
    assert connection.transactions == 1
    ownership_sql, ownership_args = connection.fetchrow_calls[0]
    assert "owner.telegram_id=$2" in ownership_sql
    assert "approval.action_type='start_research'" in ownership_sql
    assert "FOR UPDATE OF approval" in ownership_sql
    assert ownership_args == (approval_id, 9)
    assert queue_spy["approvals"] == [
        {"approval_id": approval_id, "approved_by": actor_id}
    ]
    assert queue_spy["jobs"] == [
        {
            "kind": "start_research",
            "payload": {"approval_id": str(approval_id)},
            "idempotency_key": stable_key("start-research-approval", approval_id),
        }
    ]
    answer = queue_spy["outbox"][0]  # type: ignore[index]
    assert answer["event_type"] == "telegram.answer_callback_query"
    assert answer["payload"]["show_alert"] is False
    assert any("INSERT INTO audit_log" in sql for sql, _ in connection.execute_calls)
    assert any("UPDATE processed_updates" in sql for sql, _ in connection.execute_calls)


@pytest.mark.asyncio
async def test_whatsapp_approval_enqueues_only_opaque_send_control_job(queue_spy) -> None:
    approval_id = uuid4()
    actor_id = uuid4()
    connection = FakeConnection(
        approval_row={
            "id": approval_id,
            "case_id": uuid4(),
            "status": "requested",
            "unexpired": True,
            "actor_user_id": actor_id,
        }
    )
    ingress = ResearchApprovalCallbackIngress(
        pool=FakePool(connection),  # type: ignore[arg-type]
        allowed_user_ids={9},
    )

    await ingress.accept(
        make_update(update_id=107, data=f"wa:a:{approval_id}")  # type: ignore[arg-type]
    )

    ownership_sql, _ = connection.fetchrow_calls[0]
    assert "approval.action_type='send_whatsapp'" in ownership_sql
    assert queue_spy["jobs"] == [
        {
            "kind": "enqueue_whatsapp_send",
            "payload": {"approval_id": str(approval_id)},
            "idempotency_key": stable_key(
                "enqueue-whatsapp-send-approval",
                approval_id,
            ),
        }
    ]
    assert queue_spy["outbox"][0]["payload"]["show_alert"] is False  # type: ignore[index]


@pytest.mark.asyncio
async def test_reject_is_attributed_and_never_enqueues_research(queue_spy) -> None:
    approval_id = uuid4()
    actor_id = uuid4()
    connection = FakeConnection(
        approval_row={
            "id": approval_id,
            "case_id": uuid4(),
            "status": "requested",
            "unexpired": True,
            "actor_user_id": actor_id,
        }
    )
    ingress = ResearchApprovalCallbackIngress(
        pool=FakePool(connection),  # type: ignore[arg-type]
        allowed_user_ids={9},
    )

    await ingress.accept(
        make_update(update_id=102, data=f"rs:r:{approval_id}")  # type: ignore[arg-type]
    )

    assert queue_spy["approvals"] == []
    assert queue_spy["jobs"] == []
    rejected = [
        args
        for sql, args in connection.execute_calls
        if "SET status='rejected'" in sql
    ]
    assert rejected == [(approval_id,)]
    assert queue_spy["outbox"][0]["payload"]["show_alert"] is False  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["bad", None])
async def test_malformed_callback_fails_closed_but_is_answered(queue_spy, data) -> None:
    connection = FakeConnection()
    ingress = ResearchApprovalCallbackIngress(
        pool=FakePool(connection),  # type: ignore[arg-type]
        allowed_user_ids={9},
    )

    assert await ingress.accept(
        make_update(update_id=103, data=data)  # type: ignore[arg-type]
    )

    assert connection.fetchrow_calls == []
    assert queue_spy["approvals"] == []
    assert queue_spy["jobs"] == []
    answer = queue_spy["outbox"][0]  # type: ignore[index]
    assert answer["payload"]["show_alert"] is True


@pytest.mark.asyncio
async def test_not_allowlisted_is_answered_without_lookup_or_mutation(queue_spy) -> None:
    approval_id = uuid4()
    connection = FakeConnection()
    ingress = ResearchApprovalCallbackIngress(
        pool=FakePool(connection),  # type: ignore[arg-type]
        allowed_user_ids={10},
    )

    await ingress.accept(
        make_update(
            update_id=104,
            data=f"rs:a:{approval_id}",
            sender_id=9,
        )  # type: ignore[arg-type]
    )

    assert connection.fetchrow_calls == []
    assert queue_spy["approvals"] == []
    assert queue_spy["jobs"] == []
    assert queue_spy["outbox"][0]["payload"]["show_alert"] is True  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approval_row",
    [
        None,
        {
            "id": UUID("11111111-1111-1111-1111-111111111111"),
            "case_id": UUID("22222222-2222-2222-2222-222222222222"),
            "status": "approved",
            "unexpired": True,
            "actor_user_id": UUID("33333333-3333-3333-3333-333333333333"),
        },
        {
            "id": UUID("11111111-1111-1111-1111-111111111111"),
            "case_id": UUID("22222222-2222-2222-2222-222222222222"),
            "status": "requested",
            "unexpired": False,
            "actor_user_id": UUID("33333333-3333-3333-3333-333333333333"),
        },
    ],
)
async def test_non_owner_handled_or_expired_request_never_mutates_approval(
    queue_spy,
    approval_row,
) -> None:
    approval_id = UUID("11111111-1111-1111-1111-111111111111")
    connection = FakeConnection(approval_row=approval_row)
    ingress = ResearchApprovalCallbackIngress(
        pool=FakePool(connection),  # type: ignore[arg-type]
        allowed_user_ids={9},
    )

    await ingress.accept(
        make_update(update_id=105, data=f"rs:a:{approval_id}")  # type: ignore[arg-type]
    )

    assert queue_spy["approvals"] == []
    assert queue_spy["jobs"] == []
    assert not any("action_approvals\n" in sql for sql, _ in connection.execute_calls)
    assert queue_spy["outbox"][0]["payload"]["show_alert"] is True  # type: ignore[index]


@pytest.mark.asyncio
async def test_duplicate_update_is_idempotent_and_not_answered_again(queue_spy) -> None:
    queue_spy["accepted"] = False
    connection = FakeConnection()
    ingress = ResearchApprovalCallbackIngress(
        pool=FakePool(connection),  # type: ignore[arg-type]
        allowed_user_ids={9},
    )

    accepted = await ingress.accept(
        make_update(update_id=106, data=f"rs:a:{uuid4()}")  # type: ignore[arg-type]
    )

    assert accepted is False
    assert connection.fetchrow_calls == []
    assert connection.execute_calls == []
    assert queue_spy["jobs"] == []
    assert queue_spy["outbox"] == []


def test_allowlist_fails_closed_and_rejects_invalid_ids() -> None:
    connection = FakeConnection()
    ingress = ResearchApprovalCallbackIngress(
        pool=FakePool(connection),  # type: ignore[arg-type]
        allowed_user_ids=None,
    )
    assert ingress.allowed_user_ids == frozenset()
    with pytest.raises(ValueError, match="positive Telegram IDs"):
        ResearchApprovalCallbackIngress(
            pool=FakePool(connection),  # type: ignore[arg-type]
            allowed_user_ids={0},
        )
