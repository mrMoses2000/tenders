from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import ValidationError

from procurement_bot.errors import (
    PermanentProviderError,
    ProviderAuthenticationDeferred,
    RetryableProviderError,
)
from procurement_bot.geographic_scope import GeographicDecision, decide_geographic_scope
from procurement_bot.pipeline_runtime import (
    ProviderQuotaDeferred,
    before_provider_call,
    is_quota_failure,
    provider_succeeded,
    sleep_for_quota,
)
from procurement_bot.providers.agy import StructuredExtractor
from procurement_bot.providers.browser import BrowserProviderError
from procurement_bot.providers.provider_diagnostics import (
    safe_error_code as safe_provider_error_code,
)
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.research import (
    LocalityConstraint,
    PrivatePriceLeakError,
    ResearchSource,
)
from procurement_bot.research_execution import (
    ResearchExecutionRejected,
    ResearchExecutionResult,
    ResearchPlanDrift,
    ResearchRunExecutor,
    _connection,
    _QueryBranch,
    _validate_browser_result,
    _web_plan_with_catalog_links,
)
from procurement_bot.research_recovery import persist_intent_progress, recover_intent
from procurement_bot.research_scope import load_effective_scope
from procurement_bot.search_profiles import ensure_search_profiles, load_search_intent_plan

LOGGER = logging.getLogger(__name__)
_SCHEDULER_LOCK_KEY = 731940251
_JOB_KIND = "parallel_research_task"
_MAX_BATCH_ATTEMPTS = 2
_MAX_BATCHES_PER_QUERY = 20
_BATCH_CANDIDATE_LIMIT = 2
_OLX_BATCH_CANDIDATE_LIMIT = 1
_OPERATOR_RETRY_BACKOFF_SECONDS = 30
_OPERATOR_RETRY_MARKER = "operator_retry_pending"
_RETRYABLE_TECHNICAL_FAILURE_PREFIXES = (
    "provider_failure:RetryableProviderError:",
    "provider_failure:BrowserProviderError:",
)
_PROFILE_LOCK_LIMIT = 256


class ParallelResearchRunExecutor:
    """Durable coordinator for bounded, independently leased research batches.

    Existing ResearchRunExecutor remains the owner of snapshot/hash validation and
    evidence persistence. This class only coordinates item groups, lane checkpoints,
    and queue jobs; it never has supplier messaging capabilities.
    """

    def __init__(
        self,
        database: asyncpg.Pool | asyncpg.Connection,
        browser: Any,
        *,
        max_active_groups: int = 3,
        lanes_per_group: int = 4,
        target_suppliers: int = 20,
        max_batches: int = _MAX_BATCHES_PER_QUERY,
        page_quantum: int = 1,
        profile_extractor: StructuredExtractor | None = None,
        card_candidate_limit: int | None = None,
    ) -> None:
        if (
            max_active_groups < 1
            or lanes_per_group != 4
            or target_suppliers < 1
            or not 1 <= max_batches <= 100
            or page_quantum < 1
            or (
                card_candidate_limit is not None
                and (
                    isinstance(card_candidate_limit, bool)
                    or not isinstance(card_candidate_limit, int)
                    or not 1 <= card_candidate_limit <= _BATCH_CANDIDATE_LIMIT
                )
            )
        ):
            raise ValueError("expected positive group/target limits and exactly four lanes")
        self.database = database
        self.browser = browser
        self.max_active_groups = max_active_groups
        self.lanes_per_group = lanes_per_group
        self.target_suppliers = target_suppliers
        self.max_batches = max_batches
        self.page_quantum = page_quantum
        self.profile_extractor = profile_extractor
        self.card_candidate_limit = card_candidate_limit
        self._profile_locks: dict[tuple[UUID, UUID], tuple[asyncio.Lock, int]] = {}
        self._profile_locks_guard = asyncio.Lock()
        self._profile_overflow_lock = asyncio.Lock()
        self._executor = ResearchRunExecutor(database, browser)

    @asynccontextmanager
    async def _profile_lock(self, key: tuple[UUID, UUID]):
        """Serialize profile creation per run/item without growing an unbounded map."""
        async with self._profile_locks_guard:
            entry = self._profile_locks.get(key)
            if entry is None and len(self._profile_locks) >= _PROFILE_LOCK_LIMIT:
                lock = None
            else:
                if entry is None:
                    entry = (asyncio.Lock(), 0)
                lock, users = entry
                self._profile_locks[key] = (lock, users + 1)
        if lock is None:
            async with self._profile_overflow_lock:
                yield
            return
        try:
            async with lock:
                yield
        finally:
            async with self._profile_locks_guard:
                current = self._profile_locks.get(key)
                if current is not None and current[0] is lock:
                    if current[1] <= 1:
                        self._profile_locks.pop(key, None)
                    else:
                        self._profile_locks[key] = (lock, current[1] - 1)

    async def execute(self, run_id: UUID) -> ResearchExecutionResult | None:
        """Validate and seed a run, then enqueue only groups admitted by the cap."""
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        await self.reconcile(run_id)
        async with _connection(self.database) as connection:
            run = await connection.fetchrow(
                "SELECT id,case_id,status,search_scope,error_code FROM research_runs WHERE id=$1",
                run_id,
            )
            if run is None:
                raise ResearchExecutionRejected("research run does not exist")
            if run["status"] in {"succeeded", "partial", "failed"}:
                return await self._existing_result(connection, run)
            if run["status"] != "running":
                raise ResearchExecutionRejected("research run is not running")
            try:
                snapshot = await self._executor._load_snapshot(connection, run)
            except PrivatePriceLeakError:
                return await self._executor._finish_rejected(
                    connection,
                    run_id=run_id,
                    case_id=run["case_id"],
                    error_code="private_price_detected",
                )
            except ResearchPlanDrift:
                return await self._executor._finish_rejected(
                    connection,
                    run_id=run_id,
                    case_id=run["case_id"],
                    error_code="research_plan_drift",
                )
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
                await self._seed_groups(connection, snapshot)
                await self._admit_groups(connection)
                return await self._terminal_result_if_ready(connection, run_id)

    async def reconcile(self, run_id: UUID | None = None) -> int:
        """Recover researching checkpoints whose queue ownership is no longer live.

        Paused/non-active runs and groups stay paused. Active stale checkpoints
        receive a new generation before they are requeued, fencing late results.
        """
        if run_id is not None and not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        recovered = 0
        async with _connection(self.database) as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
                stale = await connection.fetch(
                    """
                    SELECT cp.id,cp.group_id,cp.lane_id,cp.research_run_id,
                        cp.owner_generation,cp.batch_attempts,cp.recovery_base_attempts,
                        job.id AS job_id,job.attempts AS job_attempts,
                        job.max_attempts AS job_max_attempts,
                        run.status AS run_status,grp.status AS group_status,
                        procurement_case.status AS case_status
                    FROM research_branch_checkpoints cp
                    JOIN research_runs run ON run.id=cp.research_run_id
                    JOIN research_item_groups grp ON grp.id=cp.group_id
                    JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
                    LEFT JOIN jobs job ON job.id=cp.current_job_id
                    WHERE cp.status='researching'
                      AND ($1::uuid IS NULL OR cp.research_run_id=$1)
                      AND (cp.current_job_id IS NULL OR job.id IS NULL
                           OR job.status<>'running' OR job.lease_expires_at<=now()
                           OR cp.lease_until IS NULL OR cp.lease_until<=now())
                    ORDER BY cp.updated_at,cp.id
                    LIMIT 500
                    FOR UPDATE OF cp SKIP LOCKED
                    """,
                    run_id,
                )
                changed_groups: set[UUID] = set()
                changed_runs: set[UUID] = set()
                for row in stale:
                    paused = (
                        row["run_status"] != "running"
                        or row["group_status"] == "paused"
                        or row["case_status"] not in {"researching", "contacting"}
                    )
                    attempt_limit = min(
                        _MAX_BATCH_ATTEMPTS,
                        int(row["job_max_attempts"] or _MAX_BATCH_ATTEMPTS),
                    )
                    exhausted = int(row["batch_attempts"]) - int(
                        row.get("recovery_base_attempts", 0)
                    ) >= attempt_limit or (
                        row["job_attempts"] is not None
                        and int(row["job_attempts"]) >= int(row["job_max_attempts"])
                    )
                    if paused:
                        status = "paused"
                        reason = ""
                    elif exhausted:
                        status = "failed"
                        reason = "recovery_attempts_exhausted"
                    else:
                        status = "queued"
                        reason = ""
                    await connection.execute(
                        """
                        UPDATE research_branch_checkpoints
                        SET status=$2,owner_generation=owner_generation+1,
                            owner_worker=NULL,lease_until=NULL,current_job_id=NULL,
                            stop_reason=$3,last_error_code=CASE WHEN $2='failed'
                                THEN $3 ELSE 'recovered_stale_owner' END,
                            completed_at=CASE WHEN $2='failed' THEN COALESCE(completed_at,now())
                                ELSE NULL END,updated_at=now()
                        WHERE id=$1
                        """,
                        row["id"],
                        status,
                        reason,
                    )
                    lane_status = "failed" if status == "failed" else status
                    await connection.execute(
                        """UPDATE research_lanes SET status=$2,owner_worker=NULL,
                            lease_until=NULL,error_code=$3,updated_at=now() WHERE id=$1""",
                        row["lane_id"],
                        lane_status,
                        reason,
                    )
                    if not paused:
                        changed_groups.add(row["group_id"])
                        changed_runs.add(row["research_run_id"])
                    recovered += 1

                for group_id in changed_groups:
                    await self._reconcile_group(connection, group_id)
                await self._admit_groups(connection)
                for active_run_id in changed_runs:
                    await self._terminal_result_if_ready(connection, active_run_id)
        return recovered

    async def reconcile_runnable_checkpoints(self, run_id: UUID) -> int:
        """Repair queued checkpoints whose stored queue owner is no longer live."""
        if not isinstance(run_id, UUID):
            raise TypeError("run_id must be a UUID")
        async with _connection(self.database) as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
                active = await connection.fetchval(
                    """SELECT EXISTS(
                       SELECT 1 FROM research_runs run
                       JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
                       WHERE run.id=$1 AND run.status='running'
                         AND procurement_case.status IN ('researching','contacting')
                    )""",
                    run_id,
                )
                if not active:
                    return 0
                repaired = await self._reconcile_runnable_checkpoints_tx(connection, run_id)
                await self._admit_groups(connection)
                await self._terminal_result_if_ready(connection, run_id)
                return repaired

    async def _reconcile_runnable_checkpoints_tx(
        self, connection: asyncpg.Connection, run_id: UUID
    ) -> int:
        rows = await connection.fetch(
            """
            SELECT cp.*,grp.status AS group_status,grp.eligible_suppliers,
                grp.target_suppliers,run.status AS run_status,
                procurement_case.status AS case_status,job.status AS job_status
            FROM research_branch_checkpoints cp
            JOIN research_item_groups grp ON grp.id=cp.group_id
            JOIN research_runs run ON run.id=cp.research_run_id
            JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
            LEFT JOIN jobs job ON job.id=cp.current_job_id
            WHERE cp.research_run_id=$1 AND cp.status='queued'
              AND (cp.lease_until IS NULL OR cp.lease_until<=now())
              AND (job.id IS NULL OR job.status IN ('succeeded','dead'))
              AND grp.status IN ('queued','researching') AND run.status='running'
              AND procurement_case.status IN ('researching','contacting')
              AND (cp.current_job_id IS NOT NULL
                   OR grp.eligible_suppliers>=grp.target_suppliers)
              AND NOT EXISTS (
                  SELECT 1 FROM jobs live
                  WHERE live.kind=$2 AND live.payload->>'task_id'=cp.id::text
                    AND live.status IN ('pending','retry','running')
              )
            ORDER BY COALESCE(grp.scheduler_last_served_at,'-infinity'::timestamptz),
                grp.eligible_suppliers,grp.created_at,cp.lane_no,cp.id
            LIMIT 500 FOR UPDATE OF cp,grp SKIP LOCKED
            """,
            run_id,
            _JOB_KIND,
        )
        repaired = 0
        changed_groups: set[UUID] = set()
        for cp in rows:
            await connection.execute(
                """UPDATE research_branch_checkpoints
                SET current_job_id=NULL,owner_generation=owner_generation+1,
                    owner_worker=NULL,lease_until=NULL,updated_at=now()
                WHERE id=$1""",
                cp["id"],
            )
            if int(cp["eligible_suppliers"]) >= int(cp["target_suppliers"]):
                await self._finish_checkpoint(
                    connection,
                    cp,
                    status="skipped",
                    reason="target_reached",
                    eligible=int(cp["eligible_suppliers"]),
                )
                await self._reconcile_group(connection, cp["group_id"])
            else:
                changed_groups.add(cp["group_id"])
            repaired += 1
        for group_id in changed_groups:
            await self._reconcile_group(connection, group_id)
        return repaired

    async def renew_task_lease(
        self,
        task_id: UUID,
        job_id: UUID,
        worker_id: str,
    ) -> bool:
        """Keep checkpoint/lane leases aligned with a renewed durable job lease."""
        async with _connection(self.database) as connection:
            async with connection.transaction():
                renewed = await connection.fetchrow(
                    """
                    UPDATE research_branch_checkpoints cp
                    SET lease_until=job.lease_expires_at,updated_at=now()
                    FROM jobs job,research_item_groups grp,research_runs run,
                        procurement_cases procurement_case
                    WHERE cp.id=$1 AND cp.current_job_id=$2
                      AND cp.status='researching' AND cp.owner_worker=$3
                      AND grp.id=cp.group_id AND run.id=cp.research_run_id
                      AND procurement_case.id=run.case_id
                      AND grp.status='researching' AND run.status='running'
                      AND procurement_case.status IN ('researching','contacting')
                      AND job.id=$2 AND job.status='running' AND job.locked_by=$3
                      AND job.lease_expires_at>now()
                    RETURNING cp.lane_id,cp.lease_until
                    """,
                    task_id,
                    job_id,
                    worker_id,
                )
                if renewed is not None:
                    await connection.execute(
                        """UPDATE research_lanes SET lease_until=$2,updated_at=now()
                        WHERE id=$1 AND status='researching' AND owner_worker=$3""",
                        renewed["lane_id"],
                        renewed["lease_until"],
                        worker_id,
                    )
                    return True
                return bool(
                    await connection.fetchval(
                        """SELECT EXISTS(
                           SELECT 1 FROM research_branch_checkpoints cp
                           JOIN jobs job ON job.id=cp.current_job_id
                           JOIN research_runs run ON run.id=cp.research_run_id
                           JOIN research_item_groups grp ON grp.id=cp.group_id
                           JOIN procurement_cases AS procurement_case
                             ON procurement_case.id=run.case_id
                           WHERE cp.id=$1 AND cp.current_job_id=$2
                             AND job.status='running' AND job.locked_by=$3
                             AND job.lease_expires_at>now()
                             AND (cp.status IN ('succeeded','failed','blocked','skipped')
                                  OR (cp.status='queued' AND grp.status='researching'
                                      AND run.status='running'
                                      AND procurement_case.status IN ('researching','contacting')))
                        )""",
                        task_id,
                        job_id,
                        worker_id,
                    )
                )

    async def execute_task(
        self,
        payload: dict[str, Any],
        *,
        job_id: UUID,
        worker_id: str,
    ) -> ResearchExecutionResult | None:
        """Run one bounded page without holding a DB connection during browser I/O."""
        run_id = _uuid(payload.get("research_run_id"), "research_run_id")
        task_id = _uuid(payload.get("task_id"), "task_id")
        if not worker_id:
            raise ValueError("worker_id must be non-empty")

        async with _connection(self.database) as connection:
            run = await connection.fetchrow(
                "SELECT id,case_id,status,search_scope,error_code FROM research_runs WHERE id=$1",
                run_id,
            )
            if run is None:
                raise ResearchExecutionRejected("research run does not exist")
            if run["status"] in {"succeeded", "partial", "failed"}:
                return await self._existing_result(connection, run)
            snapshot = await self._executor._load_snapshot(connection, run)
            branches = {branch.id: branch for branch in snapshot.branches}
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
                row = await connection.fetchrow(
                    """
                    SELECT checkpoint.*,job.attempts AS job_attempts,
                        job.max_attempts AS job_max_attempts
                    FROM research_branch_checkpoints AS checkpoint
                    JOIN jobs AS job ON job.id=checkpoint.current_job_id
                    JOIN research_runs AS run ON run.id=checkpoint.research_run_id
                    JOIN research_item_groups AS grp ON grp.id=checkpoint.group_id
                    JOIN procurement_cases AS procurement_case ON procurement_case.id=run.case_id
                    WHERE checkpoint.id=$1 AND checkpoint.research_run_id=$2
                      AND checkpoint.current_job_id=$3
                      AND checkpoint.status IN ('queued','researching')
                      AND grp.status='researching'
                      AND run.status='running'
                      AND procurement_case.status IN ('researching','contacting')
                      AND job.status='running' AND job.locked_by=$4
                      AND job.lease_expires_at>now()
                      AND (NOT EXISTS (SELECT 1 FROM research_city_admissions a
                          WHERE a.research_run_id=run.id) OR EXISTS (
                          SELECT 1 FROM research_city_item_cursors ci
                          JOIN case_pipeline_runtime rt ON rt.case_id=ci.case_id
                          WHERE ci.request_item_id=grp.request_item_id
                            AND ci.current_run_id=run.id AND rt.research_run_id=run.id
                            AND rt.enabled AND NOT rt.paused))
                    FOR UPDATE OF checkpoint
                    """,
                    task_id,
                    run_id,
                    job_id,
                    worker_id,
                )
                if row is None:
                    raise ResearchExecutionRejected(
                        "research task no longer owns an active checkpoint"
                    )
                branch = branches.get(row["query_id"])
                if branch is None:
                    raise ResearchPlanDrift("checkpoint query is not present in the immutable plan")
                effective = await load_effective_scope(connection, run_id, branch.plan.locality)
                if effective.revision is not None:
                    branch = replace(
                        branch,
                        plan=branch.plan.model_copy(
                            update={
                                "locality": effective.locality,
                                "execution_scope_revision_sha256": (
                                    effective.revision.revision_sha256
                                ),
                                "preferred_area_text": effective.preferred_area_text,
                            }
                        ),
                    )
                    # Six semantic queries may expose 12-20 cards each. The
                    # one-card staged budget of 20 would stop before query two.
                    await connection.execute(
                        "UPDATE research_branch_checkpoints "
                        "SET max_batches=GREATEST(max_batches,80) "
                        "WHERE id=$1",
                        task_id,
                    )
                    row = dict(row)
                    row["max_batches"] = max(int(row["max_batches"]), 80)
                eligible = await self._eligible_supplier_count(connection, branch)
                if eligible >= self.target_suppliers:
                    await self._finish_checkpoint(
                        connection,
                        row,
                        status="skipped",
                        reason="target_reached",
                        eligible=eligible,
                    )
                    await self._reconcile_group(connection, row["group_id"])
                    await self._admit_groups(connection, completed_job_id=job_id)
                    return await self._after_checkpoint(connection, run_id)
                if branch.plan.source != ResearchSource.OWN_DATABASE:
                    await before_provider_call(connection, run_id)
                generation = int(row["owner_generation"]) + 1
                await connection.execute(
                    """
                    UPDATE research_branch_checkpoints
                    SET status='researching',owner_generation=$2,owner_worker=$3,
                        lease_until=(SELECT lease_expires_at FROM jobs WHERE id=$4),
                        batch_attempts=batch_attempts+1,updated_at=now()
                    WHERE id=$1
                    """,
                    task_id,
                    generation,
                    worker_id,
                    job_id,
                )
                await connection.execute(
                    """
                    UPDATE research_lanes SET status='researching',generation=generation+1,
                        owner_worker=$2,lease_until=(SELECT lease_expires_at FROM jobs WHERE id=$3),
                        updated_at=now() WHERE id=$1
                    """,
                    row["lane_id"],
                    worker_id,
                    job_id,
                )
                row = dict(row)
                row["owner_generation"] = generation
                row["batch_attempts"] = int(row["batch_attempts"]) + 1
                row["operator_retry"] = payload.get("operator_retry") is True

        # Provider calls are outside all SQL transactions and do not hold a pool connection.
        if branch.plan.source == ResearchSource.OWN_DATABASE:
            async with _connection(self.database) as connection:
                known = await self._executor._count_known_candidates(connection, branch)
            return await self._persist_batch_result(
                run_id,
                task_id,
                job_id,
                worker_id,
                generation,
                row,
                candidates_persisted=0,
                own_database_candidates=known,
                pages_examined=0,
                external_ids=[],
                exhausted=True,
                stop_reason="own_database_reused",
                locality=branch.plan.locality,
            )

        try:
            batch_candidate_limit = _effective_batch_candidate_limit(
                branch.plan.source, self.card_candidate_limit
            )
            plan = _limited_plan(
                branch,
                batch_candidate_limit=batch_candidate_limit,
                excluded_external_ids=_json_array(row["excluded_external_ids"]),
                search_cursor=_cursor_state(row["search_cursor_state"])["current"],
                execution_generation=generation,
            )
            if self.profile_extractor is not None:
                if not hasattr(self.database, "acquire"):
                    raise ResearchExecutionRejected(
                        "semantic search profiles require a database pool"
                    )
                async with self._profile_lock((run_id, branch.request_item_id)):
                    await ensure_search_profiles(
                        self.database,
                        self.profile_extractor,
                        run_id,
                        [branch.request_item_id],
                    )
                async with _connection(self.database) as connection:
                    intent_plan = await load_search_intent_plan(
                        connection,
                        plan,
                        branch.id,
                        _json_array(row["excluded_external_ids"]),
                        _cursor_state(row["search_cursor_state"])["current"],
                        intent_ordinal=int(row["semantic_intent_ordinal"]),
                    )
                if intent_plan is None:
                    return await self._persist_batch_result(
                        run_id,
                        task_id,
                        job_id,
                        worker_id,
                        generation,
                        row,
                        candidates_persisted=0,
                        own_database_candidates=0,
                        pages_examined=0,
                        external_ids=[],
                        exhausted=True,
                        stop_reason="semantic_intents_exhausted",
                        locality=branch.plan.locality,
                    )
                plan = intent_plan
            search_plan = _web_plan_with_catalog_links(plan, [])
            result = _validate_browser_result(
                search_plan, await self.browser.search_suppliers(search_plan)
            )
            if len(result.candidates) > batch_candidate_limit:
                raise ResearchExecutionRejected("browser exceeded the bounded candidate page")
        except (
            BrowserProviderError,
            PermanentProviderError,
            RetryableProviderError,
            ResearchExecutionRejected,
            ValidationError,
        ) as exc:
            if isinstance(exc, ProviderAuthenticationDeferred):
                async with (
                    _connection(self.database) as connection,
                    connection.transaction(),
                ):
                    await self._assert_fence(connection, task_id, job_id, worker_id, generation)
                    await connection.execute(
                        "UPDATE research_branch_checkpoints SET status='queued',owner_worker=NULL,"
                        "lease_until=NULL,batch_attempts=GREATEST(recovery_base_attempts,"
                        "batch_attempts-1),last_error_code=$2,"
                        "updated_at=now() "
                        "WHERE id=$1", task_id, exc.reason,
                    )
                raise
            if isinstance(exc, ProviderQuotaDeferred):
                raise
            retryable = isinstance(exc, (BrowserProviderError, RetryableProviderError))
            exhausted_attempts = (
                int(row["batch_attempts"]) - int(row.get("recovery_base_attempts", 0))
            ) >= min(_MAX_BATCH_ATTEMPTS, int(row["job_max_attempts"]))
            reason = _safe_error_code(exc)
            if is_quota_failure(reason):
                async with _connection(self.database) as connection, connection.transaction():
                    due = await sleep_for_quota(
                        connection,
                        run_id=run_id,
                        task_id=task_id,
                        job_id=job_id,
                        worker_id=worker_id,
                        generation=generation,
                    )
                if due is not None:
                    raise ProviderQuotaDeferred(due) from exc
            LOGGER.warning(
                "parallel research page failed run=%s task=%s batch=%s reason=%s",
                run_id,
                task_id,
                row["current_batch_no"],
                reason,
            )
            if retryable and not exhausted_attempts:
                await self._record_retry(task_id, job_id, worker_id, generation, reason)
                raise RetryableProviderError(reason) from exc
            terminal_status = (
                "failed"
                if isinstance(
                    exc, (BrowserProviderError, RetryableProviderError, PermanentProviderError)
                )
                else "blocked"
            )
            return await self._persist_batch_result(
                run_id,
                task_id,
                job_id,
                worker_id,
                generation,
                row,
                candidates_persisted=0,
                own_database_candidates=0,
                pages_examined=0,
                external_ids=[],
                exhausted=False,
                stop_reason=f"provider_failure:{reason}",
                terminal_status=terminal_status,
                execution_intent_ordinal=getattr(plan, "execution_intent_ordinal", None),
                locality=branch.plan.locality,
            )

        async with _connection(self.database) as connection:
            async with connection.transaction():
                await self._assert_fence(connection, task_id, job_id, worker_id, generation)
                saved = await self._executor._persist_branch(connection, branch, result)
        external_ids = [
            candidate.external_id for candidate in result.candidates if candidate.external_id
        ]
        stop_reason = f"access_issue:{result.access_issue}" if result.access_issue else ""
        if result.exhausted and not stop_reason:
            stop_reason = "source_exhausted"
        elif not external_ids and not stop_reason:
            stop_reason = "no_new_candidates"
        return await self._persist_batch_result(
            run_id,
            task_id,
            job_id,
            worker_id,
            generation,
            row,
            candidates_persisted=saved,
            own_database_candidates=0,
            pages_examined=result.pages_examined,
            external_ids=external_ids,
            next_cursor=result.next_cursor,
            exhausted=result.exhausted or bool(result.access_issue),
            stop_reason=stop_reason,
            candidate_receipts=_candidate_receipts(
                result,
                batch_no=int(row["current_batch_no"]),
                locality=branch.plan.locality,
            ),
            source_progress_token=getattr(result, "progress_token", None),
            execution_intent_ordinal=getattr(plan, "execution_intent_ordinal", None),
            locality=branch.plan.locality,
        )

    async def load_run_progress(self, run_id: UUID) -> dict[str, Any]:
        async with _connection(self.database) as connection:
            row = await connection.fetchrow(
                """
                SELECT count(*) AS groups_total,
                    count(*) FILTER (WHERE status='queued') AS groups_queued,
                    count(*) FILTER (WHERE status='researching') AS groups_researching,
                    count(*) FILTER (
                        WHERE status IN ('succeeded','partial','failed')
                    ) AS groups_terminal,
                    coalesce(sum(found_leads),0) AS found_leads,
                    coalesce(sum(eligible_suppliers),0) AS eligible_suppliers,
                    coalesce(sum(qualified_price_suppliers),0) AS qualified_price_suppliers,
                    coalesce(sum(target_suppliers),0) AS target_suppliers
                FROM research_item_groups WHERE research_run_id=$1
                """,
                run_id,
            )
            return dict(row) if row is not None else {}

    async def load_research_progress(
        self, connection: asyncpg.Connection, run_id: UUID
    ) -> dict[str, Any]:
        """Return a consistent scheduler/progress snapshot inside the caller transaction."""
        row = await connection.fetchrow(
            """
            SELECT
                count(DISTINCT grp.id) AS groups_total,
                count(DISTINCT grp.id) FILTER (WHERE grp.status='queued') AS groups_queued,
                count(DISTINCT grp.id) FILTER (
                    WHERE grp.status='researching'
                ) AS groups_researching,
                count(DISTINCT grp.id) FILTER (WHERE grp.status='paused') AS groups_paused,
                count(DISTINCT grp.id) FILTER (
                    WHERE grp.status IN ('succeeded','partial','failed')
                ) AS groups_terminal,
                count(DISTINCT cp.id) AS checkpoints_total,
                count(DISTINCT cp.id) FILTER (WHERE cp.status='queued') AS checkpoints_queued,
                count(DISTINCT cp.id) FILTER (
                    WHERE cp.status='researching'
                ) AS checkpoints_researching,
                count(DISTINCT cp.id) FILTER (
                    WHERE cp.status IN ('succeeded','failed','blocked','skipped')
                )
                    AS checkpoints_terminal,
                count(DISTINCT cp.id) FILTER (
                    WHERE cp.status='queued' AND cp.current_job_id IS NULL
                )
                    AS queued_without_job,
                count(DISTINCT cp.id) FILTER (
                    WHERE cp.status='queued' AND cp.current_job_id IS NOT NULL
                      AND (job.id IS NULL OR job.status IN ('succeeded','dead'))
                )
                    AS queued_with_terminal_job,
                max(cp.last_real_progress_at) AS last_real_progress_at,
                count(DISTINCT live.id) FILTER (WHERE live.status IN ('pending','retry','running'))
                    AS live_jobs,
                min(live.available_at) FILTER (WHERE live.status IN ('pending','retry'))
                    AS next_job_at,
                wait.next_probe_at AS provider_next_probe_at,
                wait.reason AS provider_wait_reason,
                run.status AS run_status
            FROM research_runs run
            LEFT JOIN research_item_groups grp ON grp.research_run_id=run.id
            LEFT JOIN research_branch_checkpoints cp ON cp.group_id=grp.id
            LEFT JOIN jobs job ON job.id=cp.current_job_id
            LEFT JOIN jobs live ON live.kind=$2
              AND live.payload->>'task_id'=cp.id::text
            LEFT JOIN research_provider_waits wait ON wait.research_run_id=run.id
            WHERE run.id=$1
            GROUP BY run.id,wait.next_probe_at,wait.reason
            """,
            run_id,
            _JOB_KIND,
        )
        if row is None:
            return {}
        snapshot = dict(row)
        next_times = [
            value for value in (row["provider_next_probe_at"], row["next_job_at"])
            if value is not None
        ]
        snapshot["next_wake_at"] = min(next_times) if next_times else None
        has_work = int(row["checkpoints_total"] or 0) > int(row["checkpoints_terminal"] or 0)
        stalled = bool(
            row["run_status"] == "running"
            and has_work
            and not row["live_jobs"]
            and not row["queued_without_job"]
            and not row["queued_with_terminal_job"]
            and not row["checkpoints_researching"]
            and not row["groups_paused"]
            and row["provider_next_probe_at"] is None
        )
        snapshot["stalled"] = stalled
        if row["run_status"] != "running":
            wait_reason = "run_not_running"
        elif row["groups_paused"]:
            wait_reason = "research_paused"
        elif row["provider_next_probe_at"] is not None:
            wait_reason = "provider_quota_wait"
        elif row["queued_with_terminal_job"]:
            wait_reason = "queued_checkpoint_orphaned_job"
        elif row["live_jobs"]:
            wait_reason = "queue_job_active_or_scheduled"
        elif row["queued_without_job"]:
            wait_reason = "scheduler_admission_pending"
        elif row["checkpoints_researching"]:
            wait_reason = "checkpoint_lease_or_reconciliation"
        elif stalled:
            wait_reason = "no_runnable_work"
        else:
            wait_reason = None
        snapshot["wait_reason"] = wait_reason
        return snapshot

    async def load_item_progress(self, run_id: UUID) -> list[dict[str, Any]]:
        async with _connection(self.database) as connection:
            rows = await connection.fetch(
                """
                SELECT grp.id,grp.request_item_id,grp.status,grp.reason,
                    grp.target_suppliers,grp.eligible_suppliers,
                    grp.found_leads,grp.qualified_price_suppliers,
                    grp.branches_total,grp.branches_succeeded,grp.branches_failed,
                    grp.branches_blocked,grp.candidates_persisted,
                    grp.own_database_candidates,
                    coalesce((SELECT jsonb_agg(
                        receipt.value ORDER BY receipt.value->>'batch_no'
                    )
                              FROM research_branch_checkpoints cp
                              CROSS JOIN LATERAL jsonb_array_elements(
                                  cp.candidate_receipts
                              ) AS receipt(value)
                              WHERE cp.group_id=grp.id),'[]'::jsonb) AS candidate_receipts,
                    coalesce(jsonb_agg(jsonb_build_object(
                        'lane_id',lane.id,'lane_no',lane.lane_no,'status',lane.status,
                        'generation',lane.generation
                    ) ORDER BY lane.lane_no) FILTER (WHERE lane.id IS NOT NULL),'[]'::jsonb)
                    AS lanes
                FROM research_item_groups grp
                LEFT JOIN research_lanes lane ON lane.group_id=grp.id
                WHERE grp.research_run_id=$1
                GROUP BY grp.id ORDER BY grp.created_at,grp.id
                """,
                run_id,
            )
            return [dict(row) for row in rows]

    async def pause_run(self, run_id: UUID) -> None:
        async with _connection(self.database) as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
            await connection.execute(
                "UPDATE case_pipeline_runtime SET paused=true WHERE research_run_id=$1",
                run_id,
            )
            await connection.execute(
                "UPDATE research_item_groups SET status='paused',updated_at=now() "
                "WHERE research_run_id=$1 AND status IN ('queued','researching')",
                run_id,
            )
            await connection.execute(
                "UPDATE research_lanes lane SET status='paused',updated_at=now() "
                "FROM research_item_groups grp WHERE lane.group_id=grp.id "
                "AND grp.research_run_id=$1 AND lane.status IN ('queued','researching')",
                run_id,
            )
            await connection.execute(
                "UPDATE research_branch_checkpoints SET status='paused',updated_at=now() "
                "WHERE research_run_id=$1 AND status IN ('queued','researching')",
                run_id,
            )

    async def resume_run(self, run_id: UUID) -> None:
        async with _connection(self.database) as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
                from procurement_bot.city_tier_runtime import prepare_city_resume

                await prepare_city_resume(connection, run_id)
                await connection.execute(
                    """UPDATE case_pipeline_runtime SET paused=false,next_tick_at=now(),
                    wake_reason='owner_resume' WHERE research_run_id=$1""",
                    run_id,
                )
                await connection.execute(
                    "UPDATE research_item_groups SET status='queued',updated_at=now() "
                    "WHERE research_run_id=$1 AND status='paused'",
                    run_id,
                )
                await connection.execute(
                    "UPDATE research_lanes lane SET status='queued',updated_at=now() "
                    "FROM research_item_groups grp WHERE lane.group_id=grp.id "
                    "AND grp.research_run_id=$1 AND lane.status='paused'",
                    run_id,
                )
                await connection.execute(
                    """UPDATE research_branch_checkpoints
                    SET status='queued',stop_reason='',completed_at=NULL,
                        owner_generation=owner_generation+1,
                        owner_worker=NULL,lease_until=NULL,current_job_id=NULL,
                        batch_attempts=0,
                        updated_at=now()
                    WHERE research_run_id=$1 AND status='paused'""",
                    run_id,
                )
                await self._admit_groups(connection)

    async def recover_due_branches(self, run_id: UUID) -> int:
        """Recover classified transient failures in separate, bounded cycles.

        Preserve historical attempts/operator markers, cursor and evidence. No
        automatic recovery of unsafe payloads, identity failures or denials.
        """
        from procurement_bot.pipeline_runtime import autonomy_enabled

        async with _connection(self.database) as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
            if not await autonomy_enabled(connection, run_id):
                return 0
            run = await connection.fetchrow(
                """SELECT r.status,r.case_id,c.status AS case_status FROM research_runs r
                JOIN procurement_cases c ON c.id=r.case_id WHERE r.id=$1 FOR UPDATE OF r,c""",
                run_id,
            )
            if run is None or run["status"] not in {"running", "partial"}:
                return 0
            if await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM research_item_groups WHERE research_run_id=$1 "
                "AND status='paused')",
                run_id,
            ):
                return 0
            tasks = await connection.fetch(
                """SELECT cp.* FROM research_branch_checkpoints cp
                JOIN research_item_groups g ON g.id=cp.group_id
                WHERE cp.research_run_id=$1 AND cp.status IN ('failed','succeeded')
                  AND (NOT EXISTS (SELECT 1 FROM research_city_admissions a
                      WHERE a.research_run_id=cp.research_run_id) OR EXISTS (
                      SELECT 1 FROM research_city_item_cursors ci
                      WHERE ci.request_item_id=g.request_item_id
                        AND ci.current_run_id=cp.research_run_id))
                  AND g.eligible_suppliers<g.target_suppliers
                  AND (cp.recovery_due_at IS NULL OR cp.recovery_due_at<=now())
                  AND ((cp.status='failed' AND (
                    cp.stop_reason LIKE 'provider_failure:%cli_usage_limit%'
                    OR cp.stop_reason LIKE 'provider_failure:%cli_rate_limit%'
                    OR (cp.recovery_cycles<3 AND (
                      cp.stop_reason LIKE 'provider_failure:RetryableProviderError:%'
                      OR cp.stop_reason LIKE 'provider_failure:BrowserProviderError:%'))
                    OR (cp.recovery_cycles<3 AND EXISTS(
                      SELECT 1 FROM workflow_events verified
                      WHERE verified.case_id=(SELECT r.case_id FROM research_runs r
                        WHERE r.id=cp.research_run_id)
                        AND verified.event_type='research_contact_fix_verified'
                        AND verified.data->>'checkpoint_id'=cp.id::text
                        AND verified.data->>'runtime_sha256'=$2))))
                    OR EXISTS(SELECT 1 FROM research_search_profiles profile
                      JOIN research_search_intents intent ON intent.profile_id=profile.id
                      JOIN research_queries q ON q.id=cp.query_id
                      WHERE profile.research_run_id=cp.research_run_id
                        AND profile.request_item_id=g.request_item_id AND intent.provider=q.provider
                        AND profile.profile_version=(SELECT max(p.profile_version)
                          FROM research_search_profiles p
                          WHERE p.research_run_id=profile.research_run_id
                            AND p.request_item_id=profile.request_item_id)
                        AND (intent.recovery_due_at IS NULL OR intent.recovery_due_at<=now())
                        AND (COALESCE(intent.recovery_stop_reason,intent.stop_reason)
                              LIKE '%cli_usage_limit%'
                          OR COALESCE(intent.recovery_stop_reason,intent.stop_reason)
                              LIKE '%cli_rate_limit%'
                          OR (intent.recovery_cycles<3 AND (
                            COALESCE(intent.recovery_stop_reason,intent.stop_reason)
                              LIKE 'provider_failure:RetryableProviderError:%'
                            OR COALESCE(intent.recovery_stop_reason,intent.stop_reason)
                              LIKE 'provider_failure:BrowserProviderError:%')))))
                ORDER BY g.created_at,cp.lane_no LIMIT 100 FOR UPDATE OF cp""",
                run_id,
                hashlib.sha256(
                    Path(__file__).with_name("research_execution.py").read_bytes()
                ).hexdigest(),
            )
            if not tasks:
                if run["status"] == "running":
                    await self._reconcile_runnable_checkpoints_tx(connection, run_id)
                    await self._admit_groups(connection)
                    await self._terminal_result_if_ready(connection, run_id)
                return 0
            if any(is_quota_failure(task["stop_reason"]) for task in tasks):
                await connection.execute(
                    """INSERT INTO research_provider_waits(research_run_id,next_probe_at,reason)
                    VALUES($1,now(),'historical_quota') ON CONFLICT(research_run_id) DO NOTHING""",
                    run_id,
                )
            await connection.execute(
                "UPDATE research_runs SET status='running',completed_at=NULL,error_code='' "
                "WHERE id=$1",
                run_id,
            )
            await connection.execute(
                "UPDATE procurement_cases SET status='researching',updated_at=now() WHERE id=$1",
                run["case_id"],
            )
            for task in tasks:
                recovered_intent = await recover_intent(connection, task)
                # Retry only cards carrying the same classified quota failure.
                # Original counters/error markers and successful card facts stay intact.
                await connection.execute(
                    """UPDATE research_discovery_cards card SET status='pending',
                    recovery_base_attempts=attempts,updated_at=now()
                    FROM research_discovery_pages page WHERE card.page_id=page.id
                    AND page.query_id=$1 AND card.status='failed'
                    AND (card.failure_code LIKE '%cli_usage_limit%'
                         OR card.failure_code LIKE '%cli_rate_limit%')""",
                    task["query_id"],
                )
                await connection.execute(
                    """UPDATE research_branch_checkpoints SET status='queued',
                    recovery_base_attempts=batch_attempts,recovery_cycles=recovery_cycles+1,
                    recovery_due_at=now()+interval '30 minutes',completed_at=NULL,
                    owner_generation=owner_generation+1,owner_worker=NULL,lease_until=NULL,
                    current_job_id=NULL,updated_at=now() WHERE id=$1""",
                    task["id"],
                )
                await connection.execute(
                    "UPDATE research_item_groups SET status='queued',completed_at=NULL,"
                    "reason='autonomous_technical_recovery',updated_at=now() WHERE id=$1",
                    task["group_id"],
                )
                await connection.execute(
                    "UPDATE research_lanes SET status='queued',owner_worker=NULL,lease_until=NULL,"
                    "updated_at=now() WHERE id=$1",
                    task["lane_id"],
                )
                await connection.execute(
                    """INSERT INTO workflow_events(case_id,event_type,actor_type,actor_id,
                    idempotency_key,data) VALUES($1,'research_branch_auto_recovery','worker',
                    'pipeline',$2,$3::jsonb) ON CONFLICT(idempotency_key) DO NOTHING""",
                    run["case_id"],
                    stable_key("auto-recovery", task["id"], int(task["recovery_cycles"]) + 1),
                    {
                        "checkpoint_id": str(task["id"]),
                        "batch": task["current_batch_no"],
                        "historical_attempts": task["batch_attempts"],
                        "cycle": int(task["recovery_cycles"]) + 1,
                        "historical_stop_reason": task["stop_reason"],
                        "previous_intent_ordinal": task["semantic_intent_ordinal"],
                        "previous_cursor": task["search_cursor_state"],
                        "previous_progress_token": task["source_progress_token"],
                        "recovered_intent": recovered_intent,
                    },
                )
            await self._admit_groups(connection)
            return len(tasks)

    async def retry_failed(self, run_id: UUID) -> list[UUID]:
        """Queue one bounded operator retry for technical provider failures.

        The original batch cursor, exclusions, receipts, and evidence stay intact.
        An explicit job-payload marker limits each checkpoint to one operator retry
        cycle, including across follow-up pages.
        """
        async with _connection(self.database) as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
                run = await connection.fetchrow(
                    """SELECT run.status,run.case_id,
                              procurement_case.status AS case_status
                    FROM research_runs run
                    JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
                    WHERE run.id=$1 FOR UPDATE OF run""",
                    run_id,
                )
                if run is None:
                    raise ResearchExecutionRejected("research run is not active")
                active_run = run["status"] == "running" and run["case_status"] in {
                    "researching",
                    "contacting",
                }
                recoverable_terminal_run = (
                    run["status"] == "partial" and run["case_status"] == "report_ready"
                )
                if not active_run and not recoverable_terminal_run:
                    raise ResearchExecutionRejected("research run is not active or recoverable")
                if await connection.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM research_item_groups
                    WHERE research_run_id=$1 AND status='paused')""",
                    run_id,
                ):
                    raise ResearchExecutionRejected(
                        "resume the research run before retrying failures"
                    )
                tasks = await connection.fetch(
                    """
                    SELECT checkpoint.id,checkpoint.group_id,checkpoint.lane_id,
                        checkpoint.current_batch_no,checkpoint.owner_generation
                    FROM research_branch_checkpoints checkpoint
                    JOIN research_item_groups grp ON grp.id=checkpoint.group_id
                    WHERE checkpoint.research_run_id=$1 AND checkpoint.status='failed'
                      AND grp.status IN ('partial','failed')
                      AND checkpoint.stop_reason LIKE ANY($2::text[])
                      AND NOT EXISTS (
                          SELECT 1 FROM jobs prior_job
                          WHERE prior_job.kind=$3
                            AND prior_job.payload->>'task_id'=checkpoint.id::text
                            AND prior_job.payload->>'operator_retry'='true'
                      )
                    ORDER BY grp.created_at,grp.id,checkpoint.lane_no,checkpoint.query_id
                    FOR UPDATE OF checkpoint,grp
                    """,
                    run_id,
                    [prefix + "%" for prefix in _RETRYABLE_TECHNICAL_FAILURE_PREFIXES],
                    _JOB_KIND,
                )
                if not tasks:
                    return []
                if recoverable_terminal_run:
                    # A partially completed run with a current report can be
                    # reopened for this same-run technical retry. The report and
                    # approved plan remain intact; normal checkpoint completion
                    # will reconcile the run and case again.
                    case_reopened = await connection.execute(
                        """UPDATE procurement_cases SET status='researching',updated_at=now()
                        WHERE id=$1 AND status='report_ready'""",
                        run["case_id"],
                    )
                    if case_reopened != "UPDATE 1":
                        raise ResearchExecutionRejected(
                            "procurement case changed before technical retry"
                        )
                    run_reopened = await connection.execute(
                        """UPDATE research_runs SET status='running',completed_at=NULL,
                            error_code=''
                        WHERE id=$1 AND status='partial'""",
                        run_id,
                    )
                    if run_reopened != "UPDATE 1":
                        raise ResearchExecutionRejected(
                            "research run changed before technical retry"
                        )
                task_ids: list[UUID] = []
                for task in tasks:
                    generation = int(task["owner_generation"]) + 1
                    await connection.execute(
                        """UPDATE research_branch_checkpoints
                        SET status='queued',stop_reason='',last_error_code=$2,
                            completed_at=NULL,owner_generation=$3,owner_worker=NULL,
                            lease_until=NULL,current_job_id=NULL,batch_attempts=0,
                            updated_at=now() WHERE id=$1""",
                        task["id"],
                        _OPERATOR_RETRY_MARKER,
                        generation,
                    )
                    await connection.execute(
                        """UPDATE research_lanes SET status='queued',error_code='',
                            owner_worker=NULL,lease_until=NULL,updated_at=now() WHERE id=$1""",
                        task["lane_id"],
                    )
                    await connection.execute(
                        """UPDATE research_item_groups
                        SET status='queued',reason='operator_retry_failed',completed_at=NULL,
                            updated_at=now() WHERE id=$1""",
                        task["group_id"],
                    )
                    task_ids.append(task["id"])
                if task_ids:
                    await self._admit_groups(connection)
                return task_ids

    async def _seed_groups(self, connection: asyncpg.Connection, snapshot: Any) -> None:
        items: dict[int, UUID] = {}
        for branch in snapshot.branches:
            items[branch.plan.item_index] = branch.request_item_id
        groups: dict[UUID, UUID] = {}
        for request_item_id in items.values():
            proposed_id = await connection.fetchval(
                """
                INSERT INTO research_item_groups(
                    research_run_id,request_item_id,target_suppliers
                ) VALUES ($1,$2,$3)
                ON CONFLICT (research_run_id,request_item_id) DO NOTHING
                RETURNING id
                """,
                snapshot.run_id,
                request_item_id,
                self.target_suppliers,
            )
            group_id = proposed_id or await connection.fetchval(
                """SELECT id FROM research_item_groups
                WHERE research_run_id=$1 AND request_item_id=$2""",
                snapshot.run_id,
                request_item_id,
            )
            groups[request_item_id] = group_id
            for lane_no in range(self.lanes_per_group):
                await connection.execute(
                    """
                    INSERT INTO research_lanes(group_id,lane_no)
                    VALUES ($1,$2) ON CONFLICT (group_id,lane_no) DO NOTHING
                    """,
                    group_id,
                    lane_no,
                )
        per_item: dict[UUID, list[_QueryBranch]] = {}
        for branch in snapshot.branches:
            per_item.setdefault(branch.request_item_id, []).append(branch)
        for request_item_id, branches in per_item.items():
            group_id = groups[request_item_id]
            for lane_no, branch in enumerate(branches):
                lane_id = await connection.fetchval(
                    "SELECT id FROM research_lanes WHERE group_id=$1 AND lane_no=$2",
                    group_id,
                    lane_no % self.lanes_per_group,
                )
                evidence_exists = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM source_evidence WHERE research_query_id=$1)",
                    branch.id,
                )
                checkpoint = await connection.fetchrow(
                    """
                    INSERT INTO research_branch_checkpoints(
                        research_run_id,group_id,lane_id,query_id,lane_no,
                        status,legacy_uncheckpointed,max_batches,last_error_code
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    ON CONFLICT (research_run_id,query_id) DO NOTHING
                    RETURNING id
                    """,
                    snapshot.run_id,
                    group_id,
                    lane_id,
                    branch.id,
                    lane_no % self.lanes_per_group,
                    "blocked" if branch.plan.blocked_reason else "queued",
                    bool(evidence_exists),
                    self.max_batches,
                    branch.plan.blocked_reason
                    or ("legacy_uncheckpointed" if evidence_exists else ""),
                )
                if checkpoint is not None and branch.plan.blocked_reason:
                    await connection.execute(
                        """UPDATE research_branch_checkpoints
                        SET stop_reason=$2,completed_at=now() WHERE id=$1""",
                        checkpoint["id"],
                        branch.plan.blocked_reason,
                    )
            await connection.execute(
                """
                UPDATE research_item_groups grp
                SET branches_total=(SELECT count(*)
                    FROM research_branch_checkpoints cp WHERE cp.group_id=grp.id),
                    updated_at=now()
                WHERE grp.id=$1
                """,
                group_id,
            )

    async def _admit_groups(
        self,
        connection: asyncpg.Connection,
        *,
        completed_job_id: UUID | None = None,
    ) -> None:
        await connection.execute(
            """
            UPDATE research_item_groups AS grp
            SET status='paused',reason='run_or_case_not_researching',
                completed_at=COALESCE(grp.completed_at,now()),updated_at=now()
            FROM research_runs run
            JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
            WHERE grp.research_run_id=run.id
              AND grp.status IN ('queued','researching')
              AND (run.status<>'running'
                   OR procurement_case.status NOT IN ('researching','contacting'))
            """
        )
        await connection.execute(
            """
            UPDATE research_lanes lane SET status='paused',owner_worker=NULL,
                lease_until=NULL,updated_at=now()
            FROM research_item_groups grp
            WHERE lane.group_id=grp.id AND grp.status='paused'
              AND grp.reason='run_or_case_not_researching'
              AND lane.status IN ('queued','researching')
            """
        )
        await connection.execute(
            """
            UPDATE research_branch_checkpoints checkpoint SET status='paused',
                owner_worker=NULL,lease_until=NULL,updated_at=now()
            FROM research_item_groups grp
            WHERE checkpoint.group_id=grp.id AND grp.status='paused'
              AND grp.reason='run_or_case_not_researching'
              AND checkpoint.status IN ('queued','researching')
            """
        )
        while True:
            active = int(
                await connection.fetchval(
                    "SELECT count(*) FROM research_item_groups WHERE status='researching'"
                )
                or 0
            )
            if active >= self.max_active_groups:
                break
            group = await connection.fetchrow(
                """
                SELECT grp.id,grp.research_run_id FROM research_item_groups grp
                JOIN research_runs run ON run.id=grp.research_run_id
                JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
                WHERE grp.status='queued' AND run.status='running'
                  AND procurement_case.status IN ('researching','contacting')
                ORDER BY COALESCE(grp.scheduler_last_served_at,'-infinity'::timestamptz),
                    grp.eligible_suppliers::numeric/GREATEST(1,grp.target_suppliers),
                    grp.created_at,grp.id LIMIT 1
                """
            )
            if group is None:
                break
            await connection.execute(
                "UPDATE research_item_groups SET status='researching',updated_at=now() WHERE id=$1",
                group["id"],
            )

        # Give each admitted item one queue slot before any item gets a second.
        # A group keeps at most one live child job; completing that checkpoint
        # calls this scheduler again and advances that group to its next lane/page.
        groups = await connection.fetch(
            """
            SELECT grp.id,grp.research_run_id
            FROM research_item_groups grp
            JOIN research_runs run ON run.id=grp.research_run_id
            JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
            WHERE grp.status='researching' AND run.status='running'
              AND procurement_case.status IN ('researching','contacting')
              AND NOT EXISTS (
                  SELECT 1
                  FROM research_branch_checkpoints cp
                  JOIN jobs job ON job.id=cp.current_job_id
                  WHERE cp.group_id=grp.id
                    AND job.status IN ('pending','retry','running')
                    AND ($1::uuid IS NULL OR job.id<>$1)
              )
            ORDER BY grp.created_at,grp.id
            """,
            completed_job_id,
        )
        for group in groups:
            task = await connection.fetchrow(
                """
                SELECT id,lane_id,lane_no,current_batch_no,owner_generation,last_error_code
                FROM research_branch_checkpoints
                WHERE group_id=$1 AND status='queued' AND current_job_id IS NULL
                ORDER BY lane_no,query_id LIMIT 1
                """,
                group["id"],
            )
            if task is not None:
                await self._enqueue_batch(connection, group["research_run_id"], task)

        active_group_ids = await connection.fetch(
            "SELECT id FROM research_item_groups WHERE status='researching'"
        )
        for active_group in active_group_ids:
            await connection.execute(
                """
                UPDATE research_lanes lane SET status=CASE
                    WHEN EXISTS(SELECT 1 FROM research_branch_checkpoints cp
                                WHERE cp.lane_id=lane.id
                                  AND cp.status IN ('queued','researching'))
                    THEN 'researching'
                    WHEN EXISTS(SELECT 1 FROM research_branch_checkpoints cp
                                WHERE cp.lane_id=lane.id AND cp.status='failed')
                    THEN 'failed'
                    WHEN EXISTS(SELECT 1 FROM research_branch_checkpoints cp
                                WHERE cp.lane_id=lane.id AND cp.status='blocked')
                    THEN 'blocked'
                    ELSE 'succeeded' END, updated_at=now()
                WHERE lane.group_id=$1
                """,
                active_group["id"],
            )
            await self._reconcile_group(connection, active_group["id"])

    async def _enqueue_batch(
        self,
        connection: asyncpg.Connection,
        run_id: UUID,
        task: Mapping[str, Any],
    ) -> UUID:
        key = stable_key(
            "parallel-research",
            run_id,
            task["id"],
            int(task["current_batch_no"]),
            int(task["owner_generation"]),
        )
        payload = {"research_run_id": str(run_id), "task_id": str(task["id"])}
        operator_retry = (
            task.get("operator_retry") is True
            or task.get("last_error_code") == _OPERATOR_RETRY_MARKER
        )
        if operator_retry:
            payload["operator_retry"] = True
        job_id = await enqueue_job(
            connection,
            kind=_JOB_KIND,
            payload=payload,
            idempotency_key=key,
            max_attempts=_MAX_BATCH_ATTEMPTS,
        )
        if job_id is None:
            prior = await connection.fetchrow(
                "SELECT id,status FROM jobs WHERE idempotency_key=$1 FOR UPDATE", key
            )
            if prior is None:
                raise RuntimeError("research queue idempotency key has no job row")
            if prior["status"] in {"succeeded", "dead"}:
                generation = int(task["owner_generation"]) + 1
                await connection.execute(
                    """UPDATE research_branch_checkpoints
                    SET owner_generation=$2,current_job_id=NULL,owner_worker=NULL,
                        lease_until=NULL,updated_at=now() WHERE id=$1""",
                    task["id"],
                    generation,
                )
                key = stable_key(
                    "parallel-research", run_id, task["id"],
                    int(task["current_batch_no"]), generation
                )
                task = dict(task)
                task["owner_generation"] = generation
                job_id = await enqueue_job(
                    connection,
                    kind=_JOB_KIND,
                    payload=payload,
                    idempotency_key=key,
                    max_attempts=_MAX_BATCH_ATTEMPTS,
                )
                if job_id is None:
                    prior = await connection.fetchrow(
                        "SELECT id,status FROM jobs WHERE idempotency_key=$1 FOR UPDATE", key
                    )
                    if prior is None or prior["status"] in {"succeeded", "dead"}:
                        raise RuntimeError("new research queue generation is already terminal")
                    job_id = prior["id"]
            else:
                job_id = prior["id"]
        if operator_retry:
            await connection.execute(
                """UPDATE jobs SET available_at=GREATEST(
                    available_at,now()+make_interval(secs => $2))
                WHERE id=$1 AND status IN ('pending','retry')""",
                job_id,
                _OPERATOR_RETRY_BACKOFF_SECONDS,
            )
        await connection.execute(
            "UPDATE research_branch_checkpoints SET current_job_id=$2,updated_at=now() WHERE id=$1",
            task["id"],
            job_id,
        )
        return job_id

    async def _persist_batch_result(
        self,
        run_id: UUID,
        task_id: UUID,
        job_id: UUID,
        worker_id: str,
        generation: int,
        row: Mapping[str, Any],
        *,
        candidates_persisted: int,
        own_database_candidates: int,
        pages_examined: int,
        external_ids: list[str],
        exhausted: bool,
        stop_reason: str,
        next_cursor: str | None = None,
        terminal_status: str = "succeeded",
        candidate_receipts: list[dict[str, Any]] | None = None,
        source_progress_token: str | None = None,
        execution_intent_ordinal: int | None = None,
        locality: LocalityConstraint | None = None,
    ) -> ResearchExecutionResult | None:
        result: ResearchExecutionResult | None = None
        if is_quota_failure(stop_reason):
            async with _connection(self.database) as connection, connection.transaction():
                from procurement_bot.pipeline_runtime import autonomy_enabled

                if await autonomy_enabled(connection, run_id):
                    await self._assert_fence(connection, task_id, job_id, worker_id, generation)
                    cp = await connection.fetchrow(
                        "SELECT * FROM research_branch_checkpoints WHERE id=$1 FOR UPDATE",
                        task_id,
                    )
                    excluded = list(
                        dict.fromkeys(
                            [
                                *_json_array(cp["excluded_external_ids"]),
                                *external_ids,
                            ]
                        )
                    )
                    cursor, _, _ = _cursor_state_after_result(
                        cp["search_cursor_state"],
                        next_cursor,
                        max_seen=int(cp["max_batches"]),
                    )
                    await connection.execute(
                        """UPDATE research_branch_checkpoints SET
                        candidates_persisted=candidates_persisted+$2,
                        own_database_candidates=GREATEST(own_database_candidates,$3),
                        pages_examined=pages_examined+$4,excluded_external_ids=$5::jsonb,
                        candidate_receipts=candidate_receipts || $6::jsonb,
                        search_cursor_state=$7::jsonb,
                        source_progress_token=COALESCE($8,source_progress_token),
                        last_real_progress_at=CASE WHEN $2>0 OR $4>0 THEN now()
                            ELSE last_real_progress_at END,updated_at=now()
                        WHERE id=$1""",
                        task_id,
                        candidates_persisted,
                        own_database_candidates,
                        pages_examined,
                        excluded,
                        candidate_receipts or [],
                        cursor,
                        source_progress_token,
                    )
                due = await sleep_for_quota(
                    connection,
                    run_id=run_id,
                    task_id=task_id,
                    job_id=job_id,
                    worker_id=worker_id,
                    generation=generation,
                )
            if due is not None:
                raise ProviderQuotaDeferred(due)
        async with _connection(self.database) as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
                await self._assert_fence(connection, task_id, job_id, worker_id, generation)
                if pages_examined > 0 and not is_quota_failure(stop_reason):
                    await provider_succeeded(connection, run_id)
                cp = await connection.fetchrow(
                    "SELECT * FROM research_branch_checkpoints WHERE id=$1 FOR UPDATE", task_id
                )
                if cp is None:
                    return None
                excluded = _json_array(cp["excluded_external_ids"])
                new_ids = [value for value in external_ids if value not in excluded]
                excluded = list(dict.fromkeys([*excluded, *new_ids]))
                cursor_state, cursor_advanced, repeated_cursor = _cursor_state_after_result(
                    cp["search_cursor_state"],
                    next_cursor,
                    max_seen=int(cp["max_batches"]),
                )
                stage_progressed = (
                    source_progress_token is not None
                    and source_progress_token != cp["source_progress_token"]
                )
                has_real_progress = bool(
                    pages_examined > 0
                    or candidates_persisted > 0
                    or own_database_candidates > int(cp["own_database_candidates"])
                    or new_ids
                    or cursor_advanced
                    or stage_progressed
                )
                await connection.execute(
                    """
                    UPDATE research_branch_checkpoints
                    SET candidates_persisted=candidates_persisted+$2,
                        own_database_candidates=GREATEST(own_database_candidates,$3),
                        pages_examined=pages_examined+$4,excluded_external_ids=$5::jsonb,
                        candidate_receipts=candidate_receipts || $6::jsonb,
                        search_cursor_state=$7::jsonb,
                        source_progress_token=COALESCE($8,source_progress_token),
                        last_real_progress_at=CASE WHEN $9 THEN now()
                            ELSE last_real_progress_at END,
                        last_error_code='',owner_worker=NULL,lease_until=NULL,updated_at=now()
                    WHERE id=$1
                    """,
                    task_id,
                    candidates_persisted,
                    own_database_candidates,
                    pages_examined,
                    excluded,
                    candidate_receipts or [],
                    cursor_state,
                    source_progress_token,
                    has_real_progress,
                )
                await persist_intent_progress(
                    connection,
                    task=cp,
                    ordinal=execution_intent_ordinal,
                    cursor=cursor_state,
                    token=source_progress_token,
                )
                branch = await connection.fetchrow(
                    """
                    SELECT grp.request_item_id,q.city
                    FROM research_item_groups grp
                    JOIN research_queries q ON q.id=$2
                    WHERE grp.id=$1
                    """,
                    cp["group_id"],
                    cp["query_id"],
                )
                eligible = await self._qualified_supplier_count_for_item(
                    connection,
                    branch["request_item_id"],
                    branch["city"],
                    locality=locality,
                )
                continue_batch = (
                    terminal_status == "succeeded"
                    and not exhausted
                    and eligible < self.target_suppliers
                    and int(cp["current_batch_no"]) + 1 < int(cp["max_batches"])
                    and (not repeated_cursor or stage_progressed)
                    and (bool(new_ids) or cursor_advanced or stage_progressed)
                )
                if continue_batch:
                    next_batch = int(cp["current_batch_no"]) + 1
                    next_task = dict(cp)
                    next_task["current_batch_no"] = next_batch
                    next_task["operator_retry"] = row.get("operator_retry") is True
                    waiting_group = (
                        next_batch % self.page_quantum == 0
                        and await connection.fetchval(
                            """SELECT EXISTS(
                               SELECT 1 FROM research_item_groups waiting
                               WHERE waiting.research_run_id=$1 AND waiting.id<>$2
                                 AND waiting.status='queued'
                                 AND EXISTS(SELECT 1 FROM research_branch_checkpoints queued
                                   WHERE queued.group_id=waiting.id AND queued.status='queued')
                            )""",
                            run_id,
                            cp["group_id"],
                        )
                    )
                    if waiting_group:
                        await connection.execute(
                            """UPDATE research_branch_checkpoints
                            SET status='queued',current_batch_no=$2,batch_attempts=0,
                                recovery_base_attempts=0,owner_generation=owner_generation+1,
                                current_job_id=NULL,owner_worker=NULL,lease_until=NULL,
                                stop_reason='',updated_at=now() WHERE id=$1""",
                            task_id,
                            next_batch,
                        )
                        await connection.execute(
                            """UPDATE research_lanes SET status='queued',owner_worker=NULL,
                            lease_until=NULL,updated_at=now() WHERE id=$1""",
                            cp["lane_id"],
                        )
                        await connection.execute(
                            """UPDATE research_item_groups SET status='queued',
                            scheduler_last_served_at=now(),updated_at=now() WHERE id=$1""",
                            cp["group_id"],
                        )
                        await self._admit_groups(connection, completed_job_id=job_id)
                    else:
                        await connection.execute(
                            """UPDATE research_branch_checkpoints
                            SET status='queued',current_batch_no=$2,batch_attempts=0,
                                recovery_base_attempts=0,stop_reason='',updated_at=now()
                            WHERE id=$1""",
                            task_id,
                            next_batch,
                        )
                        await self._enqueue_batch(connection, run_id, next_task)
                else:
                    reason = stop_reason
                    if repeated_cursor and not stage_progressed and terminal_status == "succeeded":
                        reason = "repeated_cursor"
                    elif (
                        terminal_status == "succeeded"
                        and not exhausted
                        and not new_ids
                        and not cursor_advanced
                        and not stage_progressed
                    ):
                        reason = "no_new_candidates"
                    if (
                        terminal_status == "succeeded"
                        and not exhausted
                        and int(cp["current_batch_no"]) + 1 >= int(cp["max_batches"])
                    ):
                        reason = "batch_limit_reached"
                    if eligible >= self.target_suppliers:
                        reason = "target_reached"
                    advance_intent = False
                    next_intent_ordinal: int | None = None
                    intent_stopped = execution_intent_ordinal is not None and (
                        terminal_status == "failed"
                        or (
                            terminal_status == "succeeded"
                            and eligible < self.target_suppliers
                            and reason != "batch_limit_reached"
                        )
                    )
                    intent_profile = None
                    if intent_stopped:
                        provider = await connection.fetchval(
                            "SELECT provider FROM research_queries WHERE id=$1", cp["query_id"]
                        )
                        intent_profile = await connection.fetchrow(
                            """
                            SELECT profile.id AS profile_id,intent.id AS intent_id
                            FROM research_item_groups grp
                            JOIN research_search_profiles profile
                              ON profile.research_run_id=$1
                             AND profile.request_item_id=grp.request_item_id
                            JOIN LATERAL (
                                SELECT newest.id
                                FROM research_search_profiles newest
                                WHERE newest.research_run_id=profile.research_run_id
                                  AND newest.request_item_id=profile.request_item_id
                                ORDER BY newest.profile_version DESC LIMIT 1
                            ) active ON active.id=profile.id
                            LEFT JOIN research_search_intents intent
                              ON intent.profile_id=profile.id
                             AND intent.ordinal=$3 AND intent.provider=$4
                            WHERE grp.id=$2
                            """,
                            run_id,
                            cp["group_id"],
                            execution_intent_ordinal,
                            provider,
                        )
                        if intent_profile is not None and intent_profile["intent_id"] is not None:
                            await connection.execute(
                                """UPDATE research_search_intents
                                SET stop_reason=CASE WHEN recovery_stop_reason IS NULL
                                    THEN $2 ELSE stop_reason END,
                                    recovery_stop_reason=CASE WHEN recovery_stop_reason IS NOT NULL
                                    THEN $2 ELSE NULL END,updated_at=now()
                                WHERE id=$1 AND COALESCE(recovery_stop_reason,stop_reason)=''""",
                                intent_profile["intent_id"],
                                reason[:500],
                            )
                            next_intent_ordinal = await connection.fetchval(
                                """SELECT min(ordinal) FROM research_search_intents
                                WHERE profile_id=$1 AND provider=$2 AND ordinal>$3
                                  AND COALESCE(recovery_stop_reason,stop_reason)=''""",
                                intent_profile["profile_id"],
                                provider,
                                execution_intent_ordinal,
                            )
                            advance_intent = (
                                next_intent_ordinal is not None
                                and eligible < self.target_suppliers
                                and int(cp["current_batch_no"]) + 1 < int(cp["max_batches"])
                            )
                    if advance_intent:
                        next_batch = int(cp["current_batch_no"]) + 1
                        await connection.execute(
                            """UPDATE research_branch_checkpoints
                            SET status='queued',current_batch_no=$2,batch_attempts=0,
                            recovery_base_attempts=0,
                                semantic_intent_ordinal=$3,
                                search_cursor_state='{"current":null,"seen":[]}'::jsonb,
                                source_progress_token=NULL,stop_reason='',
                                last_error_code=CASE WHEN $4 LIKE 'provider_failure:%'
                                    THEN $4 ELSE last_error_code END,
                                completed_at=NULL,updated_at=now()
                            WHERE id=$1""",
                            task_id,
                            next_batch,
                            next_intent_ordinal,
                            reason[:240],
                        )
                        next_task = dict(cp)
                        next_task["current_batch_no"] = next_batch
                        next_task["semantic_intent_ordinal"] = next_intent_ordinal
                        next_task["operator_retry"] = row.get("operator_retry") is True
                        await self._enqueue_batch(connection, run_id, next_task)
                    else:
                        if terminal_status == "succeeded" and intent_profile is not None:
                            prior_failures = await connection.fetchval(
                                """SELECT string_agg(
                                    ordinal::text || '=' || left(
                                        COALESCE(recovery_stop_reason,stop_reason),80
                                    ),
                                    ',' ORDER BY ordinal
                                ) FROM research_search_intents
                                WHERE profile_id=$1
                                  AND COALESCE(recovery_stop_reason,stop_reason)
                                      LIKE 'provider_failure:%'""",
                                intent_profile["profile_id"],
                            )
                            if prior_failures:
                                reason = f"{reason};semantic_intent_failures:{prior_failures}"
                        status = "succeeded" if terminal_status == "succeeded" else terminal_status
                        await self._finish_checkpoint(
                            connection, cp, status=status, reason=reason, eligible=eligible
                        )
                await self._reconcile_group(connection, cp["group_id"])
                await self._admit_groups(connection, completed_job_id=job_id)
                result = await self._terminal_result_if_ready(connection, run_id)
        return result

    async def _finish_checkpoint(
        self,
        connection: asyncpg.Connection,
        cp: Mapping[str, Any],
        *,
        status: str,
        reason: str,
        eligible: int,
    ) -> None:
        await connection.execute(
            """
            UPDATE research_branch_checkpoints SET status=$2,stop_reason=$3,
                completed_at=COALESCE(completed_at,now()),owner_worker=NULL,lease_until=NULL,
                updated_at=now() WHERE id=$1
            """,
            cp["id"],
            status,
            reason[:240],
        )
        await connection.execute(
            """
            UPDATE research_lanes SET status=$2,error_code=$3,owner_worker=NULL,
                lease_until=NULL,updated_at=now() WHERE id=$1
            """,
            cp["lane_id"],
            "succeeded" if status == "skipped" else status,
            reason[:240],
        )

    async def _reconcile_group(self, connection: asyncpg.Connection, group_id: UUID) -> None:
        branch_stats = await connection.fetchrow(
            """
            SELECT count(*) AS total,
                count(*) FILTER (
                    WHERE status IN ('succeeded','failed','blocked','skipped')
                ) AS terminal,
                count(*) FILTER (WHERE status='succeeded') AS succeeded,
                count(*) FILTER (WHERE status='failed') AS failed,
                count(*) FILTER (WHERE status='blocked') AS blocked,
                coalesce(sum(candidates_persisted),0) AS candidates,
                coalesce(sum(own_database_candidates),0) AS known
            FROM research_branch_checkpoints WHERE group_id=$1
            """,
            group_id,
        )
        group = await connection.fetchrow(
            """SELECT target_suppliers,request_item_id,status,research_run_id
            FROM research_item_groups WHERE id=$1 FOR UPDATE""",
            group_id,
        )
        if group is None:
            return
        locality_payload = await connection.fetchval(
            """SELECT query.parameters->'locality'
            FROM research_branch_checkpoints checkpoint
            JOIN research_queries query ON query.id=checkpoint.query_id
            WHERE checkpoint.group_id=$1
            ORDER BY checkpoint.lane_no,checkpoint.query_id LIMIT 1""",
            group_id,
        )
        locality = (
            LocalityConstraint.model_validate(_json_object(locality_payload))
            if locality_payload is not None
            else None
        )
        if locality is not None:
            locality = (
                await load_effective_scope(connection, group["research_run_id"], locality)
            ).locality
        supplier_stats = await connection.fetchrow(
            """
            SELECT count(DISTINCT offer.supplier_id) FILTER (
                       WHERE offer.status IN ('lead','confirm','exact')) AS found_leads,
                   count(DISTINCT offer.supplier_id) FILTER (
                       WHERE offer.status IN ('lead','confirm','exact')
                         AND supplier.do_not_contact_reason=''
                         AND EXISTS (
                             SELECT 1 FROM supplier_aliases alias
                             WHERE alias.supplier_id=supplier.id AND alias.active
                               AND alias.alias_type IN (
                                   '2gis_firm_id','domain','bin_iin','instagram','source_record'
                               )
                         )
                         AND EXISTS (
                             SELECT 1 FROM supplier_contacts contact
                             WHERE contact.supplier_id=supplier.id AND contact.active
                               AND contact.contact_type IN (
                                   'phone','whatsapp'
                               )
                         )
                   ) AS eligible,
                   count(DISTINCT offer.supplier_id) FILTER (
                       WHERE offer.status IN ('confirm','exact') AND EXISTS (
                           SELECT 1 FROM offer_observations observation
                           WHERE observation.offer_id=offer.id
                             AND observation.source_type IN ('supplier_message','manual')
                             AND observation.price_amount IS NOT NULL
                       )
                   ) AS qualified_price_suppliers
            FROM offers offer JOIN suppliers supplier ON supplier.id=offer.supplier_id
            LEFT JOIN supplier_locations location ON location.id=offer.supplier_location_id
            WHERE offer.request_item_id=$1 AND supplier.status IN ('candidate','active')
              AND (location.id IS NULL OR location.city='' OR EXISTS (
                  SELECT 1 FROM research_branch_checkpoints cp
                  JOIN research_queries query ON query.id=cp.query_id
                  WHERE cp.group_id=$2 AND lower(query.city)=lower(location.city)
              ))
            """,
            group["request_item_id"],
            group_id,
        )
        eligible = await self._qualified_supplier_count_for_item(
            connection,
            group["request_item_id"],
            locality.city if locality is not None else "",
            locality=locality,
        )
        total = int(branch_stats["total"])
        terminal = int(branch_stats["terminal"])
        if terminal < total:
            await connection.execute(
                """
                UPDATE research_item_groups SET eligible_suppliers=$2,
                    found_leads=$9,qualified_price_suppliers=$10,
                    branches_total=$3,branches_succeeded=$4,branches_failed=$5,
                    branches_blocked=$6,candidates_persisted=$7,
                    own_database_candidates=$8,updated_at=now() WHERE id=$1
                """,
                group_id,
                eligible,
                total,
                branch_stats["succeeded"],
                branch_stats["failed"],
                branch_stats["blocked"],
                branch_stats["candidates"],
                branch_stats["known"],
                supplier_stats["found_leads"],
                supplier_stats["qualified_price_suppliers"],
            )
            return
        if int(branch_stats["failed"]) and not int(branch_stats["succeeded"]):
            status = "failed"
        elif (
            int(branch_stats["failed"])
            or int(branch_stats["blocked"])
            or eligible < int(group["target_suppliers"])
        ):
            status = "partial"
        else:
            status = "succeeded"
        reasons = await connection.fetch(
            "SELECT DISTINCT stop_reason FROM research_branch_checkpoints "
            "WHERE group_id=$1 AND stop_reason<>'' ORDER BY stop_reason",
            group_id,
        )
        reason_set = [entry["stop_reason"] for entry in reasons]
        if eligible < int(group["target_suppliers"]):
            reason_set.append(f"target_not_met:{eligible}/{group['target_suppliers']}")
        if int(branch_stats["failed"]):
            reason_set.append("source_failed")
        if int(branch_stats["blocked"]):
            reason_set.append("source_blocked")
        await connection.execute(
            """
            UPDATE research_item_groups SET status=$2,reason=$3,
                eligible_suppliers=$4,found_leads=$11,qualified_price_suppliers=$12,
                branches_total=$5,branches_succeeded=$6,
                branches_failed=$7,branches_blocked=$8,candidates_persisted=$9,
                own_database_candidates=$10,completed_at=COALESCE(completed_at,now()),
                updated_at=now() WHERE id=$1
            """,
            group_id,
            status,
            ";".join(dict.fromkeys(reason_set))[:500],
            eligible,
            total,
            branch_stats["succeeded"],
            branch_stats["failed"],
            branch_stats["blocked"],
            branch_stats["candidates"],
            branch_stats["known"],
            supplier_stats["found_leads"],
            supplier_stats["qualified_price_suppliers"],
        )
        await connection.execute(
            "UPDATE research_lanes SET status=CASE WHEN status='failed' THEN 'failed' "
            "WHEN status='blocked' THEN 'blocked' ELSE 'succeeded' END,updated_at=now() "
            "WHERE group_id=$1 AND status NOT IN ('failed','blocked')",
            group_id,
        )

    async def _eligible_supplier_count(
        self,
        connection: asyncpg.Connection,
        branch: _QueryBranch,
    ) -> int:
        return await self._qualified_supplier_count_for_item(
            connection,
            branch.request_item_id,
            branch.plan.locality.city,
            locality=branch.plan.locality,
        )

    async def _qualified_supplier_count_for_item(
        self,
        connection: asyncpg.Connection,
        request_item_id: UUID,
        city: str,
        *,
        locality: LocalityConstraint | None = None,
    ) -> int:
        if locality is not None and locality.scope.value == "specific_area":
            rows = await connection.fetch(
                """
                SELECT DISTINCT offer.supplier_id,location.city,location.address_text
                FROM offers offer
                JOIN suppliers supplier ON supplier.id=offer.supplier_id
                JOIN supplier_locations location
                  ON location.id=offer.supplier_location_id AND location.active
                WHERE offer.request_item_id=$1 AND offer.status IN ('lead','confirm','exact')
                  AND supplier.status IN ('candidate','active')
                  AND supplier.do_not_contact_reason=''
                  AND EXISTS (
                      SELECT 1 FROM supplier_aliases alias
                      WHERE alias.supplier_id=supplier.id AND alias.active
                        AND alias.alias_type IN (
                            '2gis_firm_id','domain','bin_iin','instagram','source_record'
                        )
                  )
                  AND EXISTS (
                      SELECT 1 FROM supplier_contacts contact
                      WHERE contact.supplier_id=supplier.id AND contact.active
                        AND contact.contact_type IN ('phone','whatsapp')
                  )
                """,
                request_item_id,
            )
            eligible_supplier_ids = {
                row["supplier_id"]
                for row in rows
                if decide_geographic_scope(
                    locality,
                    address_city=row["city"],
                    address_text=row["address_text"],
                )
                == GeographicDecision.ACCEPTED_BY_ADDRESS
            }
            return len(eligible_supplier_ids)

        value = await connection.fetchval(
            """
            SELECT count(DISTINCT offer.supplier_id)
            FROM offers offer JOIN suppliers supplier ON supplier.id=offer.supplier_id
            LEFT JOIN supplier_locations location ON location.id=offer.supplier_location_id
            WHERE offer.request_item_id=$1 AND offer.status IN ('lead','confirm','exact')
              AND supplier.status IN ('candidate','active') AND supplier.do_not_contact_reason=''
              AND (location.id IS NULL OR location.city='' OR lower(location.city)=lower($2))
              AND EXISTS (
                  SELECT 1 FROM supplier_aliases alias
                  WHERE alias.supplier_id=supplier.id AND alias.active
                    AND alias.alias_type IN (
                        '2gis_firm_id','domain','bin_iin','instagram','source_record'
                    )
              )
              AND EXISTS (
                  SELECT 1 FROM supplier_contacts contact
                  WHERE contact.supplier_id=supplier.id AND contact.active
                    AND contact.contact_type IN (
                        'phone','whatsapp'
                    )
              )
            """,
            request_item_id,
            city,
        )
        return int(value or 0)

    async def _assert_fence(
        self,
        connection: asyncpg.Connection,
        task_id: UUID,
        job_id: UUID,
        worker_id: str,
        generation: int,
    ) -> None:
        valid = await connection.fetchrow(
            """
            SELECT cp.id FROM research_branch_checkpoints cp
            JOIN jobs job ON job.id=cp.current_job_id
            JOIN research_runs run ON run.id=cp.research_run_id
            JOIN research_item_groups grp ON grp.id=cp.group_id
            JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
            WHERE cp.id=$1 AND cp.current_job_id=$2
              AND cp.owner_generation=$3 AND cp.owner_worker=$4
              AND cp.status='researching'
              AND grp.status='researching'
              AND run.status='running'
              AND procurement_case.status IN ('researching','contacting')
              AND job.status='running' AND job.locked_by=$4
              AND job.lease_expires_at>now()
              AND (NOT EXISTS (SELECT 1 FROM research_city_admissions a
                  WHERE a.research_run_id=run.id) OR EXISTS (
                  SELECT 1 FROM research_city_item_cursors ci
                  JOIN case_pipeline_runtime rt ON rt.case_id=ci.case_id
                  WHERE ci.request_item_id=grp.request_item_id
                    AND ci.current_run_id=run.id AND rt.research_run_id=run.id
                    AND rt.enabled AND NOT rt.paused))
            FOR UPDATE OF cp,job
            """,
            task_id,
            job_id,
            generation,
            worker_id,
        )
        if valid is None:
            raise ResearchExecutionRejected("research task lease or generation was lost")

    async def _record_retry(
        self,
        task_id: UUID,
        job_id: UUID,
        worker_id: str,
        generation: int,
        error_code: str,
    ) -> None:
        async with _connection(self.database) as connection:
            async with connection.transaction():
                await self._assert_fence(connection, task_id, job_id, worker_id, generation)
                await connection.execute(
                    "UPDATE research_branch_checkpoints SET status='queued',owner_worker=NULL,"
                    "lease_until=NULL,last_error_code=$2,updated_at=now() WHERE id=$1",
                    task_id,
                    error_code[:240],
                )

    async def _terminal_result_if_ready(
        self,
        connection: asyncpg.Connection,
        run_id: UUID,
    ) -> ResearchExecutionResult | None:
        await connection.execute("SELECT pg_advisory_xact_lock($1)", _SCHEDULER_LOCK_KEY)
        run = await connection.fetchrow(
            """SELECT run.id,run.case_id,run.status,run.error_code,
                procurement_case.status AS case_status
            FROM research_runs run
            JOIN procurement_cases procurement_case ON procurement_case.id=run.case_id
            WHERE run.id=$1""",
            run_id,
        )
        if run is None:
            raise ResearchExecutionRejected("research run does not exist")
        groups = await connection.fetchrow(
            """
            SELECT count(*) AS total,
                count(*) FILTER (WHERE status IN ('succeeded','partial','failed')) AS terminal,
                count(*) FILTER (WHERE status='succeeded') AS succeeded,
                count(*) FILTER (WHERE status='failed') AS failed,
                count(*) FILTER (WHERE status='partial') AS partial,
                count(*) FILTER (WHERE status='paused') AS paused,
                coalesce(sum(branches_total),0) AS branches_total,
                coalesce(sum(branches_succeeded),0) AS branches_succeeded,
                coalesce(sum(branches_failed),0) AS branches_failed,
                coalesce(sum(branches_blocked),0) AS branches_blocked,
                coalesce(sum(candidates_persisted),0) AS candidates_persisted,
                coalesce(sum(own_database_candidates),0) AS own_database_candidates
            FROM research_item_groups WHERE research_run_id=$1
            """,
            run_id,
        )
        if not groups["total"] or groups["terminal"] != groups["total"]:
            return None
        if run["case_status"] not in {
            "researching",
            "contacting",
            "evaluating",
            "report_ready",
            "selected",
            "closed",
            "ready",
        }:
            return None
        if run["status"] in {"succeeded", "partial", "failed"}:
            return await self._existing_result(connection, run)
        if groups["failed"] and not groups["succeeded"] and not groups["partial"]:
            status, error_code = "failed", "all_item_groups_failed"
        elif groups["failed"] or groups["partial"]:
            status, error_code = "partial", "item_group_incomplete"
        else:
            status, error_code = "succeeded", ""
        await self._executor._terminalize(
            connection,
            run_id=run_id,
            case_id=run["case_id"],
            status=status,
            error_code=error_code,
        )
        return ResearchExecutionResult(
            run_id=run_id,
            case_id=run["case_id"],
            status=status,
            error_code=error_code,
            branches_total=int(groups["branches_total"]),
            branches_succeeded=int(groups["branches_succeeded"]),
            branches_failed=int(groups["branches_failed"]),
            branches_blocked=int(groups["branches_blocked"]),
            candidates_persisted=int(groups["candidates_persisted"]),
            own_database_candidates=int(groups["own_database_candidates"]),
            newly_completed=True,
        )

    async def _after_checkpoint(
        self, connection: asyncpg.Connection, run_id: UUID
    ) -> ResearchExecutionResult | None:
        return await self._terminal_result_if_ready(connection, run_id)

    async def _existing_result(
        self, connection: asyncpg.Connection, run: Mapping[str, Any]
    ) -> ResearchExecutionResult:
        row = await connection.fetchrow(
            """
            SELECT coalesce(sum(branches_total),0) AS total,
                coalesce(sum(branches_succeeded),0) AS succeeded,
                coalesce(sum(branches_failed),0) AS failed,
                coalesce(sum(branches_blocked),0) AS blocked,
                coalesce(sum(candidates_persisted),0) AS persisted,
                coalesce(sum(own_database_candidates),0) AS known
            FROM research_item_groups WHERE research_run_id=$1
            """,
            run["id"],
        )
        return ResearchExecutionResult(
            run_id=run["id"],
            case_id=run["case_id"],
            status=run["status"],
            error_code=run["error_code"],
            branches_total=int(row["total"]),
            branches_succeeded=int(row["succeeded"]),
            branches_failed=int(row["failed"]),
            branches_blocked=int(row["blocked"]),
            candidates_persisted=int(row["persisted"]),
            own_database_candidates=int(row["known"]),
            newly_completed=False,
        )


def _uuid(value: object, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _json_array(value: object) -> list[str]:
    if isinstance(value, str):
        import json

        value = json.loads(value)
    if not isinstance(value, list) or any(not isinstance(entry, str) for entry in value):
        raise ResearchPlanDrift("checkpoint external ids must be a JSON string array")
    return list(dict.fromkeys(value))


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _cursor_state(value: object) -> dict[str, Any]:
    """Decode the durable provider cursor without trusting malformed checkpoint data."""
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ResearchPlanDrift("checkpoint cursor state is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ResearchPlanDrift("checkpoint cursor state must be a JSON object")
    current = value.get("current")
    seen = value.get("seen")
    if current is not None and not isinstance(current, str):
        raise ResearchPlanDrift("checkpoint cursor must be a URL or null")
    if not isinstance(seen, list) or any(not isinstance(entry, str) for entry in seen):
        raise ResearchPlanDrift("checkpoint seen cursors must be a JSON string array")
    return {"current": current, "seen": list(dict.fromkeys(seen))}


def _cursor_state_after_result(
    value: object,
    next_cursor: str | None,
    *,
    max_seen: int,
) -> tuple[dict[str, Any], bool, bool]:
    """Return bounded cursor state and whether this response advanced or repeated it."""
    state = _cursor_state(value)
    if next_cursor is None:
        return {"current": None, "seen": state["seen"][-max_seen:]}, False, False
    if not isinstance(next_cursor, str) or not next_cursor:
        raise ResearchPlanDrift("provider returned an invalid next cursor")
    if next_cursor == state["current"] or next_cursor in state["seen"]:
        return state, False, True
    seen = [*state["seen"], next_cursor][-max_seen:]
    return {"current": next_cursor, "seen": seen}, True, False


def _limited_plan(
    branch: _QueryBranch,
    *,
    batch_candidate_limit: int,
    excluded_external_ids: list[str],
    search_cursor: str | None = None,
    execution_generation: int | None = None,
) -> Any:
    """Add runtime paging controls without altering the stored plan or query hash."""
    return branch.plan.model_copy(
        update={
            "batch_candidate_limit": batch_candidate_limit,
            "excluded_external_ids": excluded_external_ids,
            "search_cursor": search_cursor,
            "execution_query_id": branch.id,
            "execution_generation": execution_generation,
        }
    )


def _batch_candidate_limit(source: ResearchSource) -> int:
    return _OLX_BATCH_CANDIDATE_LIMIT if source == ResearchSource.OLX else _BATCH_CANDIDATE_LIMIT


def _effective_batch_candidate_limit(
    source: ResearchSource,
    configured_limit: int | None,
) -> int:
    source_limit = _batch_candidate_limit(source)
    return source_limit if configured_limit is None else min(source_limit, configured_limit)


def _is_retryable_technical_failure(stop_reason: str) -> bool:
    return stop_reason.startswith(_RETRYABLE_TECHNICAL_FAILURE_PREFIXES)


def _safe_error_code(exc: BaseException) -> str:
    diagnostic_code = getattr(exc, "diagnostic_code", None)
    if isinstance(diagnostic_code, str):
        diagnostic = safe_provider_error_code(exc)
        if diagnostic == diagnostic_code:
            if isinstance(exc, RetryableProviderError):
                return f"RetryableProviderError:{diagnostic}"
            return f"{type(exc).__name__}:{diagnostic}"
    value = str(exc).splitlines()[0][:160].strip()
    return f"{type(exc).__name__}:{value}" if value else type(exc).__name__


def _candidate_receipts(
    result: Any,
    *,
    batch_no: int,
    locality: LocalityConstraint | None = None,
) -> list[dict[str, Any]]:
    """Keep per-card contact discovery outcomes without copying private numbers."""
    receipts: list[dict[str, Any]] = []
    for candidate in result.candidates:
        contact_types = sorted({contact.kind.value for contact in candidate.contacts})
        reason = candidate.contact_discovery_reason
        if reason is None:
            reason = (
                "phone_found"
                if {"phone", "whatsapp"}.intersection(contact_types)
                else "contact_found"
                if contact_types
                else "not_attempted"
            )
        receipt = {
            "batch_no": batch_no,
            "source": candidate.source.value,
            "external_id": candidate.external_id or "",
            "contact_discovery_reason": reason,
            "contact_types": contact_types,
        }
        if locality is not None:
            receipt["geographic_decision"] = decide_geographic_scope(
                locality,
                address_city=getattr(candidate, "address_city", None),
                address_text=candidate.address,
                point=candidate.point,
                provider_selected_city=(
                    locality.city if locality.scope.value == "citywide" else None
                ),
            ).value
        receipts.append(receipt)
    return receipts
