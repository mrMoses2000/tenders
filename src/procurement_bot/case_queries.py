from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.accounting import load_financial_snapshot


class CaseQueryIntent(StrEnum):
    SUMMARY = "summary"
    OPEN_ITEMS = "open_items"
    CUSTOMER_ACCEPTED = "customer_accepted"
    FINANCIAL = "financial"


@dataclass(frozen=True, slots=True)
class ParsedCaseQuery:
    intent: CaseQueryIntent
    case_reference: str | None


@dataclass(frozen=True, slots=True)
class CaseQueryAnswer:
    case_id: UUID | None
    text: str
    as_of: datetime | None
    needs_clarification: bool = False


_FULL_UUID = re.compile(
    r"(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE,
)
_SHORT_REFERENCE = re.compile(r"(?<![0-9a-f])#([0-9a-f]{8})(?![0-9a-f])", re.IGNORECASE)


def parse_case_query(text: str) -> ParsedCaseQuery | None:
    """Recognize a deliberately small RU/KZ read-only command language."""

    normalized = " ".join(text.casefold().split())
    if not normalized or "?" not in text and len(normalized) > 300:
        return None
    reference_match = _FULL_UUID.search(normalized) or _SHORT_REFERENCE.search(normalized)
    reference = reference_match.group(1).lower() if reference_match else None

    open_item = (
        any(token in normalized for token in ("сколько", "қанша"))
        and any(token in normalized for token in ("позиц", "товар", "тауар"))
        and any(
            token in normalized
            for token in ("не закры", "остал", "открыт", "жабылмаған", "қал")
        )
    )
    accepted = (
        any(token in normalized for token in ("сколько", "қанша"))
        and any(token in normalized for token in ("позиц", "товар", "тауар"))
        and any(token in normalized for token in ("принял", "приняло", "приняты", "қабылда"))
    )
    financial = any(
        token in normalized
        for token in (
            "в плюс",
            "в минус",
            "маржа",
            "прибыл",
            "убыт",
            "себестоим",
            "қаржы",
            "пайда",
            "шығын",
        )
    ) and any(token in normalized for token in ("заяв", "тендер", "кейс", "өтінім"))
    conversational_status = normalized.rstrip("?!.") in {
        "что там",
        "ну что там",
        "как дела",
        "есть новости",
        "что с заявкой",
        "как там заявка",
        "что по заявке",
    } or any(
        phrase in normalized
        for phrase in (
            "с чего ты взял",
            "какая заявка",
            "что происходит",
            "почему ты спрашиваешь",
        )
    )
    summary = conversational_status or any(
        token in normalized
        for token in ("инфо", "информац", "данн", "статус", "мәлімет")
    ) and any(token in normalized for token in ("заяв", "тендер", "кейс", "өтінім"))
    matches = [open_item, accepted, financial, summary]
    if sum(matches) != 1:
        return None
    intent = (
        CaseQueryIntent.OPEN_ITEMS
        if open_item
        else CaseQueryIntent.CUSTOMER_ACCEPTED
        if accepted
        else CaseQueryIntent.FINANCIAL
        if financial
        else CaseQueryIntent.SUMMARY
    )
    return ParsedCaseQuery(intent=intent, case_reference=reference)


async def answer_case_query(
    connection: asyncpg.Connection,
    *,
    owner_user_id: UUID,
    query: ParsedCaseQuery,
) -> CaseQueryAnswer:
    """Answer from PostgreSQL facts only; ambiguity returns a clarification."""

    case = await _resolve_case(connection, owner_user_id, query.case_reference)
    if isinstance(case, CaseQueryAnswer):
        return case
    case_id: UUID = case["id"]
    as_of: datetime = case["updated_at"]
    label = f"«{case['title'] or 'Без названия'}»"

    if query.intent == CaseQueryIntent.OPEN_ITEMS:
        counts = await _item_counts(connection, case_id)
        return CaseQueryAnswer(
            case_id,
            (
                f"По заявке {label}: осталось {counts['open']} позиций, "
                f"закрыто {counts['closed']} из {counts['total']}."
            ),
            as_of,
        )
    if query.intent == CaseQueryIntent.CUSTOMER_ACCEPTED:
        if case["customer_id"] is None:
            return CaseQueryAnswer(
                case_id,
                f"Для заявки {label} пока не указано предприятие-получатель.",
                as_of,
                True,
            )
        accepted = int(
            await connection.fetchval(
                """
                SELECT count(*) FROM (
                    SELECT event.request_item_id
                    FROM customer_acceptance_events AS event
                    WHERE event.case_id=$1 AND event.customer_id=$2
                    GROUP BY event.request_item_id
                    HAVING sum(CASE WHEN event.event_type='accepted'
                                    THEN event.accepted_quantity
                                    ELSE -event.accepted_quantity END) > 0
                ) AS accepted_items
                """,
                case_id,
                case["customer_id"],
            )
            or 0
        )
        return CaseQueryAnswer(
            case_id,
            (
                f"По заявке {label} предприятие «{case['customer_name']}» "
                f"приняло {accepted} позиций. Учтены только сохранённые события приёмки."
            ),
            as_of,
        )
    if query.intent == CaseQueryIntent.FINANCIAL:
        snapshot = await load_financial_snapshot(
            connection,
            case_id=case_id,
            owner_user_id=owner_user_id,
        )
        margin = (
            "не вычисляется: нет суммы тендера или есть покупки без цены"
            if snapshot.provisional_margin is None
            else f"{snapshot.provisional_margin:.2f} {snapshot.currency}"
        )
        return CaseQueryAnswer(
            case_id,
            (
                f"По заявке {label}: подтверждённые закупки — "
                f"{snapshot.product_cost_known:.2f} {snapshot.currency}; доставки — "
                f"{snapshot.delivery_cost_known:.2f} {snapshot.currency}; "
                f"предварительный результат — {margin}. Это не финальная бухгалтерская отчётность."
            ),
            as_of,
        )

    counts = await _item_counts(connection, case_id)
    return CaseQueryAnswer(
        case_id,
        (
            f"Заявка {label}: {_status_text(case['status'])}. "
            f"Город: {case['city'] or 'ещё не указан'}. "
            f"Всего позиций: {counts['total']}; осталось: {counts['open']}; "
            f"закрыто: {counts['closed']}."
        ),
        as_of,
    )


async def _resolve_case(
    connection: asyncpg.Connection,
    owner_user_id: UUID,
    reference: str | None,
) -> dict[str, Any] | CaseQueryAnswer:
    base = """
        SELECT procurement_case.id,procurement_case.title,procurement_case.city,
               procurement_case.status,procurement_case.customer_id,
               procurement_case.updated_at,customer.display_name AS customer_name
        FROM procurement_cases AS procurement_case
        LEFT JOIN customers AS customer ON customer.id=procurement_case.customer_id
        WHERE procurement_case.owner_user_id=$1
    """
    if reference is not None:
        rows = await connection.fetch(
            base + " AND procurement_case.id::text LIKE $2 ORDER BY updated_at DESC LIMIT 2",
            owner_user_id,
            reference + "%",
        )
    else:
        rows = await connection.fetch(
            base
            + " AND procurement_case.status NOT IN ('closed','cancelled') "
            "ORDER BY updated_at DESC,id DESC LIMIT 6",
            owner_user_id,
        )
    if len(rows) == 1:
        return dict(rows[0])
    if not rows:
        return CaseQueryAnswer(
            None,
            "У вас пока нет сохранённой заявки. Пришлите PDF, DOCX, голосовое или список товаров.",
            None,
            True,
        )
    choices = "; ".join(
        f"«{row['title'] or 'Без названия'}» — {row['city'] or 'город не указан'}"
        for row in rows[:5]
    )
    return CaseQueryAnswer(
        None,
        f"У вас несколько активных заявок. Уточните название или город: {choices}.",
        None,
        True,
    )


def _status_text(status: str) -> str:
    return {
        "draft": "собираю данные",
        "needs_clarification": "жду вашего уточнения",
        "ready": "готова к поиску",
        "researching": "ищу поставщиков",
        "contacting": "общаюсь с поставщиками",
        "evaluating": "сравниваю предложения",
        "report_ready": "отчёт готов",
        "selected": "лучшие варианты выбраны",
        "closed": "закрыта",
        "cancelled": "отменена",
    }.get(status, "в работе")


async def _item_counts(connection: asyncpg.Connection, case_id: UUID) -> dict[str, int]:
    row = await connection.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE status='closed') AS closed,
               count(*) FILTER (WHERE status<>'closed') AS open
        FROM request_items WHERE case_id=$1
        """,
        case_id,
    )
    return {name: int(row[name] or 0) for name in ("total", "closed", "open")}
