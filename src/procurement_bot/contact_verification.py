"""Verify discovered public phone numbers before WhatsApp outreach."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import asyncpg

from procurement_bot.providers.waha import WahaClient
from procurement_bot.queue import stable_key
from procurement_bot.supplier_contact_scope import case_owned_supplier_contact_evidence


async def verify_case_whatsapp_numbers(
    pool: asyncpg.Pool,
    client: WahaClient,
    *,
    case_id: UUID,
    max_checks: int = 200,
) -> tuple[int, int]:
    """Check phone-only leads, retaining every number even when WhatsApp is absent."""

    if not 1 <= max_checks <= 200:
        raise ValueError("max_checks must be between 1 and 200")
    async with pool.acquire() as connection:
        contacts = await connection.fetch(
            """
            SELECT DISTINCT c.id,c.normalized_value
            FROM offers o
            JOIN request_items i ON i.id=o.request_item_id
            JOIN supplier_contacts c ON c.supplier_id=o.supplier_id
            JOIN source_evidence evidence ON evidence.id=c.source_evidence_id
            WHERE i.case_id=$1 AND c.active AND c.contact_type='phone'
              AND __CONTACT_SCOPE__
              AND NOT EXISTS (
                SELECT 1 FROM workflow_events event
                WHERE event.case_id=$1
                  AND event.event_type='whatsapp_contact_checked'
                  AND event.data->>'contact_id'=c.id::text
                  AND event.created_at>now()-interval '24 hours'
              )
            ORDER BY c.id
            LIMIT $2
            """.replace(
                "__CONTACT_SCOPE__", case_owned_supplier_contact_evidence(
                    contact_alias="c", evidence_alias="evidence"
                ),
            ),
            case_id, max_checks,
        )
    checked = confirmed = 0
    for contact in contacts:
        result = await client.check_phone_exists(contact["normalized_value"])
        expected_chat_id = contact["normalized_value"].removeprefix("+") + "@c.us"
        usable = result.number_exists and result.chat_id == expected_chat_id
        async with pool.acquire() as connection, connection.transaction():
            inserted = await connection.fetchval(
                """
                INSERT INTO workflow_events(
                    case_id,event_type,actor_type,actor_id,idempotency_key,data
                ) VALUES ($1,'whatsapp_contact_checked','worker','waha',$2,$3::jsonb)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                case_id,
                stable_key(
                    "whatsapp-contact-check", case_id, contact["id"],
                    datetime.now(UTC).date().isoformat(),
                ),
                {
                    "contact_id": str(contact["id"]),
                    "number_exists": result.number_exists,
                    "chat_id_kind": (
                        result.chat_id.rsplit("@", 1)[-1]
                        if result.chat_id is not None else ""
                    ),
                    "outreach_usable": usable,
                },
            )
            if inserted is None:
                continue
            checked += 1
            if usable:
                changed = await connection.execute(
                    """
                    UPDATE supplier_contacts
                    SET contact_type='whatsapp',is_whatsapp_confirmed=TRUE,
                        last_seen_at=now()
                    WHERE id=$1 AND active AND contact_type='phone'
                    """,
                    contact["id"],
                )
                confirmed += changed == "UPDATE 1"
    return checked, confirmed
