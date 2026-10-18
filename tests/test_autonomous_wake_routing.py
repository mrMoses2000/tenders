from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

import procurement_bot.telegram as telegram_module
from procurement_bot.telegram import TelegramIngress


class _Connection:
    def __init__(self, cases):
        self.cases = cases
        self.fetchval_calls = 0
        self.fetch_calls = []

    def transaction(self):
        @asynccontextmanager
        async def manager():
            yield

        return manager()

    async def fetchval(self, sql, *_args):
        self.fetchval_calls += 1
        return uuid4()

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self.cases

    async def execute(self, _sql, *_args):
        return "UPDATE 1"


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        @asynccontextmanager
        async def manager():
            yield self.connection

        return manager()


def _update(update_id: int, text: str):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=42, type="private"),
        message_id=update_id,
        date=datetime(2026, 10, 3, tzinfo=UTC),
        text=text,
        caption=None,
        from_user=SimpleNamespace(
            id=7001,
            username="owner",
            first_name="Owner",
            last_name=None,
            language_code="ru",
        ),
        voice=None,
        audio=None,
        document=None,
        photo=None,
        location=None,
    )
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        edited_message=None,
        model_dump=lambda **_: {"update_id": update_id},
    )


@pytest.mark.asyncio
async def test_continue_wakes_the_matching_owner_case(monkeypatch):
    case_id, owner_id = uuid4(), uuid4()
    connection = _Connection([{"case_id": case_id, "owner_user_id": owner_id}])
    wake_calls = []

    async def authorize(*_args, **_kwargs):
        return True

    async def accept_update(*_args, **_kwargs):
        return True

    async def enqueue_job(*_args, **_kwargs):
        raise AssertionError("continue must not enqueue intake")

    async def enqueue_outbox(*_args, **kwargs):
        return uuid4()

    async def wake_pipeline(_connection, **kwargs):
        wake_calls.append(kwargs)
        return True

    monkeypatch.setattr(telegram_module, "accept_update", accept_update)
    monkeypatch.setattr(telegram_module, "enqueue_job", enqueue_job)
    monkeypatch.setattr(telegram_module, "enqueue_outbox", enqueue_outbox)
    monkeypatch.setattr("procurement_bot.pipeline_runtime.wake_pipeline", wake_pipeline)
    ingress = TelegramIngress(pool=_Pool(connection))
    ingress._access.authorize = authorize

    assert await ingress.accept(_update(10, f"/continue {case_id}")) is True
    assert wake_calls == [
        {
            "case_id": case_id,
            "owner_id": owner_id,
            "reason": "telegram:10",
        }
    ]
    sql, args = connection.fetch_calls[0]
    assert "u.telegram_id=$1" in sql
    assert args == (7001,)


@pytest.mark.asyncio
async def test_continue_without_case_id_prompts_when_owner_has_multiple_cases(monkeypatch):
    cases = [
        {"case_id": uuid4(), "owner_user_id": uuid4()},
        {"case_id": uuid4(), "owner_user_id": uuid4()},
    ]
    connection = _Connection(cases)
    outbox = []

    async def authorize(*_args, **_kwargs):
        return True

    async def accept_update(*_args, **_kwargs):
        return True

    async def enqueue_job(*_args, **_kwargs):
        raise AssertionError("continue must not enqueue intake")

    async def enqueue_outbox(_connection, **kwargs):
        outbox.append(kwargs)
        return uuid4()

    async def wake_pipeline(*_args, **_kwargs):
        raise AssertionError("ambiguous continue must not wake a case")

    monkeypatch.setattr(telegram_module, "accept_update", accept_update)
    monkeypatch.setattr(telegram_module, "enqueue_job", enqueue_job)
    monkeypatch.setattr(telegram_module, "enqueue_outbox", enqueue_outbox)
    monkeypatch.setattr("procurement_bot.pipeline_runtime.wake_pipeline", wake_pipeline)
    ingress = TelegramIngress(pool=_Pool(connection))
    ingress._access.authorize = authorize

    assert await ingress.accept(_update(11, "/continue")) is True
    assert "Укажите заявку" in outbox[0]["payload"]["text"]
