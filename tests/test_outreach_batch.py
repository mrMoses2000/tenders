from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from uuid import UUID, uuid4

import pytest

import procurement_bot.outreach_batch as batch
from procurement_bot.supplier_outreach import initial_inquiry_text


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
        self.offer_id = uuid4()
        self.now = datetime(2026, 9, 28, 6, 0, tzinfo=UTC)
        self.row = {
            "id": self.approval_id,
            "status": "requested",
            "expires_at": self.now - timedelta(minutes=1) if expired
                          else self.now + timedelta(hours=1),
            "payload": {
                "case_id": str(self.case_id), "channel": "whatsapp",
                "recipient": "+77085766841",
                "conversation_id": str(self.conversation_id),
                "logical_action_id": "initial-availability:" + str(self.offer_id),
                "is_followup": False,
            },
            "conversation_id": self.conversation_id,
            "offer_id": self.offer_id,
            "item_name": "Товар",
            "specification_text": "",
            "quantity": Decimal("2"),
            "unit": "шт.",
            "state": "approved", "outbound_count": 0,
            "opted_out": opted_out,
            "normalized_value": "+77085766841",
            "contact_type": "whatsapp", "active": True,
        }
        self.row["payload"]["text"] = initial_inquiry_text(
            self.row["item_name"], self.row["specification_text"],
            self.row["quantity"], self.row["unit"],
        )

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchval(self, _sql: str, *_args: object) -> str:
        return "contacting"

    async def fetchrow(self, _sql: str, *_args: object) -> dict[str, object]:
        return self.row


class _RefreshConnection(_Connection):
    def __init__(self, *, opted_out: bool = False) -> None:
        super().__init__(expired=False, opted_out=opted_out)
        self.offer_id = uuid4()
        self.item_id = uuid4()
        self.row.update({
            "payload": {
                **self.row["payload"],
                "logical_action_id": f"initial-availability:{self.offer_id}",
                "text": "Здравствуйте! Есть ли Плитка, количество — 40.000000 Штука?",
            },
            "request_item_id": self.item_id,
            "item_name": "Плитка",
            "specification_text": (
                "Плита для потолка АРМСТРОНГ. Потолочная панель декоративная "
                "тепло- и звукоизоляционная, 60 x 60 см."
            ),
            "quantity": Decimal("40.000000"),
            "unit": "Штука",
            "offer_id": self.offer_id,
            "supplier_name": "Поставщик",
        })
        self.expired_ids: list[UUID] = []

    async def fetchrow(self, sql: str, *_args: object) -> dict[str, object]:
        if "JOIN users AS owner" in sql:
            return {"status": "contacting", "owner_user_id": self.owner_id}
        if "FROM action_approvals approval" in sql:
            return self.row
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: object) -> str:
        assert "UPDATE action_approvals SET status='expired'" in sql
        self.expired_ids.append(args[0])  # type: ignore[arg-type]
        return "UPDATE 1"


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


@pytest.mark.asyncio
async def test_refresh_expires_old_text_and_returns_a_new_unapproved_draft(monkeypatch) -> None:
    db = _RefreshConnection()
    replacement_id = uuid4()
    requested: list[dict[str, object]] = []

    async def request(_db: object, **kwargs: object) -> UUID:
        requested.append(kwargs)
        return replacement_id

    monkeypatch.setattr(batch, "request_approval", request)
    result = await batch.refresh_initial_outreach_drafts(
        db, case_id=db.case_id, owner_user_id=db.owner_id,
        approval_ids=[db.approval_id], now=datetime.now(UTC),
    )

    assert result[0].approval_id == replacement_id
    assert result[0].item_name == "Плитка"
    assert result[0].text.startswith("Здравствуйте! Подскажите, пожалуйста, есть ли в наличии: "
                                    "потолочная панель Armstrong, количество — 40 шт.")
    assert "тепло- и звукоизоляционная" not in result[0].text
    assert "60 x 60 см" not in result[0].text
    assert db.expired_ids == [db.approval_id]
    assert requested[0]["payload"]["text"] == result[0].text  # type: ignore[index]
    assert requested[0]["payload"]["logical_action_id"] == (  # type: ignore[index]
        f"initial-availability:{db.offer_id}"
    )


@pytest.mark.asyncio
async def test_refresh_rejects_opted_out_conversation(monkeypatch) -> None:
    db = _RefreshConnection(opted_out=True)

    async def unexpected(*_args: object, **_kwargs: object) -> UUID:
        raise AssertionError("an opted-out conversation must not be refreshed")

    monkeypatch.setattr(batch, "request_approval", unexpected)
    with pytest.raises(batch.OutreachBatchConflict, match="Контакт или диалог"):
        await batch.refresh_initial_outreach_drafts(
            db, case_id=db.case_id, owner_user_id=db.owner_id,
            approval_ids=[db.approval_id], now=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_legacy_generic_text_cannot_be_approved_as_the_current_draft(monkeypatch) -> None:
    db = _Connection(expired=False)
    db.row["payload"]["text"] = "Здравствуйте! Есть ли Плитка, количество — 40 Штука?"

    async def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("stale text must not be approved")

    monkeypatch.setattr(batch, "approve_request", unexpected)
    with pytest.raises(batch.OutreachBatchConflict, match="Старый текст черновика"):
        await batch.approve_initial_outreach_batch(
            db, case_id=db.case_id, owner_user_id=db.owner_id,
            approval_ids=[db.approval_id], now=db.now,
        )


@pytest.mark.asyncio
async def test_loaded_drafts_mark_stale_text_for_explicit_refresh() -> None:
    class _LoadConnection:
        async def fetch(self, _sql: str, *_args: object):
            return [{
                "approval_id": uuid4(), "text": "Здравствуйте! Есть ли Плитка?",
                "item_name": "Плитка",
                "specification_text": "Плита для потолка АРМСТРОНГ.",
                "quantity": Decimal("40"), "unit": "Штука",
            }]

    result = await batch.load_initial_outreach_drafts(
        _LoadConnection(), uuid4(),  # type: ignore[arg-type]
    )
    assert result[0]["needs_refresh"] is True
