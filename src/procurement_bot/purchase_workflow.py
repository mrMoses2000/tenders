from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID, uuid4

import asyncpg


@dataclass(frozen=True, slots=True)
class ParsedCloseRequest:
    line_number: int
    case_reference: str | None


@dataclass(frozen=True, slots=True)
class PurchaseEvidenceRequestResult:
    request_id: UUID | None
    case_id: UUID | None
    request_item_id: UUID | None
    text: str
    created: bool


_CLOSE_PATTERNS = (
    re.compile(r"\bзакрыл(?:а|и)?\s+позици(?:ю|и)\s*№?\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bпозици(?:я|ю)\s*№?\s*(\d+)\s+(?:закрыта|закрыл)", re.IGNORECASE),
    re.compile(r"\b(\d+)\s*(?:-?ші)?\s*позици(?:я|яны)\s+(?:жаптым|жабылды)\b", re.IGNORECASE),
)
_REFERENCE = re.compile(r"(?<![0-9a-f])#([0-9a-f]{8})(?![0-9a-f])", re.IGNORECASE)


def parse_close_request(text: str) -> ParsedCloseRequest | None:
    normalized = " ".join(text.split())
    if re.search(r"\b(?:не\s+закрыт|жабылмаған)", normalized, re.IGNORECASE):
        return None
    for pattern in _CLOSE_PATTERNS:
        match = pattern.search(normalized)
        if match is not None:
            line = int(match.group(1))
            if line <= 0:
                return None
            reference = _REFERENCE.search(normalized)
            return ParsedCloseRequest(
                line_number=line,
                case_reference=reference.group(1).lower() if reference else None,
            )
    return None


async def request_purchase_evidence(
    connection: asyncpg.Connection,
    *,
    owner_user_id: UUID,
    source_message_id: UUID,
    statement: str,
    request: ParsedCloseRequest,
) -> PurchaseEvidenceRequestResult:
    """Require a receipt before an asserted closure can affect item status."""

    async with connection.transaction():
        existing = await connection.fetchrow(
            """
            SELECT evidence.id,evidence.case_id,evidence.request_item_id,evidence.status,
                   item.line_number,item.name
            FROM purchase_evidence_requests AS evidence
            JOIN request_items AS item ON item.id=evidence.request_item_id
            WHERE evidence.requested_in_message_id=$1
            """,
            source_message_id,
        )
        if existing is not None:
            return PurchaseEvidenceRequestResult(
                existing["id"],
                existing["case_id"],
                existing["request_item_id"],
                _request_text(existing["line_number"], existing["name"]),
                False,
            )
        case_rows = await _case_rows(connection, owner_user_id, request.case_reference)
        if len(case_rows) != 1:
            choices = ", ".join(
                f"«{row['title'] or 'Без названия'}»"
                for row in case_rows[:5]
            )
            text = (
                "Не нашёл активную заявку. Сначала пришлите заявку или список товаров."
                if not case_rows
                else f"Уточните, к какой заявке относится покупка: {choices}."
            )
            return PurchaseEvidenceRequestResult(None, None, None, text, False)
        case = case_rows[0]
        item = await connection.fetchrow(
            "SELECT id,line_number,name,status FROM request_items "
            "WHERE case_id=$1 AND line_number=$2 FOR UPDATE",
            case["id"],
            request.line_number,
        )
        if item is None:
            return PurchaseEvidenceRequestResult(
                None,
                case["id"],
                None,
                f"В этой заявке нет позиции №{request.line_number}.",
                False,
            )
        if item["status"] == "closed":
            return PurchaseEvidenceRequestResult(
                None,
                case["id"],
                item["id"],
                f"Позиция №{request.line_number} уже закрыта в базе.",
                False,
            )
        waiting = await connection.fetchrow(
            """
            SELECT evidence.id,item.line_number,item.name
            FROM purchase_evidence_requests AS evidence
            JOIN request_items AS item ON item.id=evidence.request_item_id
            WHERE evidence.owner_user_id=$1 AND evidence.status='awaiting_document'
            FOR UPDATE OF evidence
            """,
            owner_user_id,
        )
        if waiting is not None:
            return PurchaseEvidenceRequestResult(
                waiting["id"],
                case["id"],
                item["id"],
                (
                    f"Сначала пришлите чек/накладную для позиции №{waiting['line_number']} "
                    f"«{waiting['name']}» либо отмените это ожидание."
                ),
                False,
            )
        request_id = uuid4()
        await connection.execute(
            """
            INSERT INTO purchase_evidence_requests(
                id,case_id,request_item_id,owner_user_id,requested_in_message_id,
                raw_statement
            ) VALUES ($1,$2,$3,$4,$5,$6)
            """,
            request_id,
            case["id"],
            item["id"],
            owner_user_id,
            source_message_id,
            statement.strip(),
        )
        return PurchaseEvidenceRequestResult(
            request_id,
            case["id"],
            item["id"],
            _request_text(item["line_number"], item["name"]),
            True,
        )


async def bind_next_attachment_as_purchase_evidence(
    connection: asyncpg.Connection,
    *,
    owner_user_id: UUID,
    attachment_id: UUID,
) -> UUID | None:
    """Bind only the owner's single pending evidence request to the next attachment."""

    row = await connection.fetchrow(
        """
        UPDATE purchase_evidence_requests
        SET incoming_attachment_id=$2,status='document_received',updated_at=now()
        WHERE id=(
            SELECT id FROM purchase_evidence_requests
            WHERE owner_user_id=$1 AND status='awaiting_document'
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """,
        owner_user_id,
        attachment_id,
    )
    return row["id"] if row is not None else None


async def mark_purchase_artifact_ready(
    connection: asyncpg.Connection,
    *,
    attachment_id: UUID,
    source_artifact_id: UUID,
) -> dict[str, object] | None:
    row = await connection.fetchrow(
        """
        UPDATE purchase_evidence_requests AS evidence
        SET source_artifact_id=$2,status='awaiting_details',updated_at=now()
        FROM request_items AS item,procurement_cases AS procurement_case,users AS owner
        WHERE evidence.incoming_attachment_id=$1
          AND evidence.status='document_received'
          AND item.id=evidence.request_item_id
          AND procurement_case.id=evidence.case_id
          AND owner.id=evidence.owner_user_id
        RETURNING evidence.id,evidence.case_id,evidence.request_item_id,
                  item.line_number,item.name,owner.telegram_id
        """,
        attachment_id,
        source_artifact_id,
    )
    return dict(row) if row is not None else None


async def _case_rows(
    connection: asyncpg.Connection,
    owner_user_id: UUID,
    reference: str | None,
) -> list[asyncpg.Record]:
    if reference is not None:
        return await connection.fetch(
            "SELECT id,title FROM procurement_cases WHERE owner_user_id=$1 "
            "AND id::text LIKE $2 ORDER BY updated_at DESC LIMIT 2",
            owner_user_id,
            reference + "%",
        )
    return await connection.fetch(
        "SELECT id,title FROM procurement_cases WHERE owner_user_id=$1 "
        "AND status NOT IN ('closed','cancelled') ORDER BY updated_at DESC,id DESC LIMIT 6",
        owner_user_id,
    )


def _request_text(line_number: int, name: str) -> str:
    return (
        f"Чтобы закрыть позицию №{line_number} «{name}», пришлите чек или "
        "накладную следующим сообщением. Один документ может подтверждать "
        "несколько позиций."
    )
