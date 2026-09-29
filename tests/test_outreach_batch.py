from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from types import TracebackType
from uuid import UUID, uuid4

import pytest

import procurement_bot.outreach_batch as batch


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self, _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None, _traceback: TracebackType | None,
    ) -> None:
        return None


class _Connection:
    def __init__(self, *, expired: bool = True, opted_out: bool = False) -> None:
        self.case_id = uuid4()
        self.owner_id = uuid4()
        self.approval_id = uuid4()
        self.conversation_id = uuid4()
        self.now = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
        self.row = {
            "id": self.approval_id,
            "status": "requested",
            "expires_at": self.now - timedelta(minutes=1) if expired
                          else self.now + timedelta(hours=1),
            "payload": {
                "case_id": str(self.case_id), "channel": "whatsapp",
                "recipient": "+77085766841", "text": "Здравствуйте! Наличие товара?",
                "conversation_id": str(self.conversation_id),
                "logical_action_id": "initial-availability:" + str(uuid4()),
                "is_followup": False,
            },
            "conversation_id": self.conversation_id,
            "state": "approved", "outbound_count": 0,
            "opted_out": opted_out,
            "normalized_value": "+77085766841",
            "contact_type": "whatsapp", "active": True,
        }

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchval(self, _sql: str, *_args: object) -> str:
        return "contacting"

    async def fetchrow(self, _sql: str, *_args: object) -> dict[str, object]:
        return self.row


@pytest.mark.asyncio
async def test_expired_exact_draft_is_renewed_and_queued(monkeypatch) -> None:
    db = _Connection()
    calls: list[tuple[str, object]] = []
    renewed_id = uuid4()

    async def renew(_db: object, **kwargs: object) -> UUID:
        calls.append(("renew", kwargs["payload"]))
        return renewed_id

    async def approve(_db: object, **kwargs: object) -> bool:
        calls.append(("approve", kwargs["approval_id"]))
        return True

    async def enqueue(_db: object, **kwargs: object) -> UUID:
        calls.append(("queue", kwargs["payload"]))
        return uuid4()

    monkeypatch.setattr(batch, "request_approval", renew)
    monkeypatch.setattr(batch, "approve_request", approve)
    monkeypatch.setattr(batch, "enqueue_job", enqueue)

    result = await batch.approve_initial_outreach_batch(
        db, case_id=db.case_id, owner_user_id=db.owner_id,
        approval_ids=[db.approval_id], now=db.now,
    )

    assert result == [renewed_id]
    assert [kind for kind, _ in calls] == ["renew", "approve", "queue"]
    assert calls[-1][1] == {"approval_id": str(renewed_id)}


@pytest.mark.asyncio
async def test_opted_out_draft_cannot_be_approved(monkeypatch) -> None:
    db = _Connection(opted_out=True)

    async def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no action may be queued")

    monkeypatch.setattr(batch, "enqueue_job", unexpected)
    with pytest.raises(batch.OutreachBatchConflict, match="Контакт или диалог"):
        await batch.approve_initial_outreach_batch(
            db, case_id=db.case_id, owner_user_id=db.owner_id,
            approval_ids=[db.approval_id], now=db.now,
        )


@pytest.mark.asyncio
async def test_quiet_hours_block_before_touching_database() -> None:
    db = _Connection()
    with pytest.raises(batch.OutreachBatchConflict, match="09:00 до 20:00"):
        await batch.approve_initial_outreach_batch(
            db, case_id=db.case_id, owner_user_id=db.owner_id,
            approval_ids=[db.approval_id], now=datetime(2026, 9, 28, 16, 0, tzinfo=UTC),
        )
