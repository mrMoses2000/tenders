from __future__ import annotations

from procurement_bot.pipeline_runtime import is_quota_failure


async def recover_intent(connection, task):
    """Use a separate outcome for retries; retain original terminal reasons."""
    intent = await connection.fetchrow(
        """SELECT intent.id,intent.ordinal,intent.cursor_state,intent.results,
        COALESCE(intent.recovery_stop_reason,intent.stop_reason) AS failure
        FROM research_branch_checkpoints cp JOIN research_item_groups g ON g.id=cp.group_id
        JOIN research_queries q ON q.id=cp.query_id
        JOIN research_search_profiles profile ON profile.research_run_id=cp.research_run_id
          AND profile.request_item_id=g.request_item_id
        JOIN research_search_intents intent ON intent.profile_id=profile.id
          AND intent.provider=q.provider
        WHERE cp.id=$1 AND profile.profile_version=(
          SELECT max(p.profile_version) FROM research_search_profiles p
          WHERE p.research_run_id=profile.research_run_id
            AND p.request_item_id=profile.request_item_id)
          AND (intent.recovery_due_at IS NULL OR intent.recovery_due_at<=now())
          AND (COALESCE(intent.recovery_stop_reason,intent.stop_reason) LIKE '%cli_usage_limit%'
            OR COALESCE(intent.recovery_stop_reason,intent.stop_reason) LIKE '%cli_rate_limit%'
            OR (intent.recovery_cycles<3 AND (
              COALESCE(intent.recovery_stop_reason,intent.stop_reason)
                LIKE 'provider_failure:RetryableProviderError:%'
              OR COALESCE(intent.recovery_stop_reason,intent.stop_reason)
                LIKE 'provider_failure:BrowserProviderError:%')))
        ORDER BY (intent.ordinal=cp.semantic_intent_ordinal) DESC,intent.ordinal LIMIT 1
        FOR UPDATE OF intent""",
        task["id"],
    )
    if intent is None:
        return None
    await connection.execute(
        """UPDATE research_search_intents SET recovery_stop_reason='',
        recovery_cycles=recovery_cycles+1,recovery_due_at=now()+interval '30 minutes',
        updated_at=now()
        WHERE id=$1""",
        intent["id"],
    )
    if int(intent["ordinal"]) != int(task["semantic_intent_ordinal"]):
        # Cursor belongs to one semantic query. Restore that query's saved cursor,
        # and archive the replaced checkpoint cursor in the recovery event.
        await connection.execute(
            """UPDATE research_branch_checkpoints SET semantic_intent_ordinal=$2,
            search_cursor_state=$3::jsonb,source_progress_token=$4 WHERE id=$1""",
            task["id"],
            intent["ordinal"],
            intent["cursor_state"],
            intent["results"].get("source_progress_token"),
        )
    return {
        "intent_id": str(intent["id"]),
        "ordinal": intent["ordinal"],
        "quota": is_quota_failure(intent["failure"]),
    }


async def persist_intent_progress(connection, *, task, ordinal, cursor, token):
    if ordinal is None:
        return
    await connection.execute(
        """UPDATE research_search_intents intent SET cursor_state=$3::jsonb,
        results=results || jsonb_build_object('source_progress_token',$4::text),updated_at=now()
        FROM research_search_profiles profile JOIN research_item_groups g
          ON g.request_item_id=profile.request_item_id AND g.research_run_id=profile.research_run_id
        JOIN research_queries q ON q.id=$5
        WHERE intent.profile_id=profile.id AND g.id=$1 AND intent.ordinal=$2
          AND intent.provider=q.provider AND profile.profile_version=(
            SELECT max(p.profile_version) FROM research_search_profiles p
            WHERE p.research_run_id=profile.research_run_id
            AND p.request_item_id=profile.request_item_id)
        """,
        task["group_id"],
        ordinal,
        cursor,
        token,
        task["query_id"],
    )
