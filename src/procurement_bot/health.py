from __future__ import annotations

from typing import Any

import asyncpg


async def health_snapshot(pool: asyncpg.Pool) -> dict[str, Any]:
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
    job_counts = {row["status"]: row["count"] for row in job_rows}
    outbox_counts = {row["status"]: row["count"] for row in outbox_rows}
    degraded = any(
        (
            stale_jobs,
            stale_outbox,
            job_counts.get("dead", 0),
            outbox_counts.get("dead", 0),
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
    }
