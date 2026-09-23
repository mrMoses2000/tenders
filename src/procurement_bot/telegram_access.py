"""Fail-closed Telegram owner bootstrap and authorization."""

from __future__ import annotations

import re
from collections.abc import Iterable

import asyncpg

_USERNAME = re.compile(r"[a-z0-9_]{5,32}")


def normalize_telegram_username(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().removeprefix("@").casefold()
    if _USERNAME.fullmatch(normalized) is None:
        return None
    return normalized


def normalize_bootstrap_usernames(values: Iterable[str]) -> frozenset[str]:
    normalized: set[str] = set()
    for value in values:
        username = normalize_telegram_username(value)
        if username is None:
            raise ValueError(f"invalid Telegram bootstrap username: {value!r}")
        normalized.add(username)
    return frozenset(normalized)


class TelegramAccessPolicy:
    """Authorize fixed IDs or atomically bind an explicitly configured username."""

    def __init__(
        self,
        *,
        allowed_user_ids: set[int] | frozenset[int] | None,
        bootstrap_usernames: set[str] | frozenset[str] | None = None,
        allow_unconfigured: bool = False,
    ) -> None:
        ids = frozenset(allowed_user_ids or ())
        if any(isinstance(value, bool) or value <= 0 for value in ids):
            raise ValueError("allowed_user_ids must contain positive Telegram IDs")
        self.allowed_user_ids = ids
        self.bootstrap_usernames = normalize_bootstrap_usernames(
            bootstrap_usernames or ()
        )
        self.allow_unconfigured = allow_unconfigured

    async def authorize(
        self,
        connection: asyncpg.Connection,
        *,
        telegram_id: object,
        username: object,
        permit_bootstrap: bool = True,
    ) -> bool:
        if (
            isinstance(telegram_id, bool)
            or not isinstance(telegram_id, int)
            or telegram_id <= 0
        ):
            return False
        if telegram_id in self.allowed_user_ids:
            return True

        if self.allow_unconfigured and not (
            self.allowed_user_ids or self.bootstrap_usernames
        ):
            return True
        if not self.bootstrap_usernames:
            return False

        already_bound = await connection.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM telegram_access_grants
                WHERE telegram_id=$1 AND active=TRUE
            )
            """,
            telegram_id,
        )
        if already_bound is True:
            return True

        if not permit_bootstrap:
            return False

        normalized = normalize_telegram_username(username)
        if normalized is None or normalized not in self.bootstrap_usernames:
            return False

        # A missing-row race cannot be serialized with a row lock. The
        # transaction-scoped advisory lock gives each configured username one
        # globally ordered first binding.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"telegram-owner-bootstrap:{normalized}",
        )
        existing = await connection.fetchval(
            """
            SELECT telegram_id
            FROM telegram_access_grants
            WHERE bootstrap_username=$1 AND active=TRUE
            """,
            normalized,
        )
        if existing is not None:
            return existing == telegram_id

        try:
            await connection.execute(
                """
                INSERT INTO telegram_access_grants(bootstrap_username,telegram_id)
                VALUES ($1,$2)
                """,
                normalized,
                telegram_id,
            )
        except asyncpg.UniqueViolationError:
            return False
        return True
