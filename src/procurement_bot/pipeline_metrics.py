from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

import asyncpg


async def load_pipeline_metrics(connection: asyncpg.Connection, case_id: UUID) -> dict[str, int]:
    """Load offer, transport, and webhook counts without mixing their meanings."""
    row = await connection.fetchrow(
        """WITH accepted_events AS (
               SELECT journal.outbox_event_id
               FROM whatsapp_send_acceptance_journal journal
               JOIN outbox_events event ON event.id=journal.outbox_event_id
               WHERE event.payload->>'case_id'=$1::uuid::text
               UNION
               SELECT reservation.outbox_event_id
               FROM whatsapp_account_send_reservations reservation
               JOIN supplier_conversations conversation
                 ON conversation.id=reservation.conversation_id
               WHERE conversation.case_id=$1::uuid AND reservation.state='accepted'
               UNION
               SELECT message.outbox_event_id
               FROM supplier_conversation_messages message
               JOIN supplier_conversations conversation
                 ON conversation.id=message.conversation_id
               JOIN whatsapp_delivery_states delivery ON delivery.message_id=message.id
               WHERE conversation.case_id=$1::uuid AND message.direction='outbound'
                 AND delivery.ack>=0 AND message.outbox_event_id IS NOT NULL
           )
           SELECT
              (SELECT count(*) FROM offers o JOIN request_items i ON i.id=o.request_item_id
                WHERE i.case_id=$1::uuid AND o.status='lead')::int AS leads,
              (SELECT count(*) FROM offer_observations obs JOIN offers o ON o.id=obs.offer_id
                JOIN request_items i ON i.id=o.request_item_id WHERE i.case_id=$1::uuid
                AND obs.price_amount IS NOT NULL AND obs.source_type IN
                ('2gis','web','supplier_site','marketplace'))::int AS public_prices,
              (SELECT count(*) FROM offer_observations obs JOIN offers o ON o.id=obs.offer_id
                JOIN request_items i ON i.id=o.request_item_id WHERE i.case_id=$1::uuid
                AND obs.price_amount IS NOT NULL AND obs.source_type='supplier_message')::int
                AS supplier_prices,
              (SELECT count(*) FROM supplier_conversation_messages m
                JOIN supplier_conversations c ON c.id=m.conversation_id WHERE c.case_id=$1::uuid
                AND m.direction='outbound')::int AS outbound_projected,
              (SELECT count(*) FROM accepted_events)::int AS accepted,
              (SELECT count(*) FROM supplier_conversation_messages m
                JOIN supplier_conversations c ON c.id=m.conversation_id
                LEFT JOIN whatsapp_delivery_states d ON d.message_id=m.id
                WHERE c.case_id=$1::uuid AND m.direction='outbound'
                AND (d.ack IS NULL OR d.ack IN (0,1)))::int AS delivery_unknown,
              (SELECT count(*) FROM outbox_events e WHERE e.event_type='whatsapp.send_text'
                AND e.payload->>'case_id'=$1::uuid::text
                AND e.status IN ('pending','retry','sending')
                AND e.last_error_code='provider_acceptance_unknown')::int AS acceptance_unknown,
              (SELECT count(*) FROM accepted_events accepted
                WHERE NOT EXISTS (SELECT 1 FROM supplier_conversation_messages message
                    WHERE message.outbox_event_id=accepted.outbox_event_id
                      AND message.direction='outbound'))::int AS accepted_pending_projection,
              (SELECT count(*) FROM whatsapp_delivery_states d JOIN supplier_conversation_messages m
                ON m.id=d.message_id JOIN supplier_conversations c ON c.id=m.conversation_id
                WHERE c.case_id=$1::uuid AND d.ack>=2)::int AS delivered,
              (SELECT count(*) FROM whatsapp_delivery_states d JOIN supplier_conversation_messages m
                ON m.id=d.message_id JOIN supplier_conversations c ON c.id=m.conversation_id
                WHERE c.case_id=$1::uuid AND d.ack>=3)::int AS read,
              (SELECT count(*) FROM whatsapp_delivery_states d JOIN supplier_conversation_messages m
                ON m.id=d.message_id JOIN supplier_conversations c ON c.id=m.conversation_id
                WHERE c.case_id=$1::uuid AND d.ack=-1)::int AS delivery_failed,
              (SELECT count(*) FROM supplier_conversations c
                WHERE c.case_id=$1::uuid AND NOT c.opted_out
                AND c.state NOT IN ('complete','closed_no_reply','opted_out'))::int
                AS waiting_chats,
              (SELECT count(DISTINCT a.group_id) FROM supplier_dialogue_assignments a
                JOIN supplier_conversations c ON c.id=a.conversation_id WHERE c.case_id=$1::uuid
                AND a.status IN ('assigned','waiting_reply'))::int AS active_groups""",
        case_id,
    )
    return dict(row)


async def load_pipeline_health(
    connection: asyncpg.Connection, case_id: UUID
) -> dict[str, Any]:
    """Read aggregate account, ingress, and research scheduler state for a case."""
    account = await connection.fetchrow(
        """WITH case_sessions AS (
               SELECT conversation.session_name
               FROM supplier_conversations conversation WHERE conversation.case_id=$1::uuid
               UNION
               SELECT reservation.session_name
               FROM whatsapp_account_send_reservations reservation
               JOIN supplier_conversations conversation
                 ON conversation.id=reservation.conversation_id
               WHERE conversation.case_id=$1::uuid
               UNION
               SELECT journal.session_name
               FROM whatsapp_send_acceptance_journal journal
               JOIN outbox_events event ON event.id=journal.outbox_event_id
               WHERE event.payload->>'case_id'=$1::uuid::text
           )
           SELECT count(*)::int AS accounts,
               count(*) FILTER (WHERE controls.session_name IS NULL
                   OR controls.operator_held)::int AS held,
               count(*) FILTER (WHERE controls.circuit_open)::int AS circuits_open,
               count(*) FILTER (WHERE controls.provider_state IN
                   ('restricted','timelock','capped'))::int AS restricted,
               count(*) FILTER (WHERE controls.provider_state='unavailable')::int
                   AS unavailable,
               count(*) FILTER (WHERE controls.session_name IS NULL
                   OR controls.provider_state='unknown'
                   OR controls.provider_observed_at IS NULL
                   OR controls.provider_observed_at>now())::int AS state_unknown,
               count(*) FILTER (WHERE controls.provider_observed_at<now()-interval '30 minutes'
                   OR controls.provider_observed_at>now())
                   ::int AS state_stale,
               count(*) FILTER (WHERE controls.provider_state='clear')::int AS state_clear,
               count(*) FILTER (WHERE NOT controls.operator_held AND NOT controls.circuit_open
                   AND controls.provider_state='clear'
                   AND controls.provider_observed_at>=now()-interval '30 minutes'
                   AND controls.provider_observed_at<=now()
                   AND (controls.provider_restriction_until IS NULL
                        OR controls.provider_restriction_until<=now())
                   AND NOT EXISTS (
                       SELECT 1 FROM whatsapp_account_send_reservations unresolved
                       WHERE unresolved.session_name=sessions.session_name
                         AND unresolved.state IN ('reserved','unknown')
                   ))::int AS outbound_allowed,
               min(controls.next_probe_at) AS next_probe_at,
               max(controls.provider_observed_at) AS provider_observed_at,
               COALESCE(sum(controls.daily_message_limit),0)::int AS daily_message_limit,
               COALESCE(sum(controls.daily_new_contact_limit),0)::int
                   AS daily_new_contact_limit,
               COALESCE(sum(controls.max_concurrent_posts),0)::int
                   AS max_concurrent_posts,
               (SELECT count(*)::int FROM whatsapp_account_send_reservations reservation
                WHERE reservation.session_name IN (SELECT session_name FROM case_sessions)
                  AND reservation.state IN ('reserved','unknown')) AS unresolved_reservations
           FROM case_sessions sessions
           LEFT JOIN whatsapp_account_controls controls
             ON controls.session_name=sessions.session_name""",
        case_id,
    )
    ingress = await connection.fetchrow(
        """SELECT
             COALESCE(sum(observation_count) FILTER (WHERE disposition='accepted'),0)::bigint
                AS accepted_7d,
             COALESCE(sum(observation_count) FILTER (WHERE disposition='ignored'),0)::bigint
                AS ignored_7d,
             COALESCE(sum(observation_count) FILTER (WHERE disposition='rejected'),0)::bigint
                AS rejected_7d,
             max(last_seen_at) AS last_seen_at
           FROM whatsapp_ingress_audit
           WHERE utc_day >= (now() AT TIME ZONE 'UTC')::date - 6"""
    )
    run = await connection.fetchrow(
        """SELECT run.id,run.status,run.created_at,
               max(checkpoint.last_real_progress_at) AS last_real_progress_at,
               count(checkpoint.id) FILTER (WHERE checkpoint.status='queued'
                   AND NOT EXISTS (
                       SELECT 1 FROM jobs live
                       WHERE live.kind='parallel_research_task'
                         AND live.payload->>'task_id'=checkpoint.id::text
                         AND live.status IN ('pending','retry','running')
                   ))::int AS queued_without_runnable_job,
               count(checkpoint.id) FILTER (WHERE checkpoint.status='queued'
                   AND checkpoint.current_job_id IS NOT NULL
                   AND (job.id IS NULL OR job.status IN ('succeeded','dead'))
               )::int AS queued_with_terminal_job,
               count(checkpoint.id) FILTER (WHERE checkpoint.status='researching'
                   AND NOT EXISTS (
                       SELECT 1 FROM jobs live
                       WHERE live.kind='parallel_research_task'
                         AND live.payload->>'task_id'=checkpoint.id::text
                         AND live.status='running' AND live.lease_expires_at>now()
                   ))::int AS researching_without_live_owner,
               wait.reason AS provider_wait_reason,
               wait.next_probe_at AS provider_next_probe_at,
               runtime.enabled,runtime.paused,runtime.wake_reason,
               runtime.next_tick_at,runtime.last_tick_at,runtime.last_tick_summary,
               (SELECT min(queued.available_at) FROM jobs queued
                WHERE queued.kind='parallel_research_task'
                  AND queued.status IN ('pending','retry')
                  AND queued.payload->>'research_run_id'=run.id::text) AS next_job_at
           FROM research_runs run
           LEFT JOIN research_branch_checkpoints checkpoint
             ON checkpoint.research_run_id=run.id
           LEFT JOIN jobs job ON job.id=checkpoint.current_job_id
           LEFT JOIN research_provider_waits wait ON wait.research_run_id=run.id
           LEFT JOIN case_pipeline_runtime runtime ON runtime.research_run_id=run.id
           WHERE run.case_id=$1
           GROUP BY run.id,wait.reason,wait.next_probe_at,runtime.enabled,runtime.paused,
             runtime.wake_reason,runtime.next_tick_at,runtime.last_tick_at,
             runtime.last_tick_summary
           ORDER BY run.created_at DESC,run.id DESC LIMIT 1""",
        case_id,
    )
    if run is None:
        research: dict[str, Any] = {
            "run_status": None,
            "last_real_progress_at": None,
            "wait_reason": "no_research_run",
            "next_wake_at": None,
            "queued_without_runnable_job": 0,
            "queued_with_terminal_job": 0,
            "stalled": False,
        }
    else:
        next_times = [value for value in (
            run["provider_next_probe_at"], run["next_job_at"]
        ) if value is not None]
        if run["enabled"] and not run["paused"] and run["next_tick_at"] is not None:
            next_times.append(run["next_tick_at"])
        queued_orphans = int(run["queued_with_terminal_job"] or 0)
        queued_unowned = int(run["queued_without_runnable_job"] or 0)
        researching_unowned = int(run["researching_without_live_owner"] or 0)
        live_count = await connection.fetchval(
            """SELECT count(*) FROM jobs WHERE kind='parallel_research_task'
               AND payload->>'research_run_id'=$1::uuid::text
               AND status IN ('pending','retry','running')""",
            run["id"],
        )
        stalled = bool(
            run["status"] == "running" and not run["paused"]
            and not live_count and (queued_unowned or researching_unowned)
            and not run["provider_next_probe_at"]
        )
        if run["paused"]:
            wait_reason = "owner_paused"
        elif run["provider_wait_reason"]:
            wait_reason = "provider_quota_wait"
        elif queued_orphans:
            wait_reason = "queued_checkpoint_terminal_job"
        elif researching_unowned and not live_count:
            wait_reason = "researching_checkpoint_without_live_owner"
        elif stalled:
            wait_reason = "queued_checkpoint_without_runnable_job"
        elif run["status"] != "running":
            wait_reason = "run_not_running"
        elif live_count:
            wait_reason = "queue_job_active_or_scheduled"
        else:
            wait_reason = "no_runnable_work"
        research = {
            "run_status": run["status"],
            "last_real_progress_at": run["last_real_progress_at"],
            "wait_reason": wait_reason,
            "runtime_wake_reason": run["wake_reason"],
            "provider_wait_reason": run["provider_wait_reason"],
            "next_wake_at": min(next_times) if next_times else None,
            "next_provider_probe_at": run["provider_next_probe_at"],
            "queued_without_runnable_job": queued_unowned,
            "queued_with_terminal_job": queued_orphans,
            "researching_without_live_owner": researching_unowned,
            "stalled": stalled,
            "live_research_jobs": int(live_count or 0),
            "last_tick_at": run["last_tick_at"],
        }
    health = {
        "account": dict(account) if account is not None else {},
        "ingress": {
            **(dict(ingress) if ingress is not None else {}),
            "window_days": 7,
        },
        "research": research,
    }
    health["account"] = dict(account) if account is not None else {}
    if run is not None:
        tick_summary = run["last_tick_summary"] or {}
        if isinstance(tick_summary, str):
            import json

            try:
                tick_summary = json.loads(tick_summary)
            except json.JSONDecodeError:
                tick_summary = {}
        if isinstance(tick_summary, dict):
            health["account"]["session_status"] = tick_summary.get("waha_status")
    return _json_safe(health)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value
