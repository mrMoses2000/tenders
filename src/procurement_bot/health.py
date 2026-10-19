from __future__ import annotations

from typing import Any

import asyncpg


def provider_auth_snapshot(settings) -> dict[str, Any]:
    """Check configured credential availability without exposing or refreshing it."""
    import os
    from pathlib import Path

    text_provider = getattr(settings, "structured_text_provider", "agy")
    if text_provider == "codex":
        try:
            executables = (settings.codex_text_executable, settings.codex_text_node_executable)
            if not all(path.is_file() and os.access(path, os.X_OK) for path in executables):
                return {"available": False, "reason": "text_provider_executable_missing"}
            # Metadata only: do not open auth files or claim account/model eligibility.
            auth = Path(settings.codex_text_codex_home) / "auth.json"
            if not auth.is_file() or auth.stat().st_size == 0:
                return {"available": False, "reason": "provider_auth_missing"}
        except OSError:
            return {"available": False, "reason": "provider_auth_missing"}
    agy_browser_required = (
        getattr(settings, "browser_research_enabled", False)
        and not (getattr(settings, "staged_research_enabled", False)
                 and getattr(settings, "staged_research_primary", "agy") == "codex")
    )
    agy_required = (
        text_provider == "agy"
        or getattr(settings, "voice_provider", "disabled") == "agy"
        or getattr(settings, "supplier_media_enabled", False)
        or agy_browser_required
    )
    if agy_required:
        try:
            settings._available_agy_auth_token()
        except (ValueError, OSError):
            return {"available": False, "reason": "provider_auth_missing"}
    return {"available": True, "reason": "credentials_present_not_account_verified"}


async def readiness_snapshot(connection) -> dict[str, Any]:
    """Current execution readiness, independent of historical dead-job counts."""
    row = await connection.fetchrow(
        """SELECT
        (SELECT count(*) FROM jobs WHERE status='running' AND lease_expires_at<=now())
            AS expired_job_leases,
        (SELECT count(*) FROM outbox_events WHERE status='sending' AND lease_expires_at<=now())
            AS expired_outbox_leases,
        (SELECT count(*) FROM jobs WHERE status IN ('pending','retry')
            AND available_at<now()-interval '30 minutes') AS overdue_jobs,
        (SELECT count(*) FROM case_pipeline_runtime WHERE enabled AND NOT paused
            AND next_tick_at<now()-interval '90 minutes') AS overdue_pipelines,
        (SELECT count(*) FROM pipeline_reviews WHERE state='open') AS open_reviews"""
    )
    counts = dict(row)
    counts["provider_auth_waiting_jobs"] = await connection.fetchval(
        "SELECT count(*) FROM jobs WHERE status IN ('pending','retry') "
        "AND last_error_code IN ('provider_auth_missing','provider_auth_failed')"
    )
    provider_authentication_blocked = counts["provider_auth_waiting_jobs"] > 0
    execution_ready = not any(counts[key] for key in (
        "expired_job_leases", "expired_outbox_leases", "overdue_jobs", "overdue_pipelines",
    )) and not provider_authentication_blocked
    return {"status": "ready" if execution_ready else "degraded",
            "database": "ok", "execution_ready": execution_ready,
            "provider_authentication_blocked": provider_authentication_blocked, **counts}


async def health_snapshot(pool: asyncpg.Pool, *, settings=None) -> dict[str, Any]:
    async with pool.acquire() as connection:
        await connection.fetchval("SELECT 1")
        job_rows = await connection.fetch(
            "SELECT status,count(*) AS count FROM jobs GROUP BY status ORDER BY status"
        )
        outbox_rows = await connection.fetch(
            "SELECT status,count(*) AS count FROM outbox_events GROUP BY status ORDER BY status"
        )
        cases = await connection.fetchval(
            "SELECT count(*) FROM procurement_cases WHERE status NOT IN ('closed','cancelled')"
        )
        stale_jobs = await connection.fetchval(
            """
            SELECT count(*) FROM jobs
            WHERE status='running' AND lease_expires_at<=now()
            """
        )
        stale_outbox = await connection.fetchval(
            """
            SELECT count(*) FROM outbox_events
            WHERE status='sending' AND lease_expires_at<=now()
            """
        )
        readiness = await readiness_snapshot(connection)
        if settings is not None:
            readiness["provider_auth"] = provider_auth_snapshot(settings)
    job_counts = {row["status"]: row["count"] for row in job_rows}
    outbox_counts = {row["status"]: row["count"] for row in outbox_rows}
    degraded = any(
        (
            stale_jobs,
            stale_outbox,
            job_counts.get("dead", 0),
            outbox_counts.get("dead", 0),
            readiness["provider_authentication_blocked"],
            settings is not None and not readiness["provider_auth"]["available"],
        )
    )
    return {
        "status": "degraded" if degraded else "ok",
        "database": "ok",
        "jobs": job_counts,
        "outbox": outbox_counts,
        "active_cases": cases,
        "expired_job_leases": stale_jobs,
        "expired_outbox_leases": stale_outbox,
        "readiness": readiness,
    }
