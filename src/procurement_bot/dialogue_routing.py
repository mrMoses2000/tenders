from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.case_queries import parse_case_query
from procurement_bot.purchase_workflow import parse_close_request


class DialogueIntent(StrEnum):
    CREATE_REQUEST = "create_request"
    UPDATE_REQUEST = "update_request"
    CASE_QUERY = "case_query"
    CLOSE_POSITION = "close_position"
    ORDINARY = "ordinary"
    ACTIVE_CASE_CONFLICT = "active_case_conflict"


class DialogueTurnConflict(RuntimeError):
    """A persisted routing decision no longer matches its immutable message."""


@dataclass(frozen=True, slots=True)
class DialogueRoute:
    intent: DialogueIntent
    reason_code: str

    @property
    def permits_intake(self) -> bool:
        return self.intent in {
            DialogueIntent.CREATE_REQUEST,
            DialogueIntent.UPDATE_REQUEST,
        }


@dataclass(frozen=True, slots=True)
class PersistedDialogueTurn:
    message_id: UUID
    user_id: UUID
    case_id: UUID | None
    intent: DialogueIntent
    reason_code: str
    input_kind: str
    context_version: int
    open_clarification_id: UUID | None
    status: str


_EXPLICIT_REQUEST = re.compile(
    r"(?:"
    r"\b(?:создай|создать|оформи|оформить|начни)\b.{0,40}\b(?:заявк\w*|тендер\w*)\b"
    r"|\b(?:найди|найти|поищи|подбери|ищу|закупи|купить)\b"
    r"|\b(?:нужно|надо|требуется)\s+(?:найти|купить|закупить)\b"
    r"|\b(?:тауып\s+бер|ізде|іздеп\s+бер|сатып\s+алу)\b"
    r"|\b(?:өтінім|тендер)\s*[:—-]"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_STRUCTURED_ITEM = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:шт\.?|штук\w*|кг|г|л|м|метр\w*|"
    r"упак\w*|рулон\w*|комплект\w*|дана|қап\w*)\b",
    re.IGNORECASE,
)
_ORDINARY = re.compile(
    r"^(?:привет|здравствуйте|сәлем|сәлеметсіз\s+бе|спасибо|рахмет|рақмет|"
    r"понял|понятно|хорошо|ладно|ок(?:ей)?|ты\s+кто|что\s+ты\s+умеешь)"
    r"[\s!.,?]*$",
    re.IGNORECASE,
)
_OFF_TOPIC = re.compile(
    r"\b(?:анекдот|погод\w*|новост\w*|рецепт\w*|фильм\w*|музык\w*|"
    r"переведи|расскажи\s+про|кто\s+такой)\b",
    re.IGNORECASE,
)
_NEW_REQUEST_WORDS = re.compile(
    r"\b(?:новая|новую|новой|жаңа)\s+(?:заявк\w*|өтінім\w*)\b",
    re.IGNORECASE,
)


def route_dialogue_turn(
    text: str,
    *,
    input_kind: str,
    active_case_id: UUID | None,
    has_open_clarification: bool,
) -> DialogueRoute:
    """Choose a side-effect boundary without a model or external service."""

    normalized = " ".join(text.casefold().split())
    if parse_close_request(text) is not None:
        return DialogueRoute(DialogueIntent.CLOSE_POSITION, "explicit_close_position")
    if parse_case_query(text) is not None:
        return DialogueRoute(DialogueIntent.CASE_QUERY, "supported_read_query")

    if input_kind == "document":
        intent = (
            DialogueIntent.UPDATE_REQUEST
            if active_case_id is not None
            else DialogueIntent.CREATE_REQUEST
        )
        return DialogueRoute(intent, "procurement_document")
    if input_kind == "location":
        if active_case_id is not None:
            return DialogueRoute(DialogueIntent.UPDATE_REQUEST, "location_for_active_request")
        return DialogueRoute(DialogueIntent.ORDINARY, "location_without_active_request")
    if input_kind == "photo":
        return DialogueRoute(DialogueIntent.ORDINARY, "photo_requires_supported_source")

    if _ORDINARY.fullmatch(normalized):
        return DialogueRoute(DialogueIntent.ORDINARY, "social_message")
    if _OFF_TOPIC.search(normalized):
        return DialogueRoute(DialogueIntent.ORDINARY, "off_topic_message")

    explicit_request = bool(_EXPLICIT_REQUEST.search(normalized))
    structured_item = bool(_STRUCTURED_ITEM.search(normalized))
    explicit_new = bool(_NEW_REQUEST_WORDS.search(normalized))

    if active_case_id is not None:
        if explicit_new:
            return DialogueRoute(
                DialogueIntent.ACTIVE_CASE_CONFLICT,
                "new_request_while_active_case_exists",
            )
        if has_open_clarification:
            return DialogueRoute(DialogueIntent.UPDATE_REQUEST, "answer_to_open_clarification")
        if explicit_request or structured_item:
            return DialogueRoute(DialogueIntent.UPDATE_REQUEST, "request_update")
        return DialogueRoute(DialogueIntent.ORDINARY, "unclassified_with_active_request")

    if explicit_request:
        return DialogueRoute(DialogueIntent.CREATE_REQUEST, "explicit_procurement_request")
    if structured_item:
        return DialogueRoute(DialogueIntent.CREATE_REQUEST, "structured_item_list")
    return DialogueRoute(DialogueIntent.ORDINARY, "no_procurement_intent")


async def persist_dialogue_route(
    connection: asyncpg.Connection,
    *,
    message_id: UUID,
    user_id: UUID,
    case_id: UUID | None,
    route: DialogueRoute,
    input_kind: str,
    context_version: int,
    open_clarification_id: UUID | None,
) -> PersistedDialogueTurn:
    row = await connection.fetchrow(
        """
        INSERT INTO dialogue_turns(
            message_id,user_id,case_id,intent,reason_code,input_kind,
            context_version,open_clarification_id
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING *
        """,
        message_id,
        user_id,
        case_id,
        route.intent.value,
        route.reason_code,
        input_kind,
        context_version,
        open_clarification_id,
    )
    if row is None:
        row = await connection.fetchrow(
            "SELECT * FROM dialogue_turns WHERE message_id=$1",
            message_id,
        )
    if row is None:
        raise DialogueTurnConflict("dialogue turn insert conflict vanished")
    persisted = _turn_from_row(row)
    expected = (
        user_id,
        case_id,
        route.intent,
        route.reason_code,
        input_kind,
        context_version,
        open_clarification_id,
    )
    actual = (
        persisted.user_id,
        persisted.case_id,
        persisted.intent,
        persisted.reason_code,
        persisted.input_kind,
        persisted.context_version,
        persisted.open_clarification_id,
    )
    if actual != expected:
        raise DialogueTurnConflict("dialogue routing decision drifted on retry")
    return persisted


async def load_dialogue_turn(
    connection: asyncpg.Connection,
    message_id: UUID,
) -> PersistedDialogueTurn | None:
    row = await connection.fetchrow(
        "SELECT * FROM dialogue_turns WHERE message_id=$1",
        message_id,
    )
    return _turn_from_row(row) if row is not None else None


async def finish_dialogue_turn(
    connection: asyncpg.Connection,
    *,
    message_id: UUID,
    case_id: UUID | None,
    status: str,
    response_kind: str,
) -> None:
    if status not in {"applied", "ignored", "failed"}:
        raise ValueError("dialogue turn terminal status is invalid")
    changed = await connection.execute(
        """
        UPDATE dialogue_turns
        SET case_id=COALESCE(case_id,$2),status=$3,response_kind=$4,processed_at=now()
        WHERE message_id=$1
          AND (status='planned' OR (status=$3 AND response_kind=$4))
          AND (case_id IS NULL OR $2 IS NULL OR case_id=$2)
        """,
        message_id,
        case_id,
        status,
        response_kind,
    )
    if changed != "UPDATE 1":
        raise DialogueTurnConflict("dialogue turn could not be completed consistently")


def ordinary_reply(*, active_case_title: str | None, reason_code: str) -> str:
    if reason_code == "location_without_active_request":
        return "Сначала пришлите заявку или напишите, какой товар нужно найти."
    if reason_code == "new_request_while_active_case_exists":
        return (
            f"Сейчас открыта заявка «{active_case_title or 'Без названия'}». "
            "Сначала закончим её или явно отменим, затем я создам новую."
        )
    if active_case_title:
        return (
            f"Я помню заявку «{active_case_title}». Ответьте на последний вопрос "
            "или спросите: «Что там?»"
        )
    return (
        "Пришлите заявку файлом или напишите, например: "
        "«Найди 20 рулонов марли в Алматы»."
    )


def _turn_from_row(row: Any) -> PersistedDialogueTurn:
    return PersistedDialogueTurn(
        message_id=row["message_id"],
        user_id=row["user_id"],
        case_id=row["case_id"],
        intent=DialogueIntent(row["intent"]),
        reason_code=row["reason_code"],
        input_kind=row["input_kind"],
        context_version=int(row["context_version"]),
        open_clarification_id=row["open_clarification_id"],
        status=row["status"],
    )
