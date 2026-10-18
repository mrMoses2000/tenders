from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg

from procurement_bot.approvals import request_approval
from procurement_bot.auto_send_policy import authorize_and_enqueue_auto_whatsapp
from procurement_bot.delivery_receipts import DeliveryReceipt, record_receipt
from procurement_bot.pipeline_metrics import load_pipeline_metrics
from procurement_bot.pipeline_runtime import autonomy_enabled
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.supplier_dialogue import WhatsAppSendCommand
from procurement_bot.supplier_outreach import _assign_to_existing_research_group


async def ensure_dialogue_slot(connection: asyncpg.Connection, conversation_id: UUID) -> None:
    """Late replies persist first; sends reacquire a bounded execution slot."""
    from procurement_bot.supplier_dialogue import WhatsAppSendDeferred

    rows = await connection.fetch(
        """SELECT a.*,p.enabled,p.paused FROM supplier_dialogue_assignments a
        JOIN supplier_conversations c ON c.id=a.conversation_id
        JOIN case_pipeline_runtime p ON p.case_id=c.case_id
        WHERE a.conversation_id=$1 AND p.enabled
          AND a.request_item_id IN (
              SELECT target.request_item_id FROM supplier_conversation_item_turns t
              JOIN supplier_conversation_targets target ON target.id=t.target_id
              WHERE t.conversation_id=$1
                AND t.status IN ('awaiting_approval','pending_reply','needs_time','ambiguous'))""",
        conversation_id,
    )
    for row in rows:
        if row["paused"]:
            raise WhatsAppSendDeferred(
                available_at=datetime.now(UTC) + timedelta(minutes=30), reason="pipeline is paused"
            )
        if row["status"] in {"assigned", "waiting_reply"}:
            continue
        case_id = await connection.fetchval(
            "SELECT case_id FROM supplier_conversations WHERE id=$1",
            conversation_id,
        )
        assigned = await _assign_to_existing_research_group(
            connection,
            case_id=case_id,
            item_id=row["request_item_id"],
            supplier_id=row["supplier_id"],
            conversation_id=conversation_id,
        )
        if assigned is False:
            raise WhatsAppSendDeferred(
                available_at=datetime.now(UTC) + timedelta(minutes=2),
                reason="dialogue group capacity is occupied",
            )


async def reconcile_transport(pool: asyncpg.Pool, waha, case_id: UUID) -> dict[str, int | str]:
    """Only authenticated provider GETs; never send or fabricate incoming data."""
    async with pool.acquire() as c:
        messages = await c.fetch(
            """SELECT m.id,m.external_message_id,m.external_chat_id,m.session_name
            FROM supplier_conversation_messages m JOIN supplier_conversations c
              ON c.id=m.conversation_id JOIN whatsapp_delivery_states d ON d.message_id=m.id
            WHERE c.case_id=$1 AND m.direction='outbound' AND d.next_check_at<=now()
              AND (d.ack IS NULL OR d.ack<3)
            ORDER BY d.next_check_at,m.id LIMIT 30""",
            case_id,
        )
        chats = await c.fetch(
            """SELECT id,external_chat_id FROM supplier_conversations
            WHERE case_id=$1 AND session_name=$2
            ORDER BY COALESCE(history_check_at,'-infinity'),id LIMIT 30""",
            case_id,
            waha.session_name,
        )
    result = {"checked": 0, "receipts": 0, "unknown": 0, "incoming": 0}
    if hasattr(waha, "get_session_status"):
        try:
            result["waha_status"] = await waha.get_session_status()
        except Exception:
            result["waha_status"] = "UNAVAILABLE"
        if result["waha_status"] != "WORKING":
            result["unknown"] = len(messages)
            result["history_unavailable"] = len(chats)
            return result
    sem = asyncio.Semaphore(3)

    async def check(message):
        async with sem:
            try:
                if message["session_name"] != waha.session_name:
                    raise ValueError("provider_session_mismatch")
                received = await waha.fetch_message(
                    message["external_chat_id"],
                    message["external_message_id"],
                )
                ack = received.get("ack") if received else None
                if (
                    received
                    and received.get("id") == message["external_message_id"]
                    and received.get("fromMe") is True
                    and type(ack) is int
                    and -1 <= ack <= 4
                ):
                    async with pool.acquire() as c:
                        projected = await record_receipt(
                            c,
                            DeliveryReceipt(
                                message["session_name"],
                                message["external_message_id"],
                                ack,
                                datetime.now(UTC),
                            ),
                            source="reconciliation",
                        )
                    if projected:
                        result["receipts"] += 1
                        error = ""
                    else:
                        error = "provider_receipt_unmatched"
                else:
                    error = "provider_message_unavailable"
            except Exception as exc:
                error = type(exc).__name__
            async with pool.acquire() as c:
                await c.execute(
                    """UPDATE whatsapp_delivery_states SET last_checked_at=now(),
                    next_check_at=now()+interval '30 minutes',last_error_code=$2,
                    state=CASE WHEN ack IS NULL AND $2<>'' THEN 'unknown' ELSE state END
                    WHERE message_id=$1""",
                    message["id"],
                    error,
                )
            result["checked"] += 1
            result["unknown"] += bool(error)

    await asyncio.gather(*(check(message) for message in messages))

    from procurement_bot.transport_recovery import reconcile_history

    result.update(await reconcile_history(pool, waha, case_id))

    return result


async def expire_waiting_slots(connection: asyncpg.Connection, case_id: UUID) -> int:
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('supplier-dialogue-active-groups',0))"
    )
    # An HTTP request may have reached WAHA while its database transaction
    # rolled back. Keep that chat visibly uncertain and give the original send
    # a full reply window from the durable outbox attempt time.
    await connection.execute(
        """WITH unknown_sends AS (
          SELECT e.payload->>'conversation_id' AS conversation_id,
                 min(e.created_at) AS first_attempt_at
          FROM outbox_events e
          JOIN supplier_conversations c
            ON e.payload->>'conversation_id'=c.id::text
          WHERE c.case_id=$1 AND e.event_type='whatsapp.send_text'
            AND e.status='retry' AND e.last_error_code='provider_acceptance_unknown'
          GROUP BY e.payload->>'conversation_id'
        )
        UPDATE supplier_conversations c SET
          reply_due_at=GREATEST(COALESCE(c.reply_due_at,
              unknown_sends.first_attempt_at+interval '24 hours'),
              unknown_sends.first_attempt_at+interval '24 hours'),
          waiting_reason='transport_unknown'
        FROM unknown_sends WHERE c.id::text=unknown_sends.conversation_id
          AND NOT c.opted_out AND c.state NOT IN ('complete','closed_no_reply','opted_out')""",
        case_id,
    )
    await connection.execute(
        """UPDATE supplier_conversations SET reply_due_at=last_outbound_at+interval '24 hours'
        WHERE case_id=$1 AND reply_due_at IS NULL AND last_outbound_at IS NOT NULL
          AND state IN ('waiting_availability','waiting_spec','waiting_price','escalated')""",
        case_id,
    )
    await connection.execute(
        """UPDATE supplier_conversations c SET reply_due_at=t.next_check_at
        FROM supplier_conversation_item_turns t WHERE t.conversation_id=c.id AND c.case_id=$1
          AND t.status='needs_time' AND t.next_check_at>COALESCE(c.reply_due_at,'-infinity')""",
        case_id,
    )
    # Keep chat, turns and item targets open for exact late-reply routing. Only
    # execution assignments are released; no physical-chat ownership is moved.
    rows = await connection.fetch(
        """UPDATE supplier_dialogue_assignments a SET status='released',updated_at=now()
        FROM supplier_conversations c WHERE a.conversation_id=c.id AND c.case_id=$1
          AND a.status IN ('assigned','waiting_reply')
          AND (c.opted_out OR c.state IN ('complete','closed_no_reply')
            OR (c.reply_due_at<=now() AND
              (c.last_outbound_at IS NOT NULL OR c.waiting_reason='transport_unknown')
              AND NOT EXISTS(SELECT 1 FROM outbox_events e
                WHERE e.payload->>'conversation_id'=c.id::text
                  AND e.event_type='whatsapp.send_text'
                  AND e.status IN ('pending','retry','sending')
                  AND NOT (e.status='retry'
                    AND e.last_error_code='provider_acceptance_unknown'))))
        RETURNING a.id,a.conversation_id""",
        case_id,
    )
    for row in rows:
        await connection.execute(
            """UPDATE supplier_conversations c SET waiting_reason=CASE WHEN EXISTS(
              SELECT 1 FROM outbox_events e
              WHERE e.payload->>'conversation_id'=c.id::text
                AND e.event_type='whatsapp.send_text' AND e.status='retry'
                AND e.last_error_code='provider_acceptance_unknown'
            ) THEN 'transport_unknown' ELSE 'late_reply_wait' END
            WHERE id=$1 AND NOT opted_out""",
            row["conversation_id"],
        )
    return len(rows)


async def reacquire_queued_dialogue_slots(
    connection: asyncpg.Connection, case_id: UUID, *, limit: int = 10
) -> int:
    """Give already-authorized late replies/reminders first claim on free slots."""
    from procurement_bot.supplier_dialogue import WhatsAppSendDeferred

    rows = await connection.fetch(
        """SELECT id FROM (
          SELECT DISTINCT ON (c.id) c.id,
            pending_action.created_at,pending_action.action_id
          FROM supplier_conversations c
          JOIN supplier_dialogue_assignments a ON a.conversation_id=c.id
          CROSS JOIN LATERAL (
            SELECT queued.created_at,queued.id AS action_id FROM (
              SELECT e.created_at,e.id FROM outbox_events e
              WHERE e.payload->>'conversation_id'=c.id::text
                AND e.event_type='whatsapp.send_text'
                AND e.status IN ('pending','retry','sending')
                AND NOT (e.status='retry'
                  AND e.last_error_code='provider_acceptance_unknown')
              UNION ALL
              SELECT j.created_at,j.id FROM jobs j
              JOIN action_approvals approval
                ON approval.id::text=j.payload->>'approval_id'
              WHERE j.kind='enqueue_whatsapp_send'
                AND j.status IN ('pending','retry','running')
                AND approval.payload->>'conversation_id'=c.id::text
            ) queued ORDER BY queued.created_at,queued.id LIMIT 1
          ) pending_action
          WHERE c.case_id=$1 AND NOT c.opted_out AND a.status='released'
            AND EXISTS(SELECT 1 FROM supplier_conversation_item_turns t
              JOIN supplier_conversation_targets target ON target.id=t.target_id
              WHERE t.conversation_id=c.id
                AND t.status IN ('awaiting_approval','pending_reply','needs_time','ambiguous')
                AND target.request_item_id=a.request_item_id)
          ORDER BY c.id,pending_action.created_at,pending_action.action_id
        ) priority ORDER BY created_at,action_id LIMIT $2""",
        case_id,
        limit,
    )
    reacquired = 0
    for row in rows:
        try:
            await ensure_dialogue_slot(connection, row["id"])
        except WhatsAppSendDeferred:
            # Preserve the queued native action. A later release/wake retries it.
            continue
        active_assignments = await connection.fetchval(
            "SELECT count(*) FROM supplier_dialogue_assignments "
            "WHERE conversation_id=$1 AND status IN ('assigned','waiting_reply')",
            row["id"],
        )
        reacquired += active_assignments > 0
    return reacquired


async def prepare_due_reminders(connection: asyncpg.Connection, case_id: UUID) -> int:
    rows = await connection.fetch(
        """SELECT c.id,c.supplier_id,c.supplier_contact_id,c.external_chat_id,c.case_id,
          c.session_name,c.preferred_language,pc.owner_user_id,sc.normalized_value,
          t.id AS turn_id,t.draft_text
        FROM supplier_conversations c JOIN procurement_cases pc ON pc.id=c.case_id
        JOIN supplier_contacts sc ON sc.id=c.supplier_contact_id
        JOIN supplier_conversation_item_turns t ON t.conversation_id=c.id
        WHERE c.case_id=$1 AND NOT c.opted_out AND sc.active AND c.reminder_count=0
          AND c.reply_due_at<=now() AND t.status IN ('pending_reply','needs_time')
          AND (c.last_inbound_at IS NULL OR c.last_inbound_at<c.last_outbound_at
            OR t.status='needs_time')
          AND (t.next_check_at IS NULL OR t.next_check_at<=now())
          AND EXISTS(SELECT 1 FROM case_auto_send_policies policy
            WHERE policy.case_id=c.case_id AND policy.owner_user_id=pc.owner_user_id
              AND policy.revoked_at IS NULL)
          AND c.state IN ('waiting_availability','waiting_spec','waiting_price')
          AND EXISTS(SELECT 1 FROM supplier_conversation_messages m
            JOIN whatsapp_delivery_states d ON d.message_id=m.id
            WHERE m.conversation_id=c.id AND m.direction='outbound' AND d.ack>=2
              AND m.received_at=c.last_outbound_at)
        ORDER BY c.reply_due_at LIMIT 10 FOR UPDATE OF c""",
        case_id,
    )
    prepared = 0
    for row in rows:
        command = WhatsAppSendCommand(
            case_id=case_id,
            recipient=row["normalized_value"],
            conversation_id=row["id"],
            text=(
                "Сәлеметсіз бе! Сұрағымызды еске саламыз: "
                if row["preferred_language"] == "kk"
                else "Здравствуйте! Напоминаю наш вопрос: "
            )
            + row["draft_text"],
            logical_action_id=f"timed-reminder:{row['turn_id']}",
            is_followup=True,
        )
        if len(command.text) > 4096:
            continue
        approval_id = await request_approval(
            connection,
            case_id=case_id,
            action_type="send_whatsapp",
            payload=command.approval_payload(),
            requested_by=row["owner_user_id"],
            expires_at=datetime.now(UTC) + timedelta(hours=48),
        )
        job = await authorize_and_enqueue_auto_whatsapp(
            connection,
            approval_id=approval_id,
            case_id=case_id,
            payload=command.approval_payload(),
            requested_by=row["owner_user_id"],
        )
        if job is None:
            continue
        await connection.execute(
            """UPDATE supplier_conversations SET reminder_count=1,
            reply_due_at=now()+interval '24 hours',waiting_reason='reminder_queued' WHERE id=$1""",
            row["id"],
        )
        prepared += 1
    return prepared


def _report_signature(value):
    """Probe clocks do not create a new report without an operational change."""
    if isinstance(value, dict):
        clocks = {
            "next_probe_at",
            "provider_observed_at",
            "next_wake_at",
            "next_job_at",
            "next_tick_at",
            "last_tick_at",
            "last_seen_at",
            "next_provider_probe_at",
            "ingress",
        }
        return {key: _report_signature(item) for key, item in value.items() if key not in clocks}
    if isinstance(value, (list, tuple)):
        return [_report_signature(item) for item in value]
    return value


async def run_pipeline_tick(worker, case_id: UUID) -> None:
    async with worker.pool.acquire() as c:
        runtime = await c.fetchrow(
            "SELECT research_run_id,last_tick_summary FROM case_pipeline_runtime WHERE case_id=$1",
            case_id,
        )
        if runtime is None or runtime["research_run_id"] is None:
            return
        run_id = runtime["research_run_id"]
        if not await autonomy_enabled(c, run_id):
            return
        previous_summary = runtime["last_tick_summary"] or {}
        if not isinstance(previous_summary, dict):
            previous_summary = {}
        metrics_before = previous_summary.get("metrics")
    from procurement_bot.outreach_schedule import coalesce_legacy_outreach, request_outreach
    from procurement_bot.transport_recovery import (
        probe_account,
        reconcile_lid_quarantine,
        reconcile_reserved_sends,
    )

    summary = {}
    account = {"outbound_allowed": False}
    if worker.waha_client is not None:
        account = await probe_account(worker.pool, worker.waha_client)
        summary["account"] = account
        summary.update(await reconcile_transport(worker.pool, worker.waha_client, case_id))
        if summary.get("waha_status") == "WORKING":
            summary["reserved_reconciliations"] = await reconcile_reserved_sends(
                worker.pool, worker.waha_client, case_id
            )
            summary["lid_resolved"] = await reconcile_lid_quarantine(
                worker.pool, worker.waha_client, case_id
            )
    executor = worker.research_executor
    if executor is not None and hasattr(executor, "recover_due_branches"):
        summary["recovered_branches"] = await executor.recover_due_branches(run_id)
    if executor is not None and hasattr(executor, "reconcile_runnable_checkpoints"):
        summary["reconciled_checkpoints"] = await executor.reconcile_runnable_checkpoints(run_id)
    async with worker.pool.acquire() as c, c.transaction():
        if not await autonomy_enabled(c, run_id):
            return
        await c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            stable_key("supplier-outreach-case-lock", case_id),
        )
        released = await expire_waiting_slots(c, case_id)
        from procurement_bot.pipeline_reviews import reconcile_reviews

        reviews = await reconcile_reviews(c, case_id)
        summary["reviews"] = reviews["counts"]
        summary["released_assignments"] = released
        summary["coalesced_outreach_jobs"] = await coalesce_legacy_outreach(c, case_id)
        outbound_ready = account["outbound_allowed"] and bool(
            getattr(worker.waha_client, "outbound_enabled", False)
        )
        summary["reminders_prepared"] = (
            await prepare_due_reminders(c, case_id) if outbound_ready else 0
        )
        summary["priority_slots_reacquired"] = (
            await reacquire_queued_dialogue_slots(c, case_id) if outbound_ready else 0
        )
        if executor is not None and hasattr(executor, "load_research_progress"):
            progress = await executor.load_research_progress(c, run_id)
            summary["research_progress"] = {
                key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in progress.items()
            }

        metrics_after = await load_pipeline_metrics(c, case_id)
        summary["metrics"] = metrics_after
        await c.execute(
            "UPDATE case_pipeline_runtime SET last_tick_summary=$2::jsonb WHERE case_id=$1",
            case_id,
            summary,
        )
        generation = await c.fetchval(
            "SELECT tick_generation FROM case_pipeline_runtime WHERE case_id=$1", case_id
        )
        if outbound_ready:
            await request_outreach(
                c, case_id=case_id, source_key=stable_key("pipeline-outreach", case_id, generation)
            )
        report_changed = (
            (
                metrics_before is not None
                and _report_signature(metrics_before) != _report_signature(metrics_after)
            )
            or previous_summary.get("waha_status") != summary.get("waha_status")
            or (previous_summary.get("account") != summary.get("account"))
            or any(
                summary.get(key, 0)
                for key in (
                    "recovered_branches",
                    "reconciled_checkpoints",
                    "released_assignments",
                    "reminders_prepared",
                    "priority_slots_reacquired",
                )
            )
        )
        if report_changed:
            await enqueue_job(
                c,
                kind="refresh_report",
                payload={"case_id": str(case_id), "trigger_id": f"pipeline:{generation}"},
                idempotency_key=stable_key("pipeline-report", case_id, generation),
            )
