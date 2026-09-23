from __future__ import annotations

import asyncpg
import pytest

from procurement_bot.telegram_access import (
    TelegramAccessPolicy,
    normalize_telegram_username,
)


class FakeConnection:
    def __init__(
        self,
        *,
        bound_id: int | None = None,
        already_authorized: bool = False,
        conflict: bool = False,
    ) -> None:
        self.bound_id = bound_id
        self.already_authorized = already_authorized
        self.conflict = conflict
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, sql: str, *args: object):
        self.calls.append((sql, args))
        if "SELECT EXISTS" in sql:
            return self.already_authorized
        if "SELECT telegram_id" in sql:
            return self.bound_id
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        if "INSERT INTO telegram_access_grants" in sql and self.conflict:
            raise asyncpg.UniqueViolationError("collision")
        return "INSERT 0 1"


def test_username_normalization_is_strict() -> None:
    assert normalize_telegram_username(" @JustMoses0002 ") == "justmoses0002"
    assert normalize_telegram_username("bad-name") is None
    assert normalize_telegram_username("x") is None


@pytest.mark.asyncio
async def test_configured_username_is_bound_once() -> None:
    connection = FakeConnection()
    policy = TelegramAccessPolicy(
        allowed_user_ids=set(),
        bootstrap_usernames={"@JustMoses0002"},
    )

    assert await policy.authorize(
        connection, telegram_id=7001, username="JustMoses0002"
    )
    insert = next(call for call in connection.calls if "INSERT INTO" in call[0])
    assert insert[1] == ("justmoses0002", 7001)


@pytest.mark.asyncio
async def test_bound_username_cannot_be_claimed_by_another_id() -> None:
    connection = FakeConnection(bound_id=7001)
    policy = TelegramAccessPolicy(
        allowed_user_ids=set(),
        bootstrap_usernames={"JustMoses0002"},
    )

    assert not await policy.authorize(
        connection, telegram_id=7002, username="JustMoses0002"
    )
    assert not any("INSERT INTO" in sql for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_previously_bound_id_does_not_depend_on_current_username() -> None:
    policy = TelegramAccessPolicy(
        allowed_user_ids=set(),
        bootstrap_usernames={"JustMoses0002"},
    )
    assert await policy.authorize(
        FakeConnection(already_authorized=True),
        telegram_id=7001,
        username="renamed_owner",
    )


@pytest.mark.asyncio
async def test_unconfigured_or_wrong_username_fails_closed() -> None:
    policy = TelegramAccessPolicy(
        allowed_user_ids=set(),
        bootstrap_usernames={"JustMoses0002"},
    )
    assert not await policy.authorize(
        FakeConnection(), telegram_id=7001, username="someone_else"
    )


@pytest.mark.asyncio
async def test_username_is_never_bootstrapped_from_a_group_message() -> None:
    connection = FakeConnection()
    policy = TelegramAccessPolicy(
        allowed_user_ids=set(),
        bootstrap_usernames={"JustMoses0002"},
    )
    assert not await policy.authorize(
        connection,
        telegram_id=7001,
        username="JustMoses0002",
        permit_bootstrap=False,
    )
    assert not any("INSERT INTO" in sql for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_static_numeric_allowlist_does_not_touch_database() -> None:
    connection = FakeConnection()
    policy = TelegramAccessPolicy(allowed_user_ids={7001})
    assert await policy.authorize(connection, telegram_id=7001, username=None)
    assert connection.calls == []
