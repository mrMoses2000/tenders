from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

from procurement_bot.conversation_targets import (
    ConversationTargetConflict,
    ConversationTargetInput,
    ConversationTargetsRequired,
    attach_conversation_target,
    list_conversation_targets,
    require_conversation_targets,
)


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.conversation_id = uuid4()
        self.case_id = uuid4()
        self.supplier_id = uuid4()
        self.offer_id = uuid4()
        self.request_item_id = uuid4()
        self.evidence_id = uuid4()
        self.conversation_exists = True
        self.ownership_exists = True
        self.offer_supplier_id = self.supplier_id
        self.item_case_id = self.case_id
        self.evidence_exists = True
        self.rows: list[dict[str, Any]] = []
        self.insert_attempts = 0

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, sql: str, *_args: object) -> str:
        assert "pg_advisory_xact_lock" in sql
        return "SELECT 1"

    async def fetchval(self, sql: str, *args: object) -> bool | UUID | None:
        if "FROM source_evidence" in sql:
            return self.evidence_exists and args[0] == self.evidence_id
        if "FROM supplier_conversations" in sql:
            if self.conversation_exists and args[0] == self.conversation_id:
                return self.conversation_id
            return None
        raise AssertionError(sql)

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        if "CROSS JOIN offers" in sql:
            if (
                not self.ownership_exists
                or args
                != (
                    self.conversation_id,
                    self.offer_id,
                    self.request_item_id,
                )
            ):
                return None
            return {
                "case_id": self.case_id,
                "supplier_id": self.supplier_id,
                "request_item_id": self.request_item_id,
                "offer_supplier_id": self.offer_supplier_id,
                "item_case_id": self.item_case_id,
            }
        if "INSERT INTO supplier_conversation_targets" in sql:
            self.insert_attempts += 1
            (
                target_id,
                conversation_id,
                case_id,
                supplier_id,
                request_item_id,
                offer_id,
                provenance_kind,
                provenance_ref,
                source_evidence_id,
                idempotency_key,
            ) = args
            if any(
                row["idempotency_key"] == idempotency_key
                or (
                    row["conversation_id"] == conversation_id
                    and row["offer_id"] == offer_id
                )
                for row in self.rows
            ):
                return None
            row = {
                "id": target_id,
                "conversation_id": conversation_id,
                "case_id": case_id,
                "supplier_id": supplier_id,
                "request_item_id": request_item_id,
                "offer_id": offer_id,
                "provenance_kind": provenance_kind,
                "provenance_ref": provenance_ref,
                "source_evidence_id": source_evidence_id,
                "idempotency_key": idempotency_key,
                "created_at": object(),
            }
            self.rows.append(row)
            return row
        raise AssertionError(sql)

    async def fetch(self, sql: str, *args: object) -> list[dict[str, Any]]:
        if "idempotency_key=$1 OR" in sql:
            key, conversation_id, offer_id = args
            return [
                row
                for row in self.rows
                if row["idempotency_key"] == key
                or (
                    row["conversation_id"] == conversation_id
                    and row["offer_id"] == offer_id
                )
            ]
        if "WHERE conversation_id=$1" in sql:
            return [row for row in self.rows if row["conversation_id"] == args[0]]
        raise AssertionError(sql)


def _value(connection: _Connection, **changes: object) -> ConversationTargetInput:
    values: dict[str, object] = {
        "conversation_id": connection.conversation_id,
        "offer_id": connection.offer_id,
        "request_item_id": connection.request_item_id,
        "provenance_kind": "approval",
        "provenance_ref": "approval:9d845071",
        "source_evidence_id": connection.evidence_id,
        "idempotency_key": "conversation-target:42",
    }
    values.update(changes)
    return ConversationTargetInput(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_attach_and_list_exact_target_is_retry_safe() -> None:
    connection = _Connection()
    value = _value(connection)

    first = await attach_conversation_target(connection, value)  # type: ignore[arg-type]
    retry = await attach_conversation_target(connection, value)  # type: ignore[arg-type]
    listed = await list_conversation_targets(  # type: ignore[arg-type]
        connection, connection.conversation_id
    )

    assert first.created is True
    assert retry.created is False
    assert retry.id == first.id
    assert listed == (retry,)
    assert first.case_id == connection.case_id
    assert first.supplier_id == connection.supplier_id
    assert first.request_item_id == connection.request_item_id
    assert connection.insert_attempts == 2


@pytest.mark.asyncio
async def test_one_supplier_conversation_can_target_multiple_exact_items() -> None:
    connection = _Connection()
    first = await attach_conversation_target(  # type: ignore[arg-type]
        connection, _value(connection)
    )
    connection.offer_id = uuid4()
    connection.request_item_id = uuid4()
    second = await attach_conversation_target(  # type: ignore[arg-type]
        connection,
        _value(
            connection,
            provenance_ref="approval:9d845071:item-2",
            idempotency_key="conversation-target:43",
        ),
    )

    targets = await require_conversation_targets(  # type: ignore[arg-type]
        connection, connection.conversation_id
    )

    assert {target.id for target in targets} == {first.id, second.id}
    assert {target.request_item_id for target in targets} == {
        first.request_item_id,
        second.request_item_id,
    }


@pytest.mark.asyncio
async def test_reused_idempotency_key_with_different_provenance_fails_closed() -> None:
    connection = _Connection()
    await attach_conversation_target(connection, _value(connection))  # type: ignore[arg-type]

    with pytest.raises(ConversationTargetConflict, match="provenance_ref"):
        await attach_conversation_target(  # type: ignore[arg-type]
            connection,
            _value(connection, provenance_ref="approval:different"),
        )


@pytest.mark.asyncio
async def test_same_conversation_offer_with_a_new_key_is_not_silently_accepted() -> None:
    connection = _Connection()
    await attach_conversation_target(connection, _value(connection))  # type: ignore[arg-type]

    with pytest.raises(ConversationTargetConflict, match="idempotency_key"):
        await attach_conversation_target(  # type: ignore[arg-type]
            connection,
            _value(connection, idempotency_key="conversation-target:other"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["supplier", "case"])
async def test_cross_supplier_or_cross_case_target_is_rejected(mismatch: str) -> None:
    connection = _Connection()
    if mismatch == "supplier":
        connection.offer_supplier_id = uuid4()
    else:
        connection.item_case_id = uuid4()

    with pytest.raises(ConversationTargetConflict, match=mismatch):
        await attach_conversation_target(connection, _value(connection))  # type: ignore[arg-type]
    assert not connection.rows


@pytest.mark.asyncio
async def test_offer_must_target_the_explicit_request_item() -> None:
    connection = _Connection()

    with pytest.raises(ValueError, match="different request item"):
        await attach_conversation_target(  # type: ignore[arg-type]
            connection,
            _value(connection, request_item_id=uuid4()),
        )


@pytest.mark.asyncio
async def test_legacy_targetless_conversation_lists_empty_but_requirement_fails() -> None:
    connection = _Connection()

    assert (
        await list_conversation_targets(  # type: ignore[arg-type]
            connection, connection.conversation_id
        )
        == ()
    )
    with pytest.raises(ConversationTargetsRequired, match="manual association"):
        await require_conversation_targets(  # type: ignore[arg-type]
            connection, connection.conversation_id
        )


def test_input_requires_explicit_provenance_and_idempotency() -> None:
    connection = _Connection()
    with pytest.raises(ValueError, match="unsupported provenance_kind"):
        _value(connection, provenance_kind="guess")
    with pytest.raises(ValueError, match="provenance_ref"):
        _value(connection, provenance_ref=" ")
    with pytest.raises(ValueError, match="idempotency_key"):
        _value(connection, idempotency_key="")


@pytest.mark.asyncio
async def test_unknown_source_evidence_is_rejected_before_insert() -> None:
    connection = _Connection()
    connection.evidence_exists = False

    with pytest.raises(ValueError, match="source evidence"):
        await attach_conversation_target(connection, _value(connection))  # type: ignore[arg-type]
    assert not connection.rows


@pytest.mark.asyncio
async def test_unknown_conversation_is_not_reported_as_empty() -> None:
    connection = _Connection()
    connection.conversation_exists = False

    with pytest.raises(ValueError, match="conversation does not exist"):
        await list_conversation_targets(  # type: ignore[arg-type]
            connection, connection.conversation_id
        )


def test_type_contract_uses_uuid_fields() -> None:
    connection = _Connection()
    value = _value(connection)

    assert isinstance(value.conversation_id, UUID)
    assert isinstance(value.offer_id, UUID)
    assert isinstance(value.request_item_id, UUID)
