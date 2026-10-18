"""Durable review items; visibility never authorizes an uncertain send replay."""

from __future__ import annotations

from uuid import UUID

from procurement_bot.queue import enqueue_outbox, stable_key

_LABELS = {
    "unknown_send": "Есть отправки поставщикам с неизвестным результатом провайдера. "
    "Повторная отправка заблокирована до подтверждения исходного сообщения.",
    "blocked_reply": "Ответ поставщика сохранён, но обработка ждёт подтверждения "
    "исходного отправленного сообщения.",
    "reply_processing_failed": "Ответ поставщика сохранён, но его автоматическая "
    "обработка завершилась ошибкой. Требуется проверка.",
}


async def record_review(connection, *, case_id: UUID, kind: str, subject_id: UUID,
                        conversation_id: UUID | None, reason_code: str) -> UUID:
    """Caller owns the transaction; repeat observations and alerts are idempotent."""
    if kind not in _LABELS or not reason_code or len(reason_code) > 200:
        raise ValueError("invalid pipeline review")
    review_id = await connection.fetchval(
        "INSERT INTO pipeline_reviews(case_id,kind,subject_id,conversation_id,reason_code) "
        "VALUES($1,$2,$3,$4,$5) ON CONFLICT(case_id,kind,subject_id) DO UPDATE "
        "SET last_seen_at=now() RETURNING id",
        case_id, kind, subject_id, conversation_id, reason_code,
    )
    owner = await connection.fetchrow(
        "SELECT u.telegram_id,p.title FROM procurement_cases p JOIN users u "
        "ON u.id=p.owner_user_id WHERE p.id=$1", case_id,
    )
    if owner is not None:
        new_alert = await connection.fetchval(
            "INSERT INTO pipeline_review_alerts(case_id,kind) VALUES($1,$2) "
            "ON CONFLICT DO NOTHING RETURNING id", case_id, kind,
        )
        if new_alert:
            await enqueue_outbox(
                connection, event_type="telegram.send_message",
                payload={"chat_id": owner["telegram_id"],
                         "text": f"{owner['title'][:120]}: {_LABELS[kind]} "
                         "Подробности доступны в карточке заявки."},
                idempotency_key=stable_key("pipeline-review-alert", case_id, kind, new_alert),
            )
    return review_id


async def load_reviews(connection, case_id: UUID, *, limit: int = 100) -> dict:
    counts = await connection.fetch(
        "SELECT kind,count(*) AS count FROM pipeline_reviews WHERE case_id=$1 "
        "AND state='open' GROUP BY kind ORDER BY kind", case_id,
    )
    rows = await connection.fetch(
        "SELECT r.id,r.kind,r.subject_id,r.conversation_id,r.reason_code,"
        "r.created_at,r.last_seen_at,s.id AS supplier_id,s.display_name AS supplier_name,"
        "contact.display_value AS supplier_contact,sc.state AS conversation_state,"
        "sc.external_chat_id,sc.opted_out,"
        "outbox.status AS outbound_status,outbox.provider_request_id,"
        "outbox.last_error_code AS outbound_error_code,"
        "left(outbox.payload->>'text',1000) AS outbound_text,"
        "length(outbox.payload->>'text')>1000 AS outbound_text_truncated,"
        "left(message.text_content,1000) AS inbound_text,"
        "message.received_at AS inbound_received_at,"
        "length(message.text_content)>1000 AS inbound_text_truncated,"
        "reply_job.status AS reply_job_status,reply_job.attempts AS reply_job_attempts,"
        "reply_job.max_attempts AS reply_job_max_attempts,"
        "reply_job.last_error_code AS reply_job_error_code "
        "FROM pipeline_reviews r "
        "LEFT JOIN supplier_conversations sc ON sc.id=r.conversation_id "
        "AND sc.case_id=r.case_id "
        "LEFT JOIN suppliers s ON s.id=sc.supplier_id "
        "LEFT JOIN supplier_contacts contact ON contact.id=sc.supplier_contact_id "
        "AND contact.supplier_id=sc.supplier_id "
        "LEFT JOIN outbox_events outbox ON r.kind='unknown_send' "
        "AND outbox.id=r.subject_id AND outbox.event_type='whatsapp.send_text' "
        "AND outbox.payload->>'case_id'=r.case_id::text "
        "AND outbox.payload->>'conversation_id'=sc.id::text "
        "LEFT JOIN jobs reply_job ON r.kind='reply_processing_failed' "
        "AND reply_job.id=r.subject_id AND reply_job.kind='process_supplier_reply' "
        "AND reply_job.payload->>'conversation_id'=sc.id::text "
        "AND (reply_job.payload->>'case_id' IS NULL "
        "OR reply_job.payload->>'case_id'=r.case_id::text) "
        "LEFT JOIN supplier_conversation_messages message "
        "ON message.conversation_id=sc.id AND message.direction='inbound' "
        "AND ((r.kind='blocked_reply' AND message.id=r.subject_id) "
        "OR (r.kind='reply_processing_failed' "
        "AND message.id::text=reply_job.payload->>'message_id')) "
        "WHERE r.case_id=$1 AND r.state='open' "
        "ORDER BY r.created_at,r.id LIMIT $2", case_id, limit,
    )
    return {"counts": {row["kind"]: row["count"] for row in counts},
            "items": [dict(row) for row in rows],
            "truncated": sum(row["count"] for row in counts) > len(rows)}


async def reconcile_reviews(connection, case_id: UUID) -> dict:
    """Backfill existing failures; resolve only durable acceptance or reply projection."""
    await connection.execute(
        "UPDATE pipeline_reviews r SET state='resolved',resolved_at=now() "
        "WHERE r.case_id=$1 AND r.state='open' AND ((r.kind='unknown_send' AND (EXISTS "
        "(SELECT 1 FROM whatsapp_send_acceptance_journal j WHERE j.outbox_event_id=r.subject_id) "
        "OR EXISTS(SELECT 1 FROM outbox_events e WHERE e.id=r.subject_id "
        "AND e.external_id IS NOT NULL) "
        "OR EXISTS(SELECT 1 FROM supplier_conversation_messages m "
        "WHERE m.outbox_event_id=r.subject_id AND m.direction='outbound'))) "
        "OR (r.kind='reply_processing_failed' AND EXISTS(SELECT 1 FROM jobs j "
        "JOIN supplier_conversations c ON c.id=r.conversation_id AND c.case_id=r.case_id "
        "JOIN supplier_conversation_messages m ON m.conversation_id=c.id "
        "AND m.id::text=j.payload->>'message_id' AND m.direction='inbound' "
        "WHERE j.id=r.subject_id AND j.kind='process_supplier_reply' "
        "AND j.payload->>'conversation_id'=c.id::text AND (j.status='succeeded' "
        "OR EXISTS(SELECT 1 FROM workflow_events w WHERE w.case_id=r.case_id "
        "AND w.event_type='supplier_reply_processed' "
        "AND w.data->>'case_id'=r.case_id::text "
        "AND w.data->>'conversation_id'=c.id::text "
        "AND w.data->>'message_id'=m.id::text)))) "
        "OR (r.kind='blocked_reply' AND EXISTS(SELECT 1 FROM workflow_events i "
        "WHERE i.case_id=r.case_id AND i.event_type='supplier_reply_processed' "
        "AND i.data->>'message_id'=r.subject_id::text)))", case_id,
    )
    await connection.execute(
        "DELETE FROM pipeline_review_alerts a WHERE a.case_id=$1 "
        "AND NOT EXISTS(SELECT 1 FROM pipeline_reviews r WHERE r.case_id=a.case_id "
        "AND r.kind=a.kind AND r.state='open')", case_id,
    )
    rows = await connection.fetch(
        "SELECT e.id,c.id AS conversation_id FROM outbox_events e "
        "JOIN supplier_conversations c ON e.payload->>'conversation_id'=c.id::text "
        "WHERE c.case_id=$1 AND e.event_type='whatsapp.send_text' "
        "AND e.external_id IS NULL AND NOT EXISTS(SELECT 1 FROM supplier_conversation_messages m "
        "WHERE m.outbox_event_id=e.id AND m.direction='outbound') "
        "AND e.provider_request_id IS NOT NULL AND (e.status IN ('retry','dead') "
        "OR (e.status='sending' AND e.lease_expires_at<=now())) "
        "AND NOT EXISTS(SELECT 1 FROM whatsapp_send_acceptance_journal j "
        "WHERE j.outbox_event_id=e.id) AND NOT EXISTS(SELECT 1 FROM "
        "whatsapp_account_send_reservations r WHERE r.outbox_event_id=e.id AND r.state='rejected')",
        case_id,
    )
    for row in rows:
        await record_review(connection, case_id=case_id, kind="unknown_send",
                            subject_id=row["id"], conversation_id=row["conversation_id"],
                            reason_code="provider_acceptance_unknown")
    failures = await connection.fetch(
        "SELECT j.id,c.id AS conversation_id,j.last_error_code FROM jobs j "
        "JOIN supplier_conversations c ON j.payload->>'conversation_id'=c.id::text "
        "JOIN supplier_conversation_messages m ON m.conversation_id=c.id "
        "AND m.id::text=j.payload->>'message_id' AND m.direction='inbound' "
        "WHERE c.case_id=$1 AND j.kind='process_supplier_reply' AND j.status='dead' "
        "AND NOT EXISTS(SELECT 1 FROM workflow_events w WHERE w.case_id=c.case_id "
        "AND w.event_type='supplier_reply_processed' "
        "AND w.data->>'case_id'=c.case_id::text "
        "AND w.data->>'conversation_id'=c.id::text "
        "AND w.data->>'message_id'=m.id::text)",
        case_id,
    )
    for row in failures:
        await record_review(connection, case_id=case_id, kind="reply_processing_failed",
                            subject_id=row["id"], conversation_id=row["conversation_id"],
                            reason_code=row["last_error_code"][:200] or "reply_job_dead")
    return await load_reviews(connection, case_id)
