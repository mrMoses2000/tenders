from __future__ import annotations

import pytest

import scripts.postgres_test_safety as safety
from scripts.postgres_test_safety import (
    UnsafeTestDatabaseError,
    configured_postgres_target,
    parse_test_postgres_dsn,
    verify_postgres_test_connection,
)


@pytest.mark.parametrize(
    ("dsn", "database", "host", "port", "socket_path"),
    [
        (
            "postgresql:///procurement_bot_test",
            "procurement_bot_test",
            None,
            None,
            None,
        ),
        (
            "postgresql://tenders_test@127.0.0.1:55432/tenders_revision",
            "tenders_revision",
            "127.0.0.1",
            55432,
            None,
        ),
        (
            "postgresql:///tenders_test?host=%2Fvar%2Frun%2Fpostgresql",
            "tenders_test",
            None,
            None,
            "/var/run/postgresql",
        ),
    ],
)
def test_accepts_documented_local_test_database_dsns(
    dsn: str,
    database: str,
    host: str | None,
    port: int | None,
    socket_path: str | None,
) -> None:
    target = parse_test_postgres_dsn(dsn)

    assert target.database == database
    assert target.host == host
    assert target.port == port
    assert target.socket_path == socket_path


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://owner@prod-db.example.com/procurement_bot_test",
        "postgresql://owner@127.0.0.1/procurement_bot",
        "postgresql://owner@127.0.0.1/prod",
        "postgresql://owner@127.0.0.1/procurement_bot_test?hostaddr=203.0.113.8",
        "postgresql://owner@127.0.0.1/procurement_bot_test?service=production",
        "postgresql://owner@127.0.0.1,10.0.0.1/procurement_bot_test",
    ],
)
def test_rejects_remote_or_non_test_database_dsns(dsn: str) -> None:
    with pytest.raises(UnsafeTestDatabaseError):
        parse_test_postgres_dsn(dsn)


def test_expected_database_and_port_can_be_pinned_by_ci() -> None:
    dsn = "postgresql://owner@127.0.0.1:55439/procurement_bot_test"

    target = parse_test_postgres_dsn(
        dsn,
        expected_database="procurement_bot_test",
        expected_port=55439,
    )

    assert target.database == "procurement_bot_test"
    with pytest.raises(UnsafeTestDatabaseError, match="EXPECTED_DATABASE"):
        parse_test_postgres_dsn(dsn, expected_database="production")
    with pytest.raises(UnsafeTestDatabaseError, match="EXPECTED_PORT"):
        parse_test_postgres_dsn(dsn, expected_port=5432)


def test_configured_target_reads_optional_exact_database_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_POSTGRES_EXPECTED_DATABASE", "tenders_revision")
    monkeypatch.setenv("TEST_POSTGRES_EXPECTED_PORT", "55432")

    target = configured_postgres_target(
        "postgresql://owner@127.0.0.1:55432/tenders_revision"
    )

    assert target.database == "tenders_revision"
    assert target.port == 55432


class _FakeConnection:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.closed = False
        self.query = ""

    async def fetchrow(self, query: str) -> dict[str, object]:
        self.query = query
        return self.row

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            {
                "database": "production",
                "server_address": "127.0.0.1",
                "server_port": 55439,
                "in_recovery": False,
            },
            "database does not match",
        ),
        (
            {
                "database": "procurement_bot_test",
                "server_address": "10.1.2.3",
                "server_port": 55439,
                "in_recovery": False,
            },
            "not on a loopback",
        ),
        (
            {
                "database": "procurement_bot_test",
                "server_address": "127.0.0.1",
                "server_port": 5432,
                "in_recovery": False,
            },
            "port does not match",
        ),
        (
            {
                "database": "procurement_bot_test",
                "server_address": "127.0.0.1",
                "server_port": 55439,
                "in_recovery": True,
            },
            "read-only standby",
        ),
    ],
)
async def test_connection_metadata_must_match_safe_target(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, object],
    message: str,
) -> None:
    connection = _FakeConnection(row)

    async def connect(_dsn: str, **kwargs: object) -> _FakeConnection:
        assert kwargs["timeout"] == 5
        return connection

    monkeypatch.setattr(safety.asyncpg, "connect", connect)
    dsn = "postgresql://owner@127.0.0.1:55439/procurement_bot_test"
    target = parse_test_postgres_dsn(dsn)

    with pytest.raises(UnsafeTestDatabaseError, match=message):
        await verify_postgres_test_connection(dsn, target)

    assert connection.closed
    assert "inet_server_addr" in connection.query


@pytest.mark.asyncio
async def test_connection_metadata_accepts_the_verified_loopback_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        {
            "database": "procurement_bot_test",
            "server_address": "127.0.0.1",
            "server_port": 55439,
            "in_recovery": False,
        }
    )

    async def connect(_dsn: str, **kwargs: object) -> _FakeConnection:
        assert kwargs["timeout"] == 5
        return connection

    monkeypatch.setattr(safety.asyncpg, "connect", connect)
    dsn = "postgresql://owner@127.0.0.1:55439/procurement_bot_test"
    target = parse_test_postgres_dsn(dsn)

    metadata = await verify_postgres_test_connection(dsn, target)

    assert metadata["database"] == "procurement_bot_test"
    assert connection.closed


@pytest.mark.asyncio
async def test_socket_target_requires_socket_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        {
            "database": "procurement_bot_test",
            "server_address": "127.0.0.1",
            "server_port": 55439,
            "in_recovery": False,
        }
    )

    async def connect(_dsn: str, **kwargs: object) -> _FakeConnection:
        assert kwargs["timeout"] == 5
        return connection

    monkeypatch.setattr(safety.asyncpg, "connect", connect)
    dsn = "postgresql:///procurement_bot_test"
    target = parse_test_postgres_dsn(dsn)

    with pytest.raises(UnsafeTestDatabaseError, match="socket"):
        await verify_postgres_test_connection(dsn, target)

    assert connection.closed
