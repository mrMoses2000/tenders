"""Refuse PostgreSQL integration targets that are not clearly disposable and local."""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

import asyncpg


class UnsafeTestDatabaseError(ValueError):
    """The configured integration-test database is not an isolated local target."""


@dataclass(frozen=True)
class PostgresTestTarget:
    database: str
    host: str | None
    port: int | None
    socket_path: str | None


def _is_test_database(database: str) -> bool:
    normalized = database.casefold()
    return (
        normalized == "tenders_revision"
        or normalized.startswith("test_")
        or normalized.endswith("_test")
        or normalized.endswith("_tests")
    )


def _one_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name, [])
    if len(values) > 1:
        raise UnsafeTestDatabaseError(f"TEST_POSTGRES_DSN has multiple {name} values")
    return values[0] if values else None


def parse_test_postgres_dsn(
    dsn: str,
    *,
    expected_database: str | None = None,
    expected_port: int | None = None,
) -> PostgresTestTarget:
    """Parse a PostgreSQL URI and reject remote or non-test database targets."""

    try:
        parsed = urlsplit(dsn)
        query = parse_qs(parsed.query, keep_blank_values=True)
        uri_host = parsed.hostname
        uri_port = parsed.port
    except ValueError as exc:
        raise UnsafeTestDatabaseError("TEST_POSTGRES_DSN is malformed") from exc

    if parsed.scheme not in {"postgres", "postgresql"} or parsed.fragment:
        raise UnsafeTestDatabaseError("TEST_POSTGRES_DSN must be a PostgreSQL URI")
    if any(key in query for key in ("hostaddr", "service", "dbname")):
        raise UnsafeTestDatabaseError(
            "TEST_POSTGRES_DSN may not override host or database through URI options"
        )

    path = unquote(parsed.path)
    if not path.startswith("/") or path.count("/") != 1:
        raise UnsafeTestDatabaseError("TEST_POSTGRES_DSN must name exactly one database")
    database = path[1:]
    if not database or not _is_test_database(database):
        raise UnsafeTestDatabaseError(
            "TEST_POSTGRES_DSN database must end in _test/_tests, start with test_, "
            "or be the documented tenders_revision database"
        )
    if expected_database and database != expected_database:
        raise UnsafeTestDatabaseError(
            "TEST_POSTGRES_DSN database does not match TEST_POSTGRES_EXPECTED_DATABASE"
        )

    query_host = _one_value(query, "host")
    if uri_host and query_host:
        raise UnsafeTestDatabaseError("TEST_POSTGRES_DSN may specify host only once")
    host_value = query_host if query_host is not None else uri_host
    socket_path: str | None = None
    host: str | None = None
    if host_value:
        if host_value.startswith("/"):
            socket = PurePosixPath(host_value)
            if ".." in socket.parts:
                raise UnsafeTestDatabaseError("TEST_POSTGRES_DSN socket path may not traverse")
            socket_path = str(socket)
        else:
            host = host_value.casefold().rstrip(".")
            if host != "localhost":
                try:
                    if not ip_address(host).is_loopback:
                        raise UnsafeTestDatabaseError(
                            "TEST_POSTGRES_DSN host must resolve to a local loopback address"
                        )
                except ValueError as exc:
                    if isinstance(exc, UnsafeTestDatabaseError):
                        raise
                    raise UnsafeTestDatabaseError(
                        "TEST_POSTGRES_DSN host must be localhost or a loopback IP"
                    ) from exc

    query_port = _one_value(query, "port")
    if uri_port is not None and query_port is not None:
        raise UnsafeTestDatabaseError("TEST_POSTGRES_DSN may specify port only once")
    try:
        port = uri_port if uri_port is not None else int(query_port) if query_port else None
    except ValueError as exc:
        raise UnsafeTestDatabaseError("TEST_POSTGRES_DSN port is malformed") from exc
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeTestDatabaseError("TEST_POSTGRES_DSN port is outside the valid range")
    if expected_port is not None and port != expected_port:
        raise UnsafeTestDatabaseError(
            "TEST_POSTGRES_DSN port does not match TEST_POSTGRES_EXPECTED_PORT"
        )

    return PostgresTestTarget(
        database=database,
        host=host,
        port=port,
        socket_path=socket_path,
    )


def configured_postgres_target(dsn: str) -> PostgresTestTarget:
    expected_database = os.getenv("TEST_POSTGRES_EXPECTED_DATABASE") or None
    expected_port_value = os.getenv("TEST_POSTGRES_EXPECTED_PORT")
    try:
        expected_port = int(expected_port_value) if expected_port_value else None
    except ValueError as exc:
        raise UnsafeTestDatabaseError("TEST_POSTGRES_EXPECTED_PORT must be an integer") from exc
    return parse_test_postgres_dsn(
        dsn,
        expected_database=expected_database,
        expected_port=expected_port,
    )


async def verify_postgres_test_connection(
    dsn: str,
    target: PostgresTestTarget,
) -> dict[str, object]:
    """Connect only after URI checks, then verify the server's actual endpoint."""

    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        row = await connection.fetchrow(
            """SELECT current_database() AS database,
                      host(inet_server_addr()) AS server_address,
                      inet_server_port() AS server_port,
                      pg_is_in_recovery() AS in_recovery"""
        )
    finally:
        await connection.close()

    if row["database"] != target.database:
        raise UnsafeTestDatabaseError(
            "Connected PostgreSQL database does not match TEST_POSTGRES_DSN"
        )
    address = row["server_address"]
    if address is None:
        if target.host is not None:
            raise UnsafeTestDatabaseError(
                "PostgreSQL used a Unix socket for a network TEST_POSTGRES_DSN"
            )
    else:
        if target.host is None:
            raise UnsafeTestDatabaseError(
                "PostgreSQL used a network connection for a socket TEST_POSTGRES_DSN"
            )
        try:
            if not ip_address(address).is_loopback:
                raise UnsafeTestDatabaseError(
                    "Connected PostgreSQL server is not on a loopback interface"
                )
        except ValueError as exc:
            if isinstance(exc, UnsafeTestDatabaseError):
                raise
            raise UnsafeTestDatabaseError(
                "PostgreSQL returned an invalid server address"
            ) from exc
    if target.port is not None and row["server_port"] != target.port:
        raise UnsafeTestDatabaseError(
            "Connected PostgreSQL server port does not match TEST_POSTGRES_DSN"
        )
    if row["in_recovery"]:
        raise UnsafeTestDatabaseError("PostgreSQL test target is a read-only standby")
    return dict(row)
