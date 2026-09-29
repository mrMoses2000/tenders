"""Owner-reviewed batches of existing, exact WhatsApp drafts.

The Mini App can renew an expired draft and approve up to ten visible texts in
one action. Delivery still passes through the established approval, worker,
outbox, and WAHA checks; this module never calls the provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from procurement_bot.approvals import approve_request, request_approval
from procurement_bot.phone import normalize_phone
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.supplier_contact_scope import case_owned_supplier_contact_evidence


class OutreachBatchConflict(ValueError):
    """One selected draft is stale, foreign, or no longer safe to send."""


async def load_initial_outreach_drafts(
    connection: asyncpg.Connection, case_id: UUID,
) -> list[dict[str, object]]:
    rows = await connection.fetch(
        """SELECT DISTINCT ON (conversation.id)
                  approval.id AS approval_id,approval.status AS approval_status,
                  approval.expires_at,approval.payload->>'text' AS text,
                  approval.payload->>'recipient' AS recipient,
                  conversation.id AS conversation_id,
                  item.id AS request_item_id,item.line_number,item.name AS item_name,
                  supplier.display_name AS supplier_name
           FROM action_approvals approval
           JOIN supplier_conversations conversation
             ON conversation.id::text=approval.payload->>'conversation_id'
            AND conversation.case_id=approval.case_id
           JOIN supplier_conversation_targets target
             ON target.conversation_id=conversation.id
           JOIN request_items item ON item.id=target.request_item_id
           JOIN suppliers supplier ON supplier.id=conversation.supplier_id
           WHERE approval.case_id=$1 AND approval.action_type='send_whatsapp'
             AND approval.payload->>'logical_action_id' LIKE 'initial-availability:%'
             AND (approval.status='requested' OR
                  (approval.status='approved' AND approval.expires_at<=now()))
             AND conversation.state='approved' AND conversation.outbound_count=0
             AND NOT conversation.opted_out
           ORDER BY conversation.id,approval.requested_at DESC,approval.id DESC,
                    item.line_number
           LIMIT 301""",
        case_id,
    )
    return [dict(row) for row in rows]


async def approve_initial_outreach_batch(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    approval_ids: Sequence[UUID],
    now: datetime | None = None,
) -> list[UUID]:
    if not 1 <= len(approval_ids) <= 10 or len(set(approval_ids)) != len(approval_ids):
        raise OutreachBatchConflict("Выберите от 1 до 10 разных черновиков")
    current = now or datetime.now(ZoneInfo("UTC"))
    local_time = current.astimezone(ZoneInfo("Asia/Almaty")).time()
    if not time(9) <= local_time < time(20):
        raise OutreachBatchConflict("Отправка доступна с 09:00 до 20:00 по Алматы")
    approved: list[UUID] = []
    async with connection.transaction():
        case_status = await connection.fetchval(
            """SELECT status FROM procurement_cases
               WHERE id=$1 AND owner_user_id=$2 FOR SHARE""",
            case_id, owner_user_id,
        )
        if case_status not in {"evaluating", "contacting", "report_ready"}:
            raise OutreachBatchConflict("Заявка не готова к переписке с поставщиками")
        for approval_id in sorted(approval_ids):
            row = await connection.fetchrow(
                """SELECT approval.id,approval.status,approval.expires_at,
                          approval.payload,conversation.id AS conversation_id,
                          conversation.state,conversation.outbound_count,
                          conversation.opted_out,contact.normalized_value,
                          contact.contact_type,contact.active
                   FROM action_approvals approval
                   JOIN supplier_conversations conversation
                     ON conversation.id::text=approval.payload->>'conversation_id'
                    AND conversation.case_id=approval.case_id
                   JOIN supplier_contacts contact
                     ON contact.id=conversation.supplier_contact_id
                   JOIN source_evidence contact_evidence
                     ON contact_evidence.id=contact.source_evidence_id
                   WHERE approval.id=$1 AND approval.case_id=$2
                     AND approval.action_type='send_whatsapp'
                     AND __CONTACT_SCOPE__
                   FOR UPDATE OF approval,conversation""".replace(
                    "__CONTACT_SCOPE__", case_owned_supplier_contact_evidence(
                        contact_alias="contact", evidence_alias="contact_evidence",
                        case_param="$2",
                    ),
                ),
                approval_id, case_id,
            )
            if row is None:
                raise OutreachBatchConflict("Черновик отправки не найден")
            payload = row["payload"]
            if not isinstance(payload, dict) or (
                payload.get("case_id") != str(case_id)
                or payload.get("conversation_id") != str(row["conversation_id"])
                or not str(payload.get("logical_action_id", "")).startswith(
                    "initial-availability:"
                )
                or payload.get("is_followup") is not False
            ):
                raise OutreachBatchConflict("Черновик не относится к первому контакту")
            try:
                recipient_matches = normalize_phone(payload.get("recipient")) == (
                    normalize_phone(row["normalized_value"])
                )
            except (TypeError, ValueError):
                recipient_matches = False
            if (
                row["state"] != "approved" or row["outbound_count"] != 0
                or row["opted_out"] or not row["active"]
                or row["contact_type"] not in {"phone", "whatsapp"}
                or not recipient_matches
            ):
                raise OutreachBatchConflict("Контакт или диалог изменился; обновите заявку")
            selected_id = approval_id
            if row["expires_at"] is not None and row["expires_at"] <= current:
                selected_id = await request_approval(
                    connection, case_id=case_id, action_type="send_whatsapp",
                    payload=payload, requested_by=owner_user_id,
                    expires_at=current + timedelta(hours=24),
                )
            elif row["status"] != "requested":
                raise OutreachBatchConflict("Черновик уже обработан")
            changed = await approve_request(
                connection, approval_id=selected_id, approved_by=owner_user_id
            )
            if not changed:
                raise OutreachBatchConflict("Черновик истёк во время одобрения; обновите заявку")
            await enqueue_job(
                connection, kind="enqueue_whatsapp_send",
                payload={"approval_id": str(selected_id)},
                idempotency_key=stable_key(
                    "enqueue-whatsapp-send-approval", selected_id
                ),
            )
            approved.append(selected_id)
    return approved
