"""Owner-reviewed batches of existing, exact WhatsApp drafts.

The Mini App can renew an expired draft and approve up to ten visible texts in
one action. Delivery still passes through the established approval, worker,
outbox, and WAHA checks; this module never calls the provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from procurement_bot.approvals import approve_request, request_approval
from procurement_bot.phone import normalize_phone
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.supplier_contact_scope import case_owned_supplier_contact_evidence
from procurement_bot.supplier_outreach import initial_inquiry_text


class OutreachBatchConflict(ValueError):
    """One selected draft is stale, foreign, or no longer safe to send."""


@dataclass(frozen=True, slots=True)
class RefreshedOutreachDraft:
    approval_id: UUID
    request_item_id: UUID
    item_name: str
    supplier_name: str
    recipient: str
    text: str


async def refresh_initial_outreach_drafts(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    approval_ids: Sequence[UUID],
    now: datetime | None = None,
) -> list[RefreshedOutreachDraft]:
    """Replace selected stale drafts with new, unapproved exact-text grants.

    The previous request is expired in the same transaction, so an old visible
    draft cannot still be approved after the owner refreshes it. This operation
    never approves, queues, or sends the replacement text; callers must show the
    returned text and obtain a new explicit approval.
    """

    if not 1 <= len(approval_ids) <= 10 or len(set(approval_ids)) != len(approval_ids):
        raise OutreachBatchConflict("Выберите от 1 до 10 разных черновиков")
    current = now or datetime.now(UTC)
    refreshed: list[RefreshedOutreachDraft] = []
    async with connection.transaction():
        case = await connection.fetchrow(
            """SELECT procurement_case.status,procurement_case.owner_user_id
               FROM procurement_cases AS procurement_case
               JOIN users AS owner ON owner.id=procurement_case.owner_user_id
               WHERE procurement_case.id=$1 AND owner.active
                 AND procurement_case.owner_user_id=$2
               FOR SHARE OF procurement_case,owner""",
            case_id, owner_user_id,
        )
        if case is None or case["status"] not in {
            "evaluating", "contacting", "report_ready",
        }:
            raise OutreachBatchConflict("Заявка не готова к переписке с поставщиками")

        for approval_id in sorted(approval_ids):
            row = await connection.fetchrow(
                """SELECT approval.id,approval.status,approval.expires_at,
                          approval.payload,conversation.id AS conversation_id,
                          conversation.state,conversation.outbound_count,
                          conversation.opted_out,contact.normalized_value,
                          contact.contact_type,contact.active,item.id AS request_item_id,
                          item.name AS item_name,item.specification_text,
                          item.quantity,item.unit,offer.id AS offer_id,
                          supplier.display_name AS supplier_name
                   FROM action_approvals approval
                   JOIN supplier_conversations conversation
                     ON conversation.id::text=approval.payload->>'conversation_id'
                    AND conversation.case_id=approval.case_id
                   JOIN supplier_conversation_targets target
                     ON target.conversation_id=conversation.id
                    AND target.offer_id::text=replace(
                        approval.payload->>'logical_action_id',
                        'initial-availability:', ''
                    )
                   JOIN offers offer ON offer.id=target.offer_id
                                    AND offer.request_item_id=target.request_item_id
                   JOIN request_items item ON item.id=target.request_item_id
                                            AND item.case_id=conversation.case_id
                   JOIN suppliers supplier ON supplier.id=conversation.supplier_id
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
                or payload.get("channel") != "whatsapp"
                or payload.get("logical_action_id")
                != f"initial-availability:{row['offer_id']}"
                or payload.get("is_followup") is not False
            ):
                raise OutreachBatchConflict("Черновик не относится к первому контакту")
            if row["status"] not in {"requested", "approved"} or (
                row["status"] == "approved"
                and (row["expires_at"] is None or row["expires_at"] > current)
            ):
                raise OutreachBatchConflict("Черновик уже обработан")
            try:
                recipient = normalize_phone(row["normalized_value"])
                recipient_matches = normalize_phone(payload.get("recipient")) == recipient
            except (TypeError, ValueError):
                recipient, recipient_matches = "", False
            if (
                row["state"] != "approved" or row["outbound_count"] != 0
                or row["opted_out"] or not row["active"]
                or row["contact_type"] != "whatsapp" or not recipient_matches
            ):
                raise OutreachBatchConflict("Контакт или диалог изменился; обновите заявку")

            text = initial_inquiry_text(
                str(row["item_name"]), str(row["specification_text"] or ""),
                row["quantity"], row["unit"],
            )
            if text == payload.get("text"):
                raise OutreachBatchConflict("Черновик уже содержит актуальный текст")

            expired = await connection.execute(
                """UPDATE action_approvals SET status='expired'
                   WHERE id=$1 AND case_id=$2 AND status=$3
                     AND ($3='requested' OR expires_at<=$4)""",
                approval_id, case_id, row["status"], current,
            )
            if expired != "UPDATE 1":
                raise OutreachBatchConflict("Черновик изменился; обновите заявку")

            refreshed_payload = dict(payload)
            refreshed_payload["recipient"] = recipient
            refreshed_payload["text"] = text
            replacement_id = await request_approval(
                connection,
                case_id=case_id,
                action_type="send_whatsapp",
                payload=refreshed_payload,
                requested_by=owner_user_id,
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            if replacement_id == approval_id:
                raise OutreachBatchConflict("Новый черновик не был создан")
            refreshed.append(
                RefreshedOutreachDraft(
                    approval_id=replacement_id,
                    request_item_id=row["request_item_id"],
                    item_name=str(row["item_name"]),
                    supplier_name=str(row["supplier_name"]),
                    recipient=recipient,
                    text=text,
                )
            )
    return refreshed


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
                  item.specification_text,item.quantity,item.unit,
                  target.offer_id,
                  supplier.display_name AS supplier_name
           FROM action_approvals approval
           JOIN supplier_conversations conversation
             ON conversation.id::text=approval.payload->>'conversation_id'
            AND conversation.case_id=approval.case_id
           JOIN supplier_conversation_targets target
             ON target.conversation_id=conversation.id
            AND target.offer_id::text=replace(
                approval.payload->>'logical_action_id',
                'initial-availability:', ''
            )
           JOIN request_items item ON item.id=target.request_item_id
                                    AND item.case_id=conversation.case_id
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
    drafts: list[dict[str, object]] = []
    for row in rows:
        draft = dict(row)
        draft["needs_refresh"] = draft.get("text") != initial_inquiry_text(
            str(draft.get("item_name") or ""),
            str(draft.get("specification_text") or ""),
            draft.get("quantity"),
            draft.get("unit"),
        )
        drafts.append(draft)
    return drafts


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
                          contact.contact_type,contact.active,offer.id AS offer_id,
                          item.name AS item_name,item.specification_text,
                          item.quantity,item.unit
                   FROM action_approvals approval
                   JOIN supplier_conversations conversation
                     ON conversation.id::text=approval.payload->>'conversation_id'
                    AND conversation.case_id=approval.case_id
                   JOIN supplier_conversation_targets target
                     ON target.conversation_id=conversation.id
                    AND target.offer_id::text=replace(
                        approval.payload->>'logical_action_id',
                        'initial-availability:', ''
                    )
                   JOIN offers offer ON offer.id=target.offer_id
                                    AND offer.request_item_id=target.request_item_id
                   JOIN request_items item ON item.id=target.request_item_id
                                            AND item.case_id=conversation.case_id
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
                or payload.get("channel") != "whatsapp"
                or not str(payload.get("logical_action_id", "")).startswith(
                    "initial-availability:"
                )
                or payload.get("is_followup") is not False
                or payload.get("logical_action_id")
                != f"initial-availability:{row['offer_id']}"
            ):
                raise OutreachBatchConflict("Черновик не относится к первому контакту")
            expected_text = initial_inquiry_text(
                str(row["item_name"]), str(row["specification_text"] or ""),
                row["quantity"], row["unit"],
            )
            if payload.get("text") != expected_text:
                raise OutreachBatchConflict(
                    "Старый текст черновика изменился; сначала обновите его и проверьте заново"
                )
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
