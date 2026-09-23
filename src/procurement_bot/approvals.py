from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from procurement_bot.errors import ValidationBlocked


class ApprovalRequired(ValidationBlocked):
    """The exact external action has no live, approved grant."""


@dataclass(frozen=True)
class ConsumedApproval:
    id: UUID
    case_id: UUID
    action_type: str
    payload_sha256: str
    consumed_at: datetime


def canonical_payload_json(payload: Mapping[str, Any]) -> str:
    """Serialize an approval payload without lossy fallbacks.

    Approval hashes are a security boundary. Values which are not genuine JSON
    (including NaN and Infinity) are rejected instead of being stringified.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("approval payload must be an object")
    if any(not isinstance(key, str) for key in payload):
        raise TypeError("approval payload keys must be strings")
    try:
        return json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("approval payload must contain only finite JSON values") from exc


def payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


async def request_approval(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    action_type: str,
    payload: Mapping[str, Any],
    requested_by: UUID | None = None,
    expires_at: datetime | None = None,
) -> UUID:
    """Create one append-only request, or return the matching live request."""

    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        if expires_at <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
    normalized = json.loads(canonical_payload_json(payload))
    digest = payload_sha256(normalized)
    approval_id = uuid4()
    lock_key = f"approval:{case_id}:{action_type}:{digest}"
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        lock_key,
    )
    await connection.execute(
        """
        UPDATE action_approvals
        SET status='expired'
        WHERE case_id=$1 AND action_type=$2 AND payload_sha256=$3
          AND status IN ('requested','approved')
          AND expires_at IS NOT NULL AND expires_at<=now()
        """,
        case_id,
        action_type,
        digest,
    )
    result = await connection.fetchval(
        """
        INSERT INTO action_approvals(
            id,case_id,action_type,payload,payload_sha256,status,
            requested_by,requested_at,expires_at
        )
        VALUES ($1,$2,$3,$4::jsonb,$5,'requested',$6,now(),$7)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        approval_id,
        case_id,
        action_type,
        normalized,
        digest,
        requested_by,
        expires_at,
    )
    if result is not None:
        return result
    existing = await connection.fetchval(
        """
        SELECT id FROM action_approvals
        WHERE case_id=$1 AND action_type=$2 AND payload_sha256=$3
          AND status IN ('requested','approved')
        """,
        case_id,
        action_type,
        digest,
    )
    if existing is None:
        raise RuntimeError("approval request conflict vanished")
    return existing


async def approve_request(
    connection: asyncpg.Connection,
    *,
    approval_id: UUID,
    approved_by: UUID,
) -> bool:
    """Approve a still-live request. Already handled grants are immutable."""

    result = await connection.execute(
        """
        UPDATE action_approvals
        SET status='approved',approved_by=$2,approved_at=now()
        WHERE id=$1 AND status='requested'
          AND (expires_at IS NULL OR expires_at>now())
        """,
        approval_id,
        approved_by,
    )
    return result == "UPDATE 1"


async def require_and_consume_approval(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    action_type: str,
    payload: Mapping[str, Any],
) -> ConsumedApproval:
    """Atomically consume the approval matching the exact outbound payload.

    Callers which also create an outbox event must invoke this inside the same
    database transaction as that insert. A grant is deliberately single-use.
    """

    digest = payload_sha256(payload)
    row = await connection.fetchrow(
        """
        UPDATE action_approvals
        SET status='consumed',consumed_at=now()
        WHERE case_id=$1 AND action_type=$2 AND payload_sha256=$3
          AND status='approved' AND (expires_at IS NULL OR expires_at>now())
        RETURNING id,case_id,action_type,payload_sha256,consumed_at
        """,
        case_id,
        action_type,
        digest,
    )
    if row is None:
        raise ApprovalRequired(
            "the exact action payload needs an unexpired approval before it can run"
        )
    return ConsumedApproval(
        id=row["id"],
        case_id=row["case_id"],
        action_type=row["action_type"],
        payload_sha256=row["payload_sha256"],
        consumed_at=row["consumed_at"],
    )


async def validate_consumed_approval(
    connection: asyncpg.Connection,
    *,
    approval_id: UUID | None,
    case_id: UUID,
    action_type: str,
    payload: Mapping[str, Any],
) -> None:
    """Revalidate the immutable grant at the external side-effect boundary."""

    if approval_id is None:
        raise ApprovalRequired("outbound action has no approval reference")
    digest = payload_sha256(payload)
    valid = await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM action_approvals
            WHERE id=$1 AND case_id=$2 AND action_type=$3
              AND payload_sha256=$4 AND status='consumed'
              AND consumed_at IS NOT NULL
              AND (expires_at IS NULL OR expires_at>consumed_at)
        )
        """,
        approval_id,
        case_id,
        action_type,
        digest,
    )
    if valid is not True:
        raise ApprovalRequired("outbound action approval is absent, stale, or mismatched")
