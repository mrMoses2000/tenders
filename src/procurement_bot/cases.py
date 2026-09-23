from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from procurement_bot.intake import IntakeResult, ParsedProcurementRequest
from procurement_bot.queue import enqueue_job, enqueue_outbox, stable_key


class StaleIntakeContext(RuntimeError):
    """Another message changed the active draft while extraction was running."""


@dataclass(frozen=True, slots=True)
class IntakeContext:
    user_id: UUID
    chat_id: int
    case_id: UUID | None
    version: int
    request: ParsedProcurementRequest | None
    case_title: str | None = None
    open_clarification_id: UUID | None = None
    open_clarification_topic: str | None = None
    open_clarification_question: str | None = None


async def load_intake_context(pool: asyncpg.Pool, message_id: UUID) -> IntakeContext:
    async with pool.acquire() as connection:
        message = await connection.fetchrow(
            """
            SELECT user_id, external_chat_id
            FROM messages
            WHERE id=$1 AND channel='telegram' AND direction='inbound'
            """,
            message_id,
        )
        if message is None:
            raise ValueError(f"unknown inbound message: {message_id}")
        if message["user_id"] is None:
            raise ValueError("inbound message is not linked to a user")
        row = await connection.fetchrow(
            """
            SELECT c.id AS case_id,c.title,p.version,p.normalized_payload,
                   clarification.id AS clarification_id,
                   clarification.topic AS clarification_topic,
                   clarification.question AS clarification_question
            FROM procurement_cases AS c
            JOIN case_parse_versions AS p ON p.case_id=c.id AND p.status='applied'
            LEFT JOIN LATERAL (
                SELECT id,topic,question
                FROM clarifications
                WHERE case_id=c.id AND status='open'
                ORDER BY created_at,id
                LIMIT 1
            ) AS clarification ON TRUE
            WHERE c.owner_user_id=$1 AND c.status IN ('draft','needs_clarification')
            ORDER BY c.updated_at DESC
            LIMIT 1
            """,
            message["user_id"],
        )
    if row is None:
        return IntakeContext(
            user_id=message["user_id"],
            chat_id=int(message["external_chat_id"]),
            case_id=None,
            version=0,
            request=None,
        )
    return IntakeContext(
        user_id=message["user_id"],
        chat_id=int(message["external_chat_id"]),
        case_id=row["case_id"],
        version=row["version"],
        request=ParsedProcurementRequest.model_validate(row["normalized_payload"]),
        case_title=row["title"],
        open_clarification_id=row["clarification_id"],
        open_clarification_topic=row["clarification_topic"],
        open_clarification_question=row["clarification_question"],
    )


async def persist_intake_result(
    pool: asyncpg.Pool,
    *,
    message_id: UUID,
    source_extraction_id: UUID | None,
    context: IntakeContext,
    result: IntakeResult,
) -> UUID:
    """Atomically apply a parsed request, projections, questions, and TG outbox."""

    payload = result.request.model_dump(mode="json")
    status = "ready" if result.ready_to_search else "needs_clarification"
    async with pool.acquire() as connection, connection.transaction():
        # Serialise creation/application per Telegram user. AGY runs outside the
        # transaction, so a second message may otherwise create a parallel draft.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1::text,0))",
            str(context.user_id),
        )
        already_applied = await connection.fetchval(
            "SELECT case_id FROM case_parse_versions WHERE source_message_id=$1",
            message_id,
        )
        if already_applied is not None:
            return already_applied
        if context.case_id is None:
            active_case = await connection.fetchval(
                """
                SELECT id FROM procurement_cases
                WHERE owner_user_id=$1 AND status IN ('draft','needs_clarification')
                ORDER BY updated_at DESC LIMIT 1
                """,
                context.user_id,
            )
            if active_case is not None:
                raise StaleIntakeContext("a draft was created while intake was running")
            case_id = uuid4()
            await connection.execute(
                """
                INSERT INTO procurement_cases(
                    id,owner_user_id,source_message_id,title,city,search_area_text,
                    delivery_address,status
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                """,
                case_id,
                context.user_id,
                message_id,
                _case_title(result.request),
                result.request.city or "",
                result.request.search_area_text or "",
                result.request.delivery_address or "",
                status,
            )
            version = 1
        else:
            case_id = context.case_id
            current_version = await connection.fetchval(
                """
                SELECT version
                FROM case_parse_versions
                WHERE case_id=$1 AND status='applied'
                FOR UPDATE
                """,
                case_id,
            )
            if current_version != context.version:
                raise StaleIntakeContext("active procurement request changed")
            version = context.version + 1
            await connection.execute(
                """
                UPDATE procurement_cases
                SET title=$2,city=$3,search_area_text=$4,delivery_address=$5,
                    status=$6,updated_at=now()
                WHERE id=$1
                """,
                case_id,
                _case_title(result.request),
                result.request.city or "",
                result.request.search_area_text or "",
                result.request.delivery_address or "",
                status,
            )
            await connection.execute(
                """
                UPDATE case_parse_versions
                SET status='superseded'
                WHERE case_id=$1 AND status='applied'
                """,
                case_id,
            )

        await connection.execute(
            """
            INSERT INTO case_parse_versions(
                case_id,source_message_id,source_extraction_id,version,
                parser_name,parser_version,normalized_payload,status,applied_at
            ) VALUES ($1,$2,$3,$4,'agy-intake','1',$5::jsonb,'applied',now())
            """,
            case_id,
            message_id,
            source_extraction_id,
            version,
            payload,
        )
        await _replace_item_projection(connection, case_id, result)
        await connection.execute(
            "UPDATE clarifications SET status='dismissed' WHERE case_id=$1 AND status='open'",
            case_id,
        )
        asked_questions = set(result.clarifications)
        for blocker in result.blockers:
            if str(blocker["question"]) not in asked_questions:
                continue
            await connection.execute(
                """
                INSERT INTO clarifications(case_id,asked_in_message_id,topic,question)
                VALUES ($1,$2,$3,$4)
                """,
                case_id,
                message_id,
                str(blocker.get("code", blocker.get("field", "unknown"))),
                str(blocker["question"]),
            )
        await connection.execute(
            """
            INSERT INTO workflow_events(
                case_id,event_type,actor_type,actor_id,idempotency_key,data
            ) VALUES ($1,'intake_applied','worker','intake',$2,$3::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            case_id,
            stable_key("workflow.intake", message_id),
            {
                "version": version,
                "ready_to_search": result.ready_to_search,
                "applied_defaults": result.applied_defaults,
            },
        )
        reply = _telegram_reply(result, case_id)
        await enqueue_outbox(
            connection,
            event_type="telegram.send_message",
            payload={"chat_id": context.chat_id, "text": reply},
            idempotency_key=stable_key("telegram.intake-result", message_id, version),
        )
        if result.ready_to_search:
            await enqueue_job(
                connection,
                kind="prepare_research",
                payload={"case_id": str(case_id), "intake_version": version},
                idempotency_key=stable_key("prepare-research", case_id, version),
            )
        # When called by the deterministic dialogue worker, completing the
        # routing turn in this transaction prevents a case mutation from being
        # committed without its corresponding dialogue state.
        await connection.execute(
            """
            UPDATE dialogue_turns
            SET case_id=$2,status='applied',response_kind='intake',processed_at=now()
            WHERE message_id=$1 AND status IN ('planned','applied')
              AND (case_id IS NULL OR case_id=$2)
            """,
            message_id,
            case_id,
        )
    return case_id


async def _replace_item_projection(
    connection: asyncpg.Connection,
    case_id: UUID,
    result: IntakeResult,
) -> None:
    await connection.execute("DELETE FROM request_items WHERE case_id=$1", case_id)
    item_status = "ready" if result.ready_to_search else "needs_clarification"
    for line_number, item in enumerate(result.request.items, start=1):
        item_id = await connection.fetchval(
            """
            INSERT INTO request_items(
                case_id,line_number,name,specification_text,quantity,unit,
                analogs_allowed,status
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING id
            """,
            case_id,
            line_number,
            item.name or f"Позиция {line_number}",
            "; ".join(_requirement_text(value) for value in item.requirements),
            item.quantity,
            item.unit or "",
            item.analogs_allowed,
            item_status,
        )
        for requirement in item.requirements:
            expected: dict[str, Any] = {
                "value": requirement.expected_value,
                "unit": requirement.unit,
            }
            await connection.execute(
                """
                INSERT INTO item_requirements(
                    request_item_id,requirement_key,expected_value,is_hard,
                    source_pointer,confidence
                ) VALUES ($1,$2,$3::jsonb,$4,$5,$6)
                """,
                item_id,
                requirement.name,
                expected,
                requirement.hard_requirement,
                requirement.source_reference or "",
                requirement.confidence,
            )


def _requirement_text(requirement: Any) -> str:
    value = requirement.expected_value or ""
    unit = requirement.unit or ""
    return " ".join(part for part in (requirement.name, value, unit) if part)


def _case_title(request: ParsedProcurementRequest) -> str:
    names = [item.name for item in request.items if item.name]
    return ", ".join(names[:3])[:500] or "Новая заявка"


def _telegram_reply(result: IntakeResult, _case_id: UUID) -> str:
    item_count = len(result.request.items)
    item_word = _item_word(item_count)
    if result.ready_to_search:
        scope_value = (
            result.request.search_scope.value
            if result.request.search_scope is not None
            else ""
        )
        scope = {
            "citywide": "по всему городу",
            "specific_area": result.request.search_area_text or "в указанном районе",
            "online_with_delivery": "в интернете с доставкой",
        }.get(scope_value, "по вашим условиям")
        return (
            f"Заявку разобрал: {item_count} {item_word}. "
            f"Ищу в городе {result.request.city}, {scope}. "
            "Сейчас подготовлю поиск и покажу его перед запуском."
        )
    intro = (
        f"Заявку разобрал: {item_count} {_item_word(item_count)}."
        if item_count
        else "Пока не понял, какой именно товар нужно найти."
    )
    questions = "\n".join(f"• {value}" for value in result.clarifications)
    return f"{intro}\n\nПеред поиском уточните:\n{questions}"


def _item_word(value: int) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return "позиция"
    if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
        return "позиции"
    return "позиций"
