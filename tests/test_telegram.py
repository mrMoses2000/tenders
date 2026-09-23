from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

import procurement_bot.telegram as telegram_module
from procurement_bot.telegram import PRIVATE_ONLY_TEXT, WELCOME_TEXT, TelegramIngress


class FakeConnection:
    def __init__(self) -> None:
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []
        self.message_id = uuid4()
        self.attachment_id = uuid4()
        self.user_id = uuid4()

    def transaction(self):
        @asynccontextmanager
        async def manager():
            yield

        return manager()

    async def fetchval(self, sql: str, *args: object):
        self.fetchval_calls.append((sql, args))
        if "incoming_attachments" in sql:
            return self.attachment_id
        if "INSERT INTO users" in sql:
            return self.user_id
        if "SELECT user_id FROM messages" in sql:
            return self.user_id
        return self.message_id

    async def fetchrow(self, sql: str, *_args: object):
        if "UPDATE purchase_evidence_requests" in sql:
            return None
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: object) -> str:
        self.execute_calls.append((sql, args))
        return "UPDATE 1"


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self):
        @asynccontextmanager
        async def manager():
            yield self.connection

        return manager()


def update_with_message(update_id: int, message: object) -> object:
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        edited_message=None,
        model_dump=lambda **_: {"update_id": update_id},
    )


def make_message(*, chat_type: str = "private", text: str | None = None, **media: object):
    defaults = {
        "voice": None,
        "audio": None,
        "document": None,
        "photo": None,
        "location": None,
    }
    defaults.update(media)
    return SimpleNamespace(
        chat=SimpleNamespace(id=42, type=chat_type),
        message_id=7,
        date=datetime(2026, 9, 22, tzinfo=UTC),
        text=text,
        caption=None,
        from_user=SimpleNamespace(
            id=9,
            username="buyer",
            first_name="Maxim",
            last_name=None,
            language_code="ru",
        ),
        **defaults,
    )


@pytest.fixture
def queue_spy(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, list] = {"jobs": [], "outbox": []}

    async def accept(*args, **kwargs):
        return True

    async def job(*args, **kwargs):
        state["jobs"].append(kwargs)
        return uuid4()

    async def outbox(*args, **kwargs):
        state["outbox"].append(kwargs)
        return uuid4()

    monkeypatch.setattr(telegram_module, "accept_update", accept)
    monkeypatch.setattr(telegram_module, "enqueue_job", job)
    monkeypatch.setattr(telegram_module, "enqueue_outbox", outbox)
    return state


@pytest.mark.asyncio
async def test_text_is_stored_and_process_job_is_enqueued(queue_spy) -> None:
    connection = FakeConnection()
    ingress = TelegramIngress(pool=FakePool(connection))

    accepted = await ingress.accept(update_with_message(100, make_message(text="Нужна марля")))

    assert accepted is True
    assert [job["kind"] for job in queue_spy["jobs"]] == ["process_intake"]
    assert "INSERT INTO users" in connection.fetchval_calls[0][0]
    assert "INSERT INTO messages" in connection.fetchval_calls[1][0]
    assert not any("incoming_attachments" in sql for sql, _ in connection.fetchval_calls)


@pytest.mark.asyncio
async def test_start_is_stored_and_replies_without_starting_intake(queue_spy) -> None:
    connection = FakeConnection()
    ingress = TelegramIngress(pool=FakePool(connection))

    accepted = await ingress.accept(update_with_message(105, make_message(text="/start")))

    assert accepted is True
    assert queue_spy["jobs"] == []
    assert queue_spy["outbox"][0]["payload"]["text"] == WELCOME_TEXT
    assert queue_spy["outbox"][0]["idempotency_key"]


@pytest.mark.asyncio
async def test_location_is_processed_as_intake(queue_spy) -> None:
    connection = FakeConnection()
    ingress = TelegramIngress(pool=FakePool(connection))
    location = SimpleNamespace(latitude=43.25, longitude=76.95)

    await ingress.accept(update_with_message(101, make_message(location=location)))

    assert queue_spy["jobs"][0]["payload"]["input_kind"] == "location"


@pytest.mark.asyncio
async def test_document_is_registered_and_download_is_only_enqueued(queue_spy) -> None:
    connection = FakeConnection()
    ingress = TelegramIngress(pool=FakePool(connection))
    document = SimpleNamespace(
        file_id="tg-file",
        file_unique_id="tg-unique",
        file_name="spec.pdf",
        mime_type="application/pdf",
        file_size=500,
    )

    await ingress.accept(update_with_message(102, make_message(document=document)))

    assert len(connection.fetchval_calls) == 4
    assert "incoming_attachments" in connection.fetchval_calls[2][0]
    assert [job["kind"] for job in queue_spy["jobs"]] == ["download_attachment"]
    payload = queue_spy["jobs"][0]["payload"]
    assert payload["incoming_message_id"] == str(connection.message_id)
    assert payload["attachment_id"] == str(connection.attachment_id)
    assert payload["file_id"] == "tg-file"
    assert queue_spy["outbox"][0]["event_type"] == "telegram.send_message"


@pytest.mark.asyncio
async def test_group_is_rejected_without_storing_message(queue_spy) -> None:
    connection = FakeConnection()
    ingress = TelegramIngress(pool=FakePool(connection))

    await ingress.accept(
        update_with_message(103, make_message(chat_type="supergroup", text="request"))
    )

    assert connection.fetchval_calls == []
    assert queue_spy["jobs"] == []
    assert queue_spy["outbox"][0]["payload"]["text"] == PRIVATE_ONLY_TEXT


@pytest.mark.asyncio
async def test_duplicate_update_is_not_routed(monkeypatch: pytest.MonkeyPatch, queue_spy) -> None:
    async def duplicate(*args, **kwargs):
        return False

    monkeypatch.setattr(telegram_module, "accept_update", duplicate)
    connection = FakeConnection()
    ingress = TelegramIngress(pool=FakePool(connection))

    assert await ingress.accept(update_with_message(104, make_message(text="x"))) is False
    assert connection.fetchval_calls == []
    assert queue_spy["jobs"] == []


@pytest.mark.asyncio
async def test_same_chat_updates_are_serialised(monkeypatch: pytest.MonkeyPatch, queue_spy) -> None:
    active = 0
    maximum = 0

    async def accept(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return True

    monkeypatch.setattr(telegram_module, "accept_update", accept)
    connection = FakeConnection()
    ingress = TelegramIngress(pool=FakePool(connection), max_concurrency=4)

    await asyncio.gather(
        ingress.accept(update_with_message(201, make_message(text="a"))),
        ingress.accept(update_with_message(202, make_message(text="b"))),
    )

    assert maximum == 1
