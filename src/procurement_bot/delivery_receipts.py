from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

from procurement_bot.queue import enqueue_job, stable_key

ACK_STATES = {-1: "failed", 0: "accepted", 1: "server", 2: "delivered", 3: "read", 4: "played"}


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    session_name: str
    external_message_id: str
    ack: int
    received_at: datetime


def parse_receipt(value: object) -> DeliveryReceipt:
    if not isinstance(value, dict) or value.get("event") != "message.ack":
        raise ValueError("invalid receipt event")
    payload = value.get("payload")
    if not isinstance(payload, dict) or payload.get("fromMe") is not True:
        raise ValueError("receipt must reference our message")
    session, message_id = value.get("session"), payload.get("id")
    if not isinstance(session, str) or not session.strip() or len(session) > 200:
        raise ValueError("invalid receipt session")
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 512:
        raise ValueError("invalid receipt id")
    ack = payload.get("ack")
    if isinstance(ack, bool) or not isinstance(ack, int) or ack not in ACK_STATES:
        raise ValueError("invalid receipt ack")
    if (
        payload.get("ackName") is not None
        and payload["ackName"]
        != {-1: "ERROR", 0: "PENDING", 1: "SERVER", 2: "DEVICE", 3: "READ", 4: "PLAYED"}[ack]
    ):
        raise ValueError("receipt ack/name mismatch")
    return DeliveryReceipt(session.strip(), message_id.strip(), ack, datetime.now(UTC))


def advance_ack(previous: int | None, incoming: int) -> int:
    if incoming not in ACK_STATES:
        raise ValueError("invalid ack")
    if previous is None:
        return incoming
    if incoming == -1:
        return -1 if previous <= 0 else previous
    # A duplicate PENDING receipt cannot prove recovery from an ERROR: receipt
    # idempotency intentionally collapses repeated ACK values. Require a real
    # server/device/delivery ACK before clearing a failed state.
    if previous == -1 and incoming == 0:
        return -1
    return max(previous, incoming)


async def record_receipt(
    connection: asyncpg.Connection, receipt: DeliveryReceipt, *, source: str = "webhook"
) -> bool:
    if source not in {"webhook", "reconciliation"}:
        raise ValueError("invalid receipt source")
    # Receipts can arrive before the HTTP send transaction commits. Persist
    # unmatched receipts and project them on completion/reconciliation later.
    async with connection.transaction():
        await connection.execute(
            """INSERT INTO whatsapp_delivery_receipts(
                session_name,external_message_id,ack,source,received_at,idempotency_key
            ) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(idempotency_key) DO NOTHING""",
            receipt.session_name,
            receipt.external_message_id,
            receipt.ack,
            source,
            receipt.received_at,
            stable_key("waha-ack", receipt.session_name, receipt.external_message_id, receipt.ack),
        )
        messages = await connection.fetch(
            """SELECT m.id,c.case_id FROM supplier_conversation_messages m
            JOIN supplier_conversations c ON c.id=m.conversation_id
            WHERE m.session_name=$1 AND m.external_message_id=$2 AND m.direction='outbound'
            ORDER BY m.id LIMIT 2 FOR SHARE OF m""",
            receipt.session_name,
            receipt.external_message_id,
        )
        # Provider IDs are only unique per chat in our schema. Never guess the
        # target conversation if WAHA reuses an ID across chats.
        if len(messages) != 1:
            return False
        message = messages[0]
        await project_message_receipts(connection, message["id"])
        await enqueue_job(
            connection,
            kind="refresh_report",
            payload={
                "case_id": str(message["case_id"]),
                "trigger_id": f"delivery:{message['id']}:{receipt.ack}",
            },
            idempotency_key=stable_key("report-delivery", message["id"], receipt.ack),
        )
        return True


async def project_message_receipts(connection: asyncpg.Connection, message_id: Any) -> None:
    await connection.execute(
        """INSERT INTO whatsapp_delivery_states(message_id,state) VALUES($1,'accepted')
        ON CONFLICT(message_id) DO NOTHING""",
        message_id,
    )
    current = await connection.fetchval(
        "SELECT ack FROM whatsapp_delivery_states WHERE message_id=$1 FOR UPDATE",
        message_id,
    )
    receipts = await connection.fetch(
        """SELECT r.ack FROM whatsapp_delivery_receipts r
        JOIN supplier_conversation_messages m
          ON m.session_name=r.session_name AND m.external_message_id=r.external_message_id
        WHERE m.id=$1 AND m.direction='outbound'
          AND (SELECT count(*) FROM supplier_conversation_messages other
               WHERE other.session_name=r.session_name
                 AND other.external_message_id=r.external_message_id
                 AND other.direction='outbound')=1
        ORDER BY r.received_at,r.id""",
        message_id,
    )
    for receipt in receipts:
        current = advance_ack(current, receipt["ack"])
    if current is not None:
        await connection.execute(
            """UPDATE whatsapp_delivery_states SET ack=$2,state=$3,updated_at=now()
            WHERE message_id=$1""",
            message_id,
            current,
            ACK_STATES[current],
        )
