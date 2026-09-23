from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from pydantic import ValidationError

from procurement_bot.intake import ParsedProcurementRequest, SearchScope
from procurement_bot.locality_workflow import (
    LocalityClarificationNotice,
    LocalityResolutionContext,
    LocalityWorkflowDenied,
    PersistedLocalityOutcome,
)
from procurement_bot.queue import enqueue_job, enqueue_outbox, stable_key
from procurement_bot.research import LocalityResolution, LocalityResolutionStatus


class LocalityPersistenceConflict(RuntimeError):
    """A completed locality lookup was retried with different immutable data."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("locality data must contain finite JSON values") from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def locality_lookup_sha256(*, city: str, phrase: str) -> str:
    """Return the PII-safe identity of one city/phrase tuple."""

    if not city.strip() or not phrase.strip():
        raise ValueError("city and phrase must be non-empty")
    return _sha256_json({"city": city.strip().casefold(), "phrase": phrase.strip().casefold()})


def _as_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise LocalityWorkflowDenied("applied intake payload is not an object")
    return dict(value)


def _context_token(row: Mapping[str, Any], request: ParsedProcurementRequest) -> str:
    return _sha256_json(
        {
            "contract_version": 1,
            "case_id": str(row["case_id"]),
            "owner_user_id": str(row["owner_user_id"]),
            "telegram_chat_id": int(row["telegram_chat_id"]),
            "owner_active": bool(row["owner_active"]),
            "case_status": str(row["case_status"]),
            "case_parse_version_id": str(row["case_parse_version_id"]),
            "intake_version": int(row["intake_version"]),
            "request": request.model_dump(mode="json"),
        }
    )


def _parse_request(row: Mapping[str, Any]) -> ParsedProcurementRequest:
    try:
        request = ParsedProcurementRequest.model_validate(_as_object(row["normalized_payload"]))
    except (ValidationError, ValueError, TypeError) as exc:
        raise LocalityWorkflowDenied("applied intake payload is invalid") from exc
    if request.search_scope != SearchScope.SPECIFIC_AREA:
        raise LocalityWorkflowDenied("request no longer requires locality resolution")
    if request.city is None or request.search_area_text is None:
        raise LocalityWorkflowDenied("specific-area request has no city or phrase")
    return request


def _outcome(row: Mapping[str, Any], *, newly_persisted: bool) -> PersistedLocalityOutcome:
    status = LocalityResolutionStatus(str(row["status"]))
    return PersistedLocalityOutcome(
        resolution_id=row["id"],
        resolution_version=int(row["version"]),
        status=status,
        newly_persisted=newly_persisted,
        followup_kind=(
            "prepare_research"
            if status == LocalityResolutionStatus.RESOLVED
            else "telegram_clarification"
        ),
    )


_CONTEXT_SQL = """
SELECT c.id AS case_id,c.owner_user_id,c.status AS case_status,
       u.telegram_id AS telegram_chat_id,u.active AS owner_active,
       p.id AS case_parse_version_id,p.version AS intake_version,
       p.normalized_payload
FROM procurement_cases AS c
JOIN users AS u ON u.id=c.owner_user_id
JOIN case_parse_versions AS p ON p.case_id=c.id AND p.status='applied'
WHERE c.id=$1 AND c.owner_user_id=$2 AND p.version=$3
"""


class PostgreSQLLocalityResolutionRepository:
    """PostgreSQL implementation of the locality workflow persistence boundary."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def load_context(
        self,
        *,
        case_id: UUID,
        intake_version: int,
        owner_user_id: UUID,
    ) -> LocalityResolutionContext:
        if not isinstance(case_id, UUID) or not isinstance(owner_user_id, UUID):
            raise TypeError("case_id and owner_user_id must be UUIDs")
        if intake_version < 1:
            raise ValueError("intake_version must be positive")
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                _CONTEXT_SQL,
                case_id,
                owner_user_id,
                intake_version,
            )
            if row is None:
                raise LocalityWorkflowDenied("case, owner, or intake version changed")
            if row["owner_active"] is not True:
                raise LocalityWorkflowDenied("case owner is inactive")
            request = _parse_request(row)
            assert request.city is not None
            assert request.search_area_text is not None
            lookup_hash = locality_lookup_sha256(
                city=request.city,
                phrase=request.search_area_text,
            )
            completed_row = await connection.fetchrow(
                """
                SELECT id,version,status,result_sha256,context_token
                FROM locality_resolutions
                WHERE case_id=$1 AND intake_version=$2 AND lookup_sha256=$3
                ORDER BY version DESC
                LIMIT 1
                """,
                case_id,
                intake_version,
                lookup_hash,
            )
            completed = (
                _outcome(completed_row, newly_persisted=False)
                if completed_row is not None
                else None
            )
            if completed is None and row["case_status"] != "needs_clarification":
                raise LocalityWorkflowDenied("case is not awaiting locality clarification")
            if completed is not None:
                expected_status = (
                    "ready"
                    if completed.status == LocalityResolutionStatus.RESOLVED
                    else "needs_clarification"
                )
                if row["case_status"] != expected_status:
                    raise LocalityWorkflowDenied("completed locality projection is stale")
            return LocalityResolutionContext(
                case_id=case_id,
                owner_user_id=owner_user_id,
                telegram_chat_id=int(row["telegram_chat_id"]),
                intake_version=int(row["intake_version"]),
                case_status=str(row["case_status"]),
                request=request,
                context_token=_context_token(row, request),
                completed=completed,
            )

    async def commit_resolution(
        self,
        *,
        context: LocalityResolutionContext,
        resolution: LocalityResolution,
        clarification: LocalityClarificationNotice | None,
    ) -> PersistedLocalityOutcome:
        self._validate_commit_input(context, resolution, clarification)
        request = context.request
        assert request.city is not None
        assert request.search_area_text is not None
        lookup_hash = locality_lookup_sha256(
            city=request.city,
            phrase=request.search_area_text,
        )
        candidates = [candidate.model_dump(mode="json") for candidate in resolution.candidates]
        result_payload = resolution.model_dump(mode="json")
        selected = (
            resolution.selected.model_dump(mode="json") if resolution.selected is not None else None
        )
        candidates_hash = _sha256_json(candidates)
        result_hash = _sha256_json(result_payload)
        selected_hash = _sha256_json(selected) if selected is not None else None

        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                _CONTEXT_SQL + " FOR UPDATE OF c,p",
                context.case_id,
                context.owner_user_id,
                context.intake_version,
            )
            if row is None:
                raise LocalityWorkflowDenied("case ownership or intake version changed")
            if row["owner_active"] is not True:
                raise LocalityWorkflowDenied("case owner is inactive")
            current_request = _parse_request(row)
            if current_request != request:
                raise LocalityWorkflowDenied("applied intake payload changed")
            current_lookup_hash = locality_lookup_sha256(
                city=current_request.city or "",
                phrase=current_request.search_area_text or "",
            )
            if not hmac.compare_digest(current_lookup_hash, lookup_hash):
                raise LocalityWorkflowDenied("locality city or phrase changed")

            existing = await connection.fetchrow(
                """
                SELECT id,version,status,result_sha256,context_token
                FROM locality_resolutions
                WHERE case_id=$1 AND intake_version=$2 AND lookup_sha256=$3
                ORDER BY version DESC
                LIMIT 1
                FOR UPDATE
                """,
                context.case_id,
                context.intake_version,
                lookup_hash,
            )
            if existing is not None:
                if not hmac.compare_digest(str(existing["context_token"]), context.context_token):
                    raise LocalityWorkflowDenied("locality context token is stale")
                if not hmac.compare_digest(str(existing["result_sha256"]), result_hash):
                    raise LocalityPersistenceConflict(
                        "locality context already has a different immutable result"
                    )
                expected_case_status = (
                    "ready"
                    if existing["status"] == LocalityResolutionStatus.RESOLVED.value
                    else "needs_clarification"
                )
                if row["case_status"] != expected_case_status:
                    raise LocalityWorkflowDenied("completed locality projection is stale")
                return _outcome(existing, newly_persisted=False)

            current_token = _context_token(row, current_request)
            if not hmac.compare_digest(current_token, context.context_token):
                raise LocalityWorkflowDenied("locality context token is stale")
            if row["case_status"] != "needs_clarification":
                raise LocalityWorkflowDenied("case is not awaiting locality clarification")

            version = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(MAX(version),0)+1
                    FROM locality_resolutions
                    WHERE case_id=$1 AND intake_version=$2 AND lookup_sha256=$3
                    """,
                    context.case_id,
                    context.intake_version,
                    lookup_hash,
                )
            )
            resolution_id = uuid4()
            idempotency_key = stable_key(
                "locality-resolution",
                context.case_id,
                context.intake_version,
                lookup_hash,
                result_hash,
            )
            await connection.execute(
                """
                INSERT INTO locality_resolutions(
                    id,case_id,case_parse_version_id,intake_version,version,
                    city,phrase,lookup_sha256,status,candidates,candidates_sha256,
                    result_payload,result_sha256,selected_candidate,
                    selected_candidate_sha256,clarification_question,context_token,
                    idempotency_key
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12::jsonb,$13,
                    $14::jsonb,$15,$16,$17,$18
                )
                """,
                resolution_id,
                context.case_id,
                row["case_parse_version_id"],
                context.intake_version,
                version,
                resolution.city,
                resolution.phrase,
                lookup_hash,
                resolution.status.value,
                candidates,
                candidates_hash,
                result_payload,
                result_hash,
                selected,
                selected_hash,
                resolution.clarification_question,
                context.context_token,
                idempotency_key,
            )
            if resolution.status == LocalityResolutionStatus.RESOLVED:
                await self._commit_resolved(
                    connection,
                    context=context,
                    resolution_id=resolution_id,
                    resolution_version=version,
                )
            else:
                assert clarification is not None
                await self._commit_clarification(
                    connection,
                    context=context,
                    resolution_id=resolution_id,
                    resolution_version=version,
                    clarification=clarification,
                )
        return PersistedLocalityOutcome(
            resolution_id=resolution_id,
            resolution_version=version,
            status=resolution.status,
            newly_persisted=True,
            followup_kind=(
                "prepare_research"
                if resolution.status == LocalityResolutionStatus.RESOLVED
                else "telegram_clarification"
            ),
        )

    @staticmethod
    def _validate_commit_input(
        context: LocalityResolutionContext,
        resolution: LocalityResolution,
        clarification: LocalityClarificationNotice | None,
    ) -> None:
        request = context.request
        if request.city is None or request.search_area_text is None:
            raise LocalityWorkflowDenied("locality request is incomplete")
        if resolution.city != request.city or resolution.phrase != request.search_area_text:
            raise LocalityWorkflowDenied("resolution city or phrase differs from intake")
        if resolution.status == LocalityResolutionStatus.RESOLVED:
            if clarification is not None:
                raise ValueError("resolved locality cannot enqueue a clarification")
        else:
            if clarification is None:
                raise ValueError("ambiguous locality requires a clarification notice")
            if clarification.chat_id != context.telegram_chat_id:
                raise LocalityWorkflowDenied("clarification targets a different Telegram chat")

    @staticmethod
    async def _commit_resolved(
        connection: asyncpg.Connection,
        *,
        context: LocalityResolutionContext,
        resolution_id: UUID,
        resolution_version: int,
    ) -> None:
        changed = await connection.execute(
            """
            UPDATE procurement_cases
            SET status='ready',updated_at=now()
            WHERE id=$1 AND owner_user_id=$2 AND status='needs_clarification'
            """,
            context.case_id,
            context.owner_user_id,
        )
        if changed != "UPDATE 1":
            raise LocalityWorkflowDenied("case changed before locality resolution commit")
        payload = {
            "case_id": str(context.case_id),
            "intake_version": context.intake_version,
            "locality_resolution_id": str(resolution_id),
            "locality_resolution_version": resolution_version,
        }
        await enqueue_job(
            connection,
            kind="prepare_research",
            payload=payload,
            idempotency_key=stable_key(
                "prepare-research-locality",
                resolution_id,
                resolution_version,
            ),
        )

    @staticmethod
    async def _commit_clarification(
        connection: asyncpg.Connection,
        *,
        context: LocalityResolutionContext,
        resolution_id: UUID,
        resolution_version: int,
        clarification: LocalityClarificationNotice,
    ) -> None:
        changed = await connection.execute(
            """
            UPDATE procurement_cases
            SET status='needs_clarification',updated_at=now()
            WHERE id=$1 AND owner_user_id=$2 AND status='needs_clarification'
            """,
            context.case_id,
            context.owner_user_id,
        )
        if changed != "UPDATE 1":
            raise LocalityWorkflowDenied("case changed before clarification commit")
        await enqueue_outbox(
            connection,
            event_type="telegram.send_message",
            payload={"chat_id": clarification.chat_id, "text": clarification.text},
            idempotency_key=stable_key(
                "telegram-locality-clarification",
                resolution_id,
                resolution_version,
            ),
        )
