from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest

from procurement_bot.mini_app import (
    add_item_requirement,
    parse_item_requirement,
    refresh_outreach_drafts,
    validate_public_url,
    verify_init_data,
)


def _signed_data(token: str, *, auth_date: int, user_id: int) -> str:
    values = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id})}
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def test_signed_fresh_telegram_init_data_is_accepted() -> None:
    assert verify_init_data(_signed_data("test-token", auth_date=1000, user_id=42),
                            "test-token", now=1200) == 42


def test_tampered_or_expired_init_data_is_rejected() -> None:
    valid = _signed_data("test-token", auth_date=1000, user_id=42)
    assert verify_init_data(valid, "other-token", now=1200) is None
    assert verify_init_data(valid.replace("42", "43"), "test-token", now=1200) is None
    assert verify_init_data(valid, "test-token", now=1000 + 24 * 3600 + 1) is None
    assert verify_init_data(valid + "&hash=duplicate", "test-token", now=1200) is None
    assert verify_init_data("", "test-token", now=1200) is None


def test_public_url_requires_https_and_normalizes_path() -> None:
    assert validate_public_url("https://example.com/miniapp") == "https://example.com/miniapp/"
    with pytest.raises(ValueError):
        validate_public_url("http://example.com/miniapp")
    with pytest.raises(ValueError):
        validate_public_url("https://example.com/miniapp?token=unsafe")


def test_item_requirement_requires_an_explicit_source_and_valid_fields() -> None:
    requirement = parse_item_requirement({
        "requirement_key": "Диаметр трубы",
        "expected_value": "20",
        "unit": "мм",
        "operator": "minimum",
        "is_hard": True,
        "source_pointer": "ТЗ, пункт 3.2",
    })
    assert requirement == {
        "requirement_key": "Диаметр трубы",
        "expected_value": {"value": "20", "unit": "мм"},
        "operator": "minimum",
        "is_hard": True,
        "source_pointer": "ТЗ, пункт 3.2",
    }
    with pytest.raises(ValueError, match="источник"):
        parse_item_requirement({
            "requirement_key": "Диаметр трубы", "expected_value": "20",
        })
    with pytest.raises(ValueError, match="оператор"):
        parse_item_requirement({
            "requirement_key": "Диаметр трубы", "expected_value": "20",
            "source_pointer": "ТЗ, пункт 3.2", "operator": "delete",
        })


class _RequirementRequest:
    def __init__(self, body, *, item_exists=True):
        self.match_info = {"case_id": str(uuid4()), "item_id": str(uuid4())}
        self.app = {"pool": _RequirementPool(item_exists=item_exists)}
        self.owner_id = uuid4()
        self.body = body

    def __getitem__(self, key):
        if key == "owner_id":
            return self.owner_id
        raise KeyError(key)

    async def json(self):
        return self.body


class _RequirementPool:
    def __init__(self, *, item_exists):
        self.connection = _RequirementConnection(item_exists=item_exists)

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class _RequirementConnection:
    def __init__(self, *, item_exists):
        self.item_exists = item_exists
        self.inserted_requirement = False
        self.queries = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "FROM request_items i" in query:
            return {"id": args[0]} if self.item_exists else None
        if "FROM workflow_events" in query:
            return None
        raise AssertionError(f"unexpected fetchrow query: {query}")

    async def fetchval(self, query, *args):
        self.queries.append(query)
        if "INSERT INTO item_requirements" in query:
            self.inserted_requirement = True
            return uuid4()
        raise AssertionError(f"unexpected fetchval query: {query}")

    async def execute(self, query, *args):
        self.queries.append(query)
        assert "INSERT INTO workflow_events" in query
        assert args[4]["requirement_id"]


@pytest.mark.asyncio
async def test_item_requirement_route_is_owner_scoped_and_additive(monkeypatch) -> None:
    import procurement_bot.mini_app as mini_app

    async def no_job(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mini_app, "enqueue_job", no_job)
    request = _RequirementRequest({
        "idempotency_key": str(uuid4()), "requirement_key": "Диаметр",
        "expected_value": "20", "unit": "мм", "source_pointer": "ТЗ, пункт 3",
    })
    response = await add_item_requirement(request)
    result = json.loads(response.text)
    assert response.status == 201
    assert UUID(result["id"])
    connection = request.app["pool"].connection
    assert connection.inserted_requirement
    assert all("UPDATE item_requirements" not in query for query in connection.queries)
    assert all("DELETE FROM item_requirements" not in query for query in connection.queries)

    foreign_request = _RequirementRequest({
        "idempotency_key": str(uuid4()), "requirement_key": "Диаметр",
        "expected_value": "20", "source_pointer": "ТЗ, пункт 3",
    }, item_exists=False)
    foreign_response = await add_item_requirement(foreign_request)
    assert foreign_response.status == 400
    assert not foreign_request.app["pool"].connection.inserted_requirement


@pytest.mark.asyncio
async def test_refresh_outreach_route_returns_unapproved_replacement(monkeypatch) -> None:
    import procurement_bot.mini_app as mini_app
    from procurement_bot.outreach_batch import RefreshedOutreachDraft

    approval_id = uuid4()
    request = _RequirementRequest({"approval_ids": [str(approval_id)]})
    seen = {}

    async def refresh(_connection, **kwargs):
        seen.update(kwargs)
        return [RefreshedOutreachDraft(
            approval_id=uuid4(), request_item_id=uuid4(), item_name="Плитка",
            supplier_name="Магазин", recipient="+77000000000", text="Точный текст",
        )]

    monkeypatch.setattr(mini_app, "refresh_initial_outreach_drafts", refresh)
    response = await refresh_outreach_drafts(request)
    payload = json.loads(response.text)
    assert response.status == 200
    assert seen["case_id"] == UUID(request.match_info["case_id"])
    assert seen["owner_user_id"] == request.owner_id
    assert seen["approval_ids"] == [approval_id]
    assert payload["drafts"][0]["text"] == "Точный текст"
