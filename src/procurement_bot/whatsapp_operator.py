"""Narrow authenticated handler boundary for WhatsApp account operator commands.

Application wiring must pass an auth context produced by its trusted session
middleware; request payloads never supply an actor ID.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.providers.waha import WahaClient
from procurement_bot.whatsapp_account import (
    attest_legacy_unknown_inventory,
    load_legacy_unknown_inventory,
    pin_whatsapp_account_identity,
)


@dataclass(frozen=True, slots=True)
class AuthenticatedOperatorContext:
    """Trusted actor identity resolved by the application authentication layer."""

    actor_user_id: UUID
    authentication_source: str


async def pin_from_operator_context(
    pool: asyncpg.Pool,
    client: WahaClient,
    *,
    auth: AuthenticatedOperatorContext,
    expected_account_jid: str,
    reason: str,
    idempotency_key: str,
) -> int:
    return await pin_whatsapp_account_identity(
        pool,
        client,
        actor_user_id=auth.actor_user_id,
        expected_account_jid=expected_account_jid,
        reason=reason,
        operator_idempotency_key=idempotency_key,
    )


async def legacy_inventory_from_operator_context(
    pool: asyncpg.Pool,
    *,
    auth: AuthenticatedOperatorContext,
    session_name: str,
) -> dict[str, Any]:
    return await load_legacy_unknown_inventory(
        pool,
        session_name=session_name,
        actor_user_id=auth.actor_user_id,
    )


async def quarantine_from_operator_context(
    pool: asyncpg.Pool,
    *,
    auth: AuthenticatedOperatorContext,
    session_name: str,
    reservation_ids: list[UUID],
    inventory_sha256: str,
    source_reference: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return await attest_legacy_unknown_inventory(
        pool,
        session_name=session_name,
        actor_user_id=auth.actor_user_id,
        reservation_ids=reservation_ids,
        inventory_sha256=inventory_sha256,
        source_reference=source_reference,
        reason=reason,
        operator_idempotency_key=idempotency_key,
    )
