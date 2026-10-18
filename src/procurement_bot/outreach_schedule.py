from __future__ import annotations

from uuid import UUID

import asyncpg

from procurement_bot.queue import enqueue_job, stable_key


async def _lock_request(connection: asyncpg.Connection, case_id: UUID):
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
        stable_key("outreach-work", case_id),
    )
    await connection.execute(
        "INSERT INTO case_outreach_requests(case_id) VALUES($1) ON CONFLICT DO NOTHING",
        case_id,
    )
    return await connection.fetchrow(
        "SELECT * FROM case_outreach_requests WHERE case_id=$1 FOR UPDATE", case_id
    )


async def request_outreach(
    connection: asyncpg.Connection, *, case_id: UUID, source_key: str
) -> UUID | None:
    """One preparation job per case; dirty generations retain changes during a hold."""
    if not source_key or len(source_key) > 500:
        raise ValueError("invalid outreach source key")
    async with connection.transaction():
        row = await _lock_request(connection, case_id)
        inserted = await connection.fetchval(
            "INSERT INTO case_outreach_sources(case_id,source_key) VALUES($1,$2) "
            "ON CONFLICT DO NOTHING RETURNING source_key",
            case_id,
            source_key,
        )
        generation = row["dirty_generation"] + bool(inserted)
        await connection.execute(
            "UPDATE case_outreach_requests SET dirty_generation=$2,updated_at=now() "
            "WHERE case_id=$1",
            case_id,
            generation,
        )
        job_id = row["current_job_id"]
        if job_id is not None:
            status = await connection.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
            if status in {"pending", "retry", "running"}:
                return job_id
        if generation <= row["prepared_generation"]:
            return None
        job = await enqueue_job(
            connection,
            kind="prepare_supplier_outreach",
            payload={"case_id": str(case_id)},
            idempotency_key=stable_key("coalesced-outreach", case_id, generation),
            max_attempts=20,
        )
        if job is None:
            job_id = await connection.fetchval(
                "SELECT id FROM jobs WHERE idempotency_key=$1",
                stable_key("coalesced-outreach", case_id, generation),
            )
            status = await connection.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
            if status not in {"pending", "retry", "running"}:
                # A completed preparation with a stale marker must get a fresh identity.
                generation += 1
                job = await enqueue_job(
                    connection,
                    kind="prepare_supplier_outreach",
                    payload={"case_id": str(case_id)},
                    idempotency_key=stable_key("coalesced-outreach", case_id, generation),
                    max_attempts=20,
                )
                job_id = job if job is not None else None
        else:
            job_id = job
        await connection.execute(
            "UPDATE case_outreach_requests SET current_job_id=$2,dirty_generation=$3,"
            "updated_at=now() WHERE case_id=$1",
            case_id,
            job_id,
            generation,
        )
        return job_id


async def begin_outreach(
    connection: asyncpg.Connection, *, case_id: UUID, job_id: UUID
) -> int | None:
    async with connection.transaction():
        row = await _lock_request(connection, case_id)
        if row["current_job_id"] not in {None, job_id}:
            status = await connection.fetchval(
                "SELECT status FROM jobs WHERE id=$1", row["current_job_id"]
            )
            if status in {"pending", "retry", "running"}:
                return None
        generation = max(row["dirty_generation"], row["prepared_generation"] + 1)
        await connection.execute(
            "UPDATE case_outreach_requests SET current_job_id=$2,dirty_generation=$3,"
            "updated_at=now() WHERE case_id=$1",
            case_id,
            job_id,
            generation,
        )
        return generation


async def finish_outreach(
    connection: asyncpg.Connection, *, case_id: UUID, job_id: UUID, generation: int
) -> None:
    async with connection.transaction():
        row = await _lock_request(connection, case_id)
        if row["current_job_id"] != job_id:
            return
        await connection.execute(
            "UPDATE case_outreach_requests SET prepared_generation=GREATEST("
            "prepared_generation,$3),current_job_id=NULL,updated_at=now() "
            "WHERE case_id=$1 AND current_job_id=$2",
            case_id,
            job_id,
            generation,
        )
        if row["dirty_generation"] > generation:
            await request_outreach(
                connection,
                case_id=case_id,
                source_key=stable_key("outreach-dirty-after", job_id, row["dirty_generation"]),
            )


async def coalesce_legacy_outreach(connection: asyncpg.Connection, case_id: UUID) -> int:
    """Retire only unclaimed duplicate preparation work, recording its old metadata."""
    async with connection.transaction():
        row = await _lock_request(connection, case_id)
        jobs = await connection.fetch(
            "SELECT id,status FROM jobs WHERE kind='prepare_supplier_outreach' "
            "AND payload->>'case_id'=$1 AND status IN ('pending','retry','running') "
            "ORDER BY (status='running') DESC,available_at,created_at,id FOR UPDATE",
            str(case_id),
        )
        if not jobs:
            return 0
        ids = {j["id"] for j in jobs}
        canonical = row["current_job_id"] if row["current_job_id"] in ids else jobs[0]["id"]
        await connection.execute(
            "UPDATE case_outreach_requests SET current_job_id=$2,"
            "dirty_generation=GREATEST(dirty_generation,prepared_generation+1),"
            "updated_at=now() WHERE case_id=$1",
            case_id,
            canonical,
        )
        retired = await connection.fetch(
            "WITH archived AS (INSERT INTO outreach_coalesced_jobs("
            "original_job_id,canonical_job_id,previous_error_code,previous_attempts) "
            "SELECT id,$2,last_error_code,attempts FROM jobs "
            "WHERE kind='prepare_supplier_outreach' "
            "AND payload->>'case_id'=$1 AND id<>$2 AND status IN ('pending','retry') "
            "ON CONFLICT DO NOTHING RETURNING original_job_id) "
            "UPDATE jobs j SET status='succeeded',completed_at=now(),"
            "last_error_code='outreach_coalesced',updated_at=now() FROM archived a "
            "WHERE j.id=a.original_job_id RETURNING j.id",
            str(case_id),
            canonical,
        )
        return len(retired)
