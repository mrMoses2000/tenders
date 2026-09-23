from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from procurement_bot.outbox import TelegramOutboxWorker, WhatsAppOutboxWorker
from procurement_bot.providers.waha import WahaSendResult
from procurement_bot.queue import ClaimedOutbox


def _event(event_type: str, payload: dict[str, Any]) -> ClaimedOutbox:
    return ClaimedOutbox(
        id=uuid4(),
        event_type=event_type,
        payload=payload,
        attempts=1,
        max_attempts=3,
        idempotency_key=f"event:{uuid4().hex}",
        locked_by="test-worker",
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
        approval_id=uuid4() if event_type == "whatsapp.send_text" else None,
        provider_request_id="reserved-42" if event_type == "whatsapp.send_text" else None,
    )


class FakeBot:
    async def send_message(self, **_kwargs: object) -> object:
        return type("Sent", (), {"message_id": 12})()

    async def answer_callback_query(self, **_kwargs: object) -> bool:
        return True

    async def send_document(self, **_kwargs: object) -> object:
        return type("Sent", (), {"message_id": 13})()


class FakeWahaClient:
    async def send_text(self, **_kwargs: object) -> WahaSendResult:
        return WahaSendResult(external_message_id="wa-12", raw={"id": "wa-12"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_kind", "expected_types"),
    [
        (
            "telegram",
            (
                "telegram.send_message",
                "telegram.answer_callback_query",
                "telegram.send_document",
            ),
        ),
        ("whatsapp", ("whatsapp.send_text",)),
    ],
)
async def test_outbox_workers_claim_only_their_own_channel(
    monkeypatch: pytest.MonkeyPatch,
    worker_kind: str,
    expected_types: tuple[str, ...],
) -> None:
    captured: dict[str, object] = {}

    async def fake_claim(_pool: object, **kwargs: object) -> None:
        captured.update(kwargs)
        return None

    monkeypatch.setattr("procurement_bot.outbox.claim_outbox", fake_claim)
    if worker_kind == "telegram":
        worker = TelegramOutboxWorker(
            pool=object(),  # type: ignore[arg-type]
            bot=FakeBot(),  # type: ignore[arg-type]
            worker_id="telegram-worker",
        )
    else:
        worker = WhatsAppOutboxWorker(
            pool=object(),  # type: ignore[arg-type]
            client=FakeWahaClient(),  # type: ignore[arg-type]
            worker_id="whatsapp-worker",
        )

    assert not await worker.run_once()
    assert captured["event_types"] == expected_types


@pytest.mark.asyncio
async def test_whatsapp_worker_passes_outbox_key_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event(
        "whatsapp.send_text",
        {
            "case_id": str(uuid4()),
            "channel": "whatsapp",
            "recipient": "+77001234567",
            "text": "Здравствуйте",
            "conversation_id": str(uuid4()),
            "logical_action_id": "availability-v1",
            "is_followup": False,
        },
    )
    calls: dict[str, object] = {}

    class RecordingClient:
        async def send_text(self, **kwargs: object) -> WahaSendResult:
            calls.update(kwargs)
            return WahaSendResult(external_message_id="wa-42", raw={"id": "wa-42"})

    async def fake_claim(*_args: object, **_kwargs: object) -> ClaimedOutbox:
        return event

    async def fake_allowed(*_args: object, **_kwargs: object) -> object:
        return object()

    async def fake_complete(*args: object, **_kwargs: object) -> bool:
        result = args[3]
        calls["external_id"] = result.external_message_id
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_outbox", fake_claim)
    monkeypatch.setattr(
        "procurement_bot.outbox.assert_whatsapp_delivery_allowed",
        fake_allowed,
    )
    monkeypatch.setattr(
        "procurement_bot.outbox.complete_whatsapp_delivery",
        fake_complete,
    )

    async def fake_validate(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("procurement_bot.outbox.validate_consumed_approval", fake_validate)
    worker = WhatsAppOutboxWorker(
        pool=object(),  # type: ignore[arg-type]
        client=RecordingClient(),  # type: ignore[arg-type]
        worker_id="whatsapp-worker",
    )

    assert await worker.run_once()
    assert calls == {
        "recipient": "+77001234567",
        "text": "Здравствуйте",
        "idempotency_key": event.idempotency_key,
        "provider_message_id": "reserved-42",
        "external_id": "wa-42",
    }


@pytest.mark.asyncio
async def test_telegram_worker_resolves_and_sends_private_report_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = tmp_path / "reports" / "aa" / "report.html"
    report.parent.mkdir(parents=True)
    report.write_text("<html>report</html>", encoding="utf-8")
    event = _event(
        "telegram.send_document",
        {
            "chat_id": 123,
            "storage_path": "reports/aa/report.html",
            "filename": "procurement-report.html",
            "caption": "Отчёт готов",
        },
    )
    captured: dict[str, object] = {}

    class RecordingBot(FakeBot):
        async def send_document(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return type("Sent", (), {"message_id": 44})()

    async def claim(*_args: object, **_kwargs: object) -> ClaimedOutbox:
        return event

    async def complete(*_args: object, **kwargs: object) -> bool:
        captured["external_id"] = kwargs["external_id"]
        return True

    monkeypatch.setattr("procurement_bot.outbox.claim_outbox", claim)
    monkeypatch.setattr("procurement_bot.outbox.complete_outbox", complete)
    worker = TelegramOutboxWorker(
        pool=object(),  # type: ignore[arg-type]
        bot=RecordingBot(),  # type: ignore[arg-type]
        worker_id="telegram-worker",
        results_root=tmp_path,
    )

    assert await worker.run_once()
    assert captured["chat_id"] == 123
    assert captured["caption"] == "Отчёт готов"
    assert Path(captured["document"].path) == report  # type: ignore[union-attr]
    assert captured["external_id"] == "44"
