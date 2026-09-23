from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

import procurement_bot.outbox as outbox_module
import procurement_bot.worker as worker_module
from procurement_bot.outbox import TelegramOutboxWorker
from procurement_bot.queue import ClaimedJob, ClaimedOutbox
from procurement_bot.telegram import TelegramIngress
from procurement_bot.worker import JobWorker


@pytest.mark.asyncio
async def test_telegram_ingress_routes_callback_before_message_path() -> None:
    update = SimpleNamespace(
        update_id=701,
        callback_query=SimpleNamespace(id="callback-701"),
        message=SimpleNamespace(chat=SimpleNamespace(id=42)),
    )
    routed: list[object] = []

    class CallbackIngressSpy:
        async def accept(self, candidate: object) -> bool:
            routed.append(candidate)
            return False

    ingress = TelegramIngress(
        pool=object(),  # type: ignore[arg-type]
        allowed_user_ids={9},
    )
    ingress._callback_ingress = CallbackIngressSpy()  # type: ignore[assignment]

    accepted = await ingress.accept(update)  # type: ignore[arg-type]

    assert accepted is False
    assert routed == [update]


@pytest.mark.asyncio
async def test_telegram_outbox_delivers_callback_answer_and_completes_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = ClaimedOutbox(
        id=uuid4(),
        event_type="telegram.answer_callback_query",
        payload={
            "callback_query_id": "callback-702",
            "text": "Поиск запущен.",
            "show_alert": False,
        },
        attempts=1,
        max_attempts=3,
        idempotency_key="callback-answer:702",
        locked_by="telegram-test-worker",
        lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
        approval_id=None,
        provider_request_id=None,
    )
    answers: list[dict[str, object]] = []
    completions: list[tuple[tuple[object, ...], dict[str, object]]] = []
    failures: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class RecordingBot:
        async def send_message(self, **_kwargs: object) -> object:
            raise AssertionError("callback answers must not use send_message")

        async def answer_callback_query(self, **kwargs: object) -> bool:
            answers.append(kwargs)
            return True

    async def claim(*_args: object, **_kwargs: object) -> ClaimedOutbox:
        return event

    async def complete(*args: object, **kwargs: object) -> bool:
        completions.append((args, kwargs))
        return True

    async def fail(*args: object, **kwargs: object) -> bool:
        failures.append((args, kwargs))
        return False

    monkeypatch.setattr(outbox_module, "claim_outbox", claim)
    monkeypatch.setattr(outbox_module, "complete_outbox", complete)
    monkeypatch.setattr(outbox_module, "fail_outbox", fail)
    pool = object()
    worker = TelegramOutboxWorker(
        pool=pool,  # type: ignore[arg-type]
        bot=RecordingBot(),  # type: ignore[arg-type]
        worker_id="telegram-test-worker",
    )

    assert await worker.run_once() is True
    assert answers == [
        {
            "callback_query_id": "callback-702",
            "text": "Поиск запущен.",
            "show_alert": False,
            "cache_time": 0,
        }
    ]
    assert completions == [
        (
            (pool, event.id, "telegram-test-worker"),
            {"external_id": "callback-702"},
        )
    ]
    assert failures == []


@pytest.mark.asyncio
async def test_worker_dispatches_approved_start_then_opaque_research_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_id = uuid4()
    research_run_id = uuid4()
    starts: list[tuple[object, object]] = []
    executions: list[object] = []

    async def start(database: object, *, approval_id: object) -> None:
        starts.append((database, approval_id))

    class RecordingExecutor:
        async def execute(self, run_id: object) -> None:
            executions.append(run_id)

    monkeypatch.setattr(worker_module, "start_approved_research", start)
    pool = object()
    worker = object.__new__(JobWorker)
    worker.pool = pool  # type: ignore[assignment]
    worker.research_executor = RecordingExecutor()

    await worker._dispatch(
        ClaimedJob(
            id=uuid4(),
            kind="start_research",
            payload={"approval_id": str(approval_id)},
            attempts=1,
            max_attempts=3,
            idempotency_key="start-research:703",
            locked_by="job-test-worker",
            lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
        )
    )
    await worker._dispatch(
        ClaimedJob(
            id=uuid4(),
            kind="execute_research",
            payload={"research_run_id": str(research_run_id)},
            attempts=1,
            max_attempts=3,
            idempotency_key="execute-research:703",
            locked_by="job-test-worker",
            lease_expires_at=datetime(2026, 9, 22, 12, tzinfo=UTC),
        )
    )

    assert starts == [(pool, approval_id)]
    assert executions == [research_run_id]
