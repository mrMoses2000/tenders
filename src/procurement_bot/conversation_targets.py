from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

import asyncpg

PROVENANCE_KINDS = frozenset({"research", "approval", "operator", "automation", "import"})


class ConversationTargetConflict(RuntimeError):
    """An idempotency or natural key was reused for a different target fact."""


class ConversationTargetsRequired(RuntimeError):
    """A workflow tried to use a legacy or incomplete targetless conversation."""


@dataclass(frozen=True, slots=True)
class ConversationTargetInput:
    conversation_id: UUID
    offer_id: UUID
    request_item_id: UUID
    provenance_kind: str
    provenance_ref: str
    idempotency_key: str
    source_evidence_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.provenance_kind not in PROVENANCE_KINDS:
            raise ValueError(f"unsupported provenance_kind: {self.provenance_kind}")
        _require_nonempty("provenance_ref", self.provenance_ref)
        _require_nonempty("idempotency_key", self.idempotency_key)


@dataclass(frozen=True, slots=True)
class ConversationTarget:
    id: UUID
    conversation_id: UUID
    case_id: UUID
    supplier_id: UUID
    request_item_id: UUID
    offer_id: UUID
    provenance_kind: str
    provenance_ref: str
    source_evidence_id: UUID | None
    idempotency_key: str
    created: bool = False


async def attach_conversation_target(
    connection: asyncpg.Connection,
    value: ConversationTargetInput,
) -> ConversationTarget:
    """Attach an exact offer/item to a conversation with retry-safe provenance.

    The lookup checks the ownership relationship before insertion. Composite
    foreign keys repeat that proof at commit time, closing the race between the
    validation query and the write.
    """

    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"conversation-target:{value.idempotency_key}",
        )
        ownership = await connection.fetchrow(
            """
            SELECT
                c.case_id,
                c.supplier_id,
                o.request_item_id,
                o.supplier_id AS offer_supplier_id,
                i.case_id AS item_case_id
            FROM supplier_conversations AS c
            CROSS JOIN offers AS o
            JOIN request_items AS i ON i.id=$3 AND o.request_item_id=i.id
            WHERE c.id=$1 AND o.id=$2
            FOR SHARE OF c, o, i
            """,
            value.conversation_id,
            value.offer_id,
            value.request_item_id,
        )
        if ownership is None:
            raise ValueError(
                "conversation, offer, or request item does not exist or the offer targets "
                "a different request item"
            )
        if ownership["supplier_id"] != ownership["offer_supplier_id"]:
            raise ConversationTargetConflict(
                "offer supplier does not match the conversation supplier"
            )
        if ownership["case_id"] != ownership["item_case_id"]:
            raise ConversationTargetConflict(
                "request item case does not match the conversation case"
            )
        if value.source_evidence_id is not None:
            evidence_exists = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM source_evidence WHERE id=$1)",
                value.source_evidence_id,
            )
            if not evidence_exists:
                raise ValueError("source evidence does not exist")

        target_id = uuid4()
        row = await connection.fetchrow(
            """
            INSERT INTO supplier_conversation_targets(
                id,conversation_id,case_id,supplier_id,request_item_id,offer_id,
                provenance_kind,provenance_ref,source_evidence_id,idempotency_key
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT DO NOTHING
            RETURNING *
            """,
            target_id,
            value.conversation_id,
            ownership["case_id"],
            ownership["supplier_id"],
            value.request_item_id,
            value.offer_id,
            value.provenance_kind,
            value.provenance_ref,
            value.source_evidence_id,
            value.idempotency_key,
        )
        if row is not None:
            return _target(row, created=True)

        collisions = await connection.fetch(
            """
            SELECT * FROM supplier_conversation_targets
            WHERE idempotency_key=$1 OR (conversation_id=$2 AND offer_id=$3)
            ORDER BY created_at,id
            """,
            value.idempotency_key,
            value.conversation_id,
            value.offer_id,
        )
        if not collisions:
            raise RuntimeError("conversation target conflict vanished")
        if len(collisions) != 1:
            raise ConversationTargetConflict(
                "idempotency key and conversation/offer identity resolve to different targets"
            )
        existing = collisions[0]
        _verify_existing(
            existing,
            {
                "conversation_id": value.conversation_id,
                "case_id": ownership["case_id"],
                "supplier_id": ownership["supplier_id"],
                "request_item_id": value.request_item_id,
                "offer_id": value.offer_id,
                "provenance_kind": value.provenance_kind,
                "provenance_ref": value.provenance_ref,
                "source_evidence_id": value.source_evidence_id,
                "idempotency_key": value.idempotency_key,
            },
        )
        return _target(existing, created=False)


async def list_conversation_targets(
    connection: asyncpg.Connection,
    conversation_id: UUID,
) -> tuple[ConversationTarget, ...]:
    """List stable target identities; an existing legacy conversation may be empty."""

    async with connection.transaction():
        existing_id = await connection.fetchval(
            "SELECT id FROM supplier_conversations WHERE id=$1 FOR SHARE",
            conversation_id,
        )
        if existing_id is None:
            raise ValueError("conversation does not exist")
        rows = await connection.fetch(
            """
            SELECT * FROM supplier_conversation_targets
            WHERE conversation_id=$1
            ORDER BY created_at,id
            """,
            conversation_id,
        )
        return tuple(_target(row, created=False) for row in rows)


async def require_conversation_targets(
    connection: asyncpg.Connection,
    conversation_id: UUID,
) -> tuple[ConversationTarget, ...]:
    """Return targets or fail closed before reply interpretation or sending."""

    targets = await list_conversation_targets(connection, conversation_id)
    if not targets:
        raise ConversationTargetsRequired(
            "conversation has no exact offer/request-item target; manual association is required"
        )
    return targets


def _target(row: Mapping[str, object], *, created: bool) -> ConversationTarget:
    return ConversationTarget(
        id=row["id"],  # type: ignore[arg-type]
        conversation_id=row["conversation_id"],  # type: ignore[arg-type]
        case_id=row["case_id"],  # type: ignore[arg-type]
        supplier_id=row["supplier_id"],  # type: ignore[arg-type]
        request_item_id=row["request_item_id"],  # type: ignore[arg-type]
        offer_id=row["offer_id"],  # type: ignore[arg-type]
        provenance_kind=row["provenance_kind"],  # type: ignore[arg-type]
        provenance_ref=row["provenance_ref"],  # type: ignore[arg-type]
        source_evidence_id=row["source_evidence_id"],  # type: ignore[arg-type]
        idempotency_key=row["idempotency_key"],  # type: ignore[arg-type]
        created=created,
    )


def _verify_existing(
    row: Mapping[str, object],
    expected: Mapping[str, object],
) -> None:
    changed = sorted(key for key, value in expected.items() if row[key] != value)
    if changed:
        raise ConversationTargetConflict(
            "conversation target collision with different fields: " + ", ".join(changed)
        )


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
