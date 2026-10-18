from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from procurement_bot.transport_recovery import reconcile_lid_quarantine


class _Connection:
    def __init__(self, phone: str, quarantine_row: dict[str, object]) -> None:
        self.phone = phone
        self.quarantine_row = quarantine_row

    async def fetchval(self, sql: str, *_args: object) -> object:
        if "SELECT EXISTS" in sql:
            return True
        if "array_agg" in sql:
            return [self.phone]
        raise AssertionError(sql)

    async def fetch(self, sql: str, *_args: object) -> list[dict[str, object]]:
        assert "whatsapp_inbound_quarantine" in sql
        return [self.quarantine_row]


class _Acquire(AbstractAsyncContextManager[_Connection]):
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


class _MappingClient:
    session_name = "default"

    def __init__(self, *, full_page_count: int, target_page: int) -> None:
        self.full_page_count = full_page_count
        self.target_page = target_page
        self.offsets: list[int] = []

    async def fetch_lid_mappings(self, *, limit: int, offset: int) -> list[dict[str, str]]:
        assert limit == 100
        self.offsets.append(offset)
        page_number = offset // 100
        if page_number >= self.full_page_count:
            if page_number == self.target_page:
                return [
                    {"lid": "known-supplier@lid", "pn": "77000000003@c.us"},
                ]
            return []

        rows = [
            {"lid": f"filler-{offset + index}@lid", "pn": "77000000009@c.us"}
            for index in range(100)
        ]
        if page_number == self.target_page:
            rows[0] = {"lid": "known-supplier@lid", "pn": "77000000003@c.us"}
        return rows


def _fixture() -> tuple[_Pool, dict[str, object], object]:
    case_id = uuid4()
    quarantine_row: dict[str, object] = {
        "id": uuid4(),
        "session_name": "default",
        "provider_event_id": "provider-event-1",
        "sender_jid": "known-supplier@lid",
        "received_at": datetime(2026, 10, 6, 9, tzinfo=UTC),
        "message_kind": "text",
        "text_content": "supplier reply",
    }
    return _Pool(_Connection("+77000000003", quarantine_row)), quarantine_row, case_id


@pytest.mark.asyncio
async def test_lid_mapping_on_fourth_page_allows_case_scoped_text_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import procurement_bot.whatsapp_ingress as ingress

    pool, _row, case_id = _fixture()
    client = _MappingClient(full_page_count=3, target_page=3)
    resolutions: list[dict[str, object]] = []

    async def resolve(_connection, _quarantine_id, **kwargs):
        resolutions.append(kwargs)
        return SimpleNamespace(resolved=kwargs["mapping_snapshot_complete"])

    monkeypatch.setattr(ingress, "resolve_lid_quarantine", resolve)

    resolved = await reconcile_lid_quarantine(pool, client, case_id)  # type: ignore[arg-type]

    assert resolved == 1
    assert client.offsets == [0, 100, 200, 300]
    assert len(resolutions) == 1
    resolution = resolutions[0]
    assert resolution["mapping_snapshot_complete"] is True
    assert resolution["case_id"] == case_id
    mappings = resolution["mappings"]
    assert len(mappings) == 301  # type: ignore[arg-type]
    assert sum(mapping.lid == "known-supplier@lid" for mapping in mappings) == 1


@pytest.mark.asyncio
async def test_ten_full_lid_pages_remain_incomplete_and_do_not_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import procurement_bot.whatsapp_ingress as ingress

    pool, _row, case_id = _fixture()
    client = _MappingClient(full_page_count=10, target_page=9)
    resolutions: list[dict[str, object]] = []

    async def resolve(_connection, _quarantine_id, **kwargs):
        resolutions.append(kwargs)
        return SimpleNamespace(resolved=kwargs["mapping_snapshot_complete"])

    monkeypatch.setattr(ingress, "resolve_lid_quarantine", resolve)

    resolved = await reconcile_lid_quarantine(pool, client, case_id)  # type: ignore[arg-type]

    assert resolved == 0
    assert client.offsets == list(range(0, 1000, 100))
    assert len(resolutions) == 1
    assert resolutions[0]["mapping_snapshot_complete"] is False
    assert resolutions[0]["case_id"] == case_id
