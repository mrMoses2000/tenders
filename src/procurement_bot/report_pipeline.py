from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.evaluation import (
    OfferAssessment,
    RequirementCheck,
    RequirementStatus,
    TechnicalStatus,
    evaluate_and_rank,
)
from procurement_bot.queue import enqueue_outbox, stable_key
from procurement_bot.report_artifacts import persist_report_artifact
from procurement_bot.reporting import (
    ProcurementReport,
    ReportEvidence,
    SupplierReportCard,
    render_report_html,
)

_REPORT_EVENT = "report_built"
_TERMINAL_RESEARCH_STATUSES = ("succeeded", "partial")


class ReportBuildRejected(RuntimeError):
    """The requested report cannot be derived from the durable workflow state."""


@dataclass(frozen=True, slots=True)
class ReportBuildResult:
    case_id: UUID
    research_run_id: UUID | None
    artifact_id: UUID
    version: int
    storage_path: str
    sha256: str
    byte_size: int
    suppliers_found: int
    suppliers_responded: int
    exact_matches: int
    newly_built: bool


async def build_case_report(
    connection: asyncpg.Connection,
    *,
    results_root: Path,
    case_id: UUID,
    research_run_id: UUID | None = None,
) -> ReportBuildResult:
    """Build one conservative HTML report and publish its metadata atomically.

    The generated HTML is a private, content-addressed file. PostgreSQL remains
    authoritative for the artifact identity and workflow transition. No provider
    or network operation is performed by this service.
    """

    if not isinstance(case_id, UUID):
        raise TypeError("case_id must be a UUID")
    if research_run_id is not None and not isinstance(research_run_id, UUID):
        raise TypeError("research_run_id must be a UUID or None")

    async with connection.transaction():
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
            f"report-build:{case_id}",
        )
        case = await connection.fetchrow(
            """
            SELECT c.id,c.status,c.title,c.city,c.search_area_text,c.created_at,
                c.updated_at,u.telegram_id,u.active AS owner_active
            FROM procurement_cases AS c
            JOIN users AS u ON u.id=c.owner_user_id
            WHERE c.id=$1
            FOR UPDATE OF c
            """,
            case_id,
        )
        if case is None:
            raise ReportBuildRejected("procurement case does not exist")
        if not case["owner_active"] or case["telegram_id"] is None:
            raise ReportBuildRejected("case owner is unavailable")

        run = await _load_research_run(
            connection,
            case_id=case_id,
            research_run_id=research_run_id,
        )
        selected_run_id = run["id"] if run is not None else None
        event_key = stable_key("workflow.report-built", case_id, selected_run_id or "no-run")
        existing = await _load_existing_result(
            connection,
            case_id=case_id,
            research_run_id=selected_run_id,
            event_key=event_key,
        )
        if existing is not None:
            return existing
        if case["status"] != "evaluating":
            raise ReportBuildRejected("procurement case is not ready for reporting")

        items = await connection.fetch(
            """
            SELECT id,line_number,name,specification_text,quantity,unit,
                analogs_allowed,status
            FROM request_items
            WHERE case_id=$1
            ORDER BY line_number,id
            """,
            case_id,
        )
        if not items:
            raise ReportBuildRejected("procurement case has no current request items")
        item_ids = [item["id"] for item in items]
        requirements = await connection.fetch(
            """
            SELECT request_item_id,requirement_key,is_hard,source_pointer
            FROM item_requirements
            WHERE request_item_id=ANY($1::uuid[])
            ORDER BY request_item_id,requirement_key,source_pointer,id
            """,
            item_ids,
        )
        offers = await connection.fetch(
            """
            SELECT
                o.id AS offer_id,o.status AS offer_status,o.supplier_product_name,
                o.model_sku,o.supplier_id,s.display_name AS supplier_name,
                ri.id AS request_item_id,ri.line_number,ri.name AS item_name,
                ri.specification_text,ri.quantity,ri.unit,ri.analogs_allowed,
                location.city AS location_city,location.address_text,
                location.district,location.market_name,location.pavilion,
                observation.price_amount,observation.currency,observation.price_unit,
                observation.availability_status,observation.vat_status,
                observation.delivery_status,observation.observed_at,
                evidence.title AS evidence_title,
                evidence.source_locator,evidence.source_url
            FROM offers AS o
            JOIN request_items AS ri ON ri.id=o.request_item_id
            JOIN suppliers AS s ON s.id=o.supplier_id
            LEFT JOIN supplier_locations AS location ON location.id=o.supplier_location_id
            LEFT JOIN LATERAL (
                SELECT latest.*
                FROM offer_observations AS latest
                WHERE latest.offer_id=o.id
                ORDER BY latest.observed_at DESC,latest.created_at DESC,latest.id DESC
                LIMIT 1
            ) AS observation ON TRUE
            LEFT JOIN source_evidence AS evidence
                ON evidence.id=observation.source_evidence_id
            WHERE ri.case_id=$1 AND o.status<>'withdrawn'
            ORDER BY ri.line_number,s.display_name,o.created_at,o.id
            """,
            case_id,
        )
        suppliers_responded = int(
            await connection.fetchval(
                """
                SELECT count(DISTINCT conversation.supplier_id)
                FROM supplier_conversations AS conversation
                WHERE conversation.case_id=$1 AND conversation.inbound_count>0
                """,
                case_id,
            )
            or 0
        )
        contact_metrics = await connection.fetchrow(
            """
            WITH case_suppliers AS (
                SELECT DISTINCT offer.supplier_id
                FROM offers AS offer
                JOIN request_items AS item ON item.id=offer.request_item_id
                WHERE item.case_id=$1 AND offer.status<>'withdrawn'
            )
            SELECT
                (SELECT count(DISTINCT conversation.supplier_id)
                 FROM supplier_conversations AS conversation
                 WHERE conversation.case_id=$1 AND conversation.outbound_count>0)
                    AS contacted,
                (SELECT count(*) FROM case_suppliers AS supplier
                 WHERE NOT EXISTS (
                     SELECT 1 FROM supplier_contacts AS contact
                     WHERE contact.supplier_id=supplier.supplier_id
                       AND contact.active AND contact.contact_type='whatsapp'
                 )) AS whatsapp_unavailable,
                (SELECT count(*) FROM outbox_events AS event
                 WHERE event.event_type='whatsapp.send_text' AND event.status='dead'
                   AND event.payload->>'case_id'=$1::text) AS whatsapp_failed
            """,
            case_id,
        )

        generated_at = _generated_at(case, run)
        report, summary = _build_report_projection(
            case=case,
            items=items,
            requirements=requirements,
            offers=offers,
            suppliers_responded=suppliers_responded,
            contact_metrics=contact_metrics,
            generated_at=generated_at,
        )
        html = render_report_html(report)
        artifact = await persist_report_artifact(
            connection,
            results_root=results_root,
            case_id=case_id,
            research_run_id=selected_run_id,
            html=html,
            summary=summary,
            idempotency_key=stable_key(
                "report-artifact",
                case_id,
                selected_run_id or "no-run",
            ),
        )
        transitioned = await connection.execute(
            """
            UPDATE procurement_cases
            SET status='report_ready',updated_at=now()
            WHERE id=$1 AND status='evaluating'
            """,
            case_id,
        )
        if transitioned != "UPDATE 1":
            raise ReportBuildRejected("case changed before report publication")

        event_data = {
            "artifact_id": str(artifact.id),
            "research_run_id": str(selected_run_id) if selected_run_id else None,
            "version": artifact.version,
            "storage_path": artifact.storage_path,
            "sha256": artifact.sha256,
            "byte_size": artifact.byte_size,
            **summary,
        }
        event_id = await connection.fetchval(
            """
            INSERT INTO workflow_events(
                case_id,event_type,actor_type,actor_id,idempotency_key,data
            ) VALUES ($1,$2,'worker','report-pipeline',$3,$4::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            case_id,
            _REPORT_EVENT,
            event_key,
            event_data,
        )
        if event_id is None:
            raise ReportBuildRejected("report workflow event appeared during locked build")
        await enqueue_outbox(
            connection,
            event_type="telegram.send_document",
            payload={
                "chat_id": int(case["telegram_id"]),
                "storage_path": artifact.storage_path,
                "filename": "otchet-po-zayavke.html",
                "caption": _telegram_announcement(summary),
            },
            idempotency_key=stable_key("telegram.report-ready", artifact.id),
        )
        return ReportBuildResult(
            case_id=case_id,
            research_run_id=selected_run_id,
            artifact_id=artifact.id,
            version=artifact.version,
            storage_path=artifact.storage_path,
            sha256=artifact.sha256,
            byte_size=artifact.byte_size,
            suppliers_found=int(summary["suppliers_found"]),
            suppliers_responded=int(summary["suppliers_responded"]),
            exact_matches=int(summary["exact_matches"]),
            newly_built=True,
        )


async def _load_research_run(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    research_run_id: UUID | None,
) -> Mapping[str, Any] | None:
    if research_run_id is None:
        return await connection.fetchrow(
            """
            SELECT id,case_id,status,started_at,completed_at
            FROM research_runs
            WHERE case_id=$1 AND status=ANY($2::text[])
            ORDER BY completed_at DESC NULLS LAST,created_at DESC,id DESC
            LIMIT 1
            """,
            case_id,
            list(_TERMINAL_RESEARCH_STATUSES),
        )
    run = await connection.fetchrow(
        """
        SELECT id,case_id,status,started_at,completed_at
        FROM research_runs
        WHERE id=$1
        """,
        research_run_id,
    )
    if run is None:
        raise ReportBuildRejected("research run does not exist")
    if run["case_id"] != case_id:
        raise ReportBuildRejected("research run belongs to a different case")
    if run["status"] not in _TERMINAL_RESEARCH_STATUSES:
        raise ReportBuildRejected("research run is not complete enough for reporting")
    return run


async def _load_existing_result(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    research_run_id: UUID | None,
    event_key: str,
) -> ReportBuildResult | None:
    event = await connection.fetchrow(
        """
        SELECT event_type,data
        FROM workflow_events
        WHERE idempotency_key=$1
        """,
        event_key,
    )
    if event is None:
        return None
    if event["event_type"] != _REPORT_EVENT:
        raise ReportBuildRejected("report idempotency key belongs to another event")
    data = _json_object(event["data"], field="report workflow data")
    try:
        artifact_id = UUID(str(data["artifact_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise ReportBuildRejected("report workflow event has no valid artifact id") from exc
    artifact = await connection.fetchrow(
        """
        SELECT id,case_id,research_run_id,version,storage_path,sha256,byte_size,summary
        FROM report_artifacts
        WHERE id=$1
        """,
        artifact_id,
    )
    if artifact is None or artifact["case_id"] != case_id:
        raise ReportBuildRejected("report workflow artifact is absent or belongs elsewhere")
    if artifact["research_run_id"] != research_run_id:
        raise ReportBuildRejected("report workflow artifact has different provenance")
    summary = _json_object(artifact["summary"], field="report summary")
    return ReportBuildResult(
        case_id=case_id,
        research_run_id=research_run_id,
        artifact_id=artifact["id"],
        version=artifact["version"],
        storage_path=artifact["storage_path"],
        sha256=artifact["sha256"],
        byte_size=artifact["byte_size"],
        suppliers_found=int(summary.get("suppliers_found", 0)),
        suppliers_responded=int(summary.get("suppliers_responded", 0)),
        exact_matches=int(summary.get("exact_matches", 0)),
        newly_built=False,
    )


def _build_report_projection(
    *,
    case: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    requirements: Sequence[Mapping[str, Any]],
    offers: Sequence[Mapping[str, Any]],
    suppliers_responded: int,
    contact_metrics: Mapping[str, Any],
    generated_at: datetime,
) -> tuple[ProcurementReport, dict[str, Any]]:
    requirement_names: dict[UUID, list[str]] = {}
    for requirement in requirements:
        requirement_names.setdefault(requirement["request_item_id"], []).append(
            str(requirement["requirement_key"])
        )

    assessments = [
        _assessment(row, requirement_names.get(row["request_item_id"], []))
        for row in offers
    ]
    evaluated = evaluate_and_rank(assessments)
    evaluation_by_offer = {value.offer.offer_id: value for value in evaluated}
    cards: list[SupplierReportCard] = []
    for row in offers:
        value = evaluation_by_offer[str(row["offer_id"])]
        cards.append(
            SupplierReportCard(
                supplier_name=str(row["supplier_name"]),
                location=_location(row),
                product_name=(
                    str(row["supplier_product_name"]).strip()
                    or str(row["item_name"])
                ),
                technical_status=_technical_label(value.technical_status),
                availability=_availability_label(row["availability_status"]),
                price=row["price_amount"],
                currency=str(row["currency"] or "KZT"),
                rank=value.rank,
                evidence=_report_evidence(row),
                questions_remaining=_questions_remaining(
                    row,
                    value.technical_status,
                    requirement_names.get(row["request_item_id"], []),
                ),
            )
        )

    suppliers_found = len({row["supplier_id"] for row in offers})
    exact_matches = sum(
        value.technical_status == TechnicalStatus.MATCH for value in evaluated
    )
    title = str(case["title"] or "").strip() or _items_title(items)
    report = ProcurementReport(
        case_reference=str(case["id"])[:8],
        title=title[:500],
        city=str(case["city"] or "").strip() or "Город не указан",
        search_scope=(
            str(case["search_area_text"] or "").strip()
            or "Область поиска не указана"
        ),
        suppliers_found=suppliers_found,
        suppliers_responded=suppliers_responded,
        suppliers_contacted=int(contact_metrics["contacted"] or 0),
        whatsapp_unavailable=int(contact_metrics["whatsapp_unavailable"] or 0),
        whatsapp_failed=int(contact_metrics["whatsapp_failed"] or 0),
        exact_matches=exact_matches,
        cards=cards,
        generated_at=generated_at,
    )
    summary = {
        "suppliers_found": suppliers_found,
        "suppliers_responded": suppliers_responded,
        "suppliers_contacted": int(contact_metrics["contacted"] or 0),
        "whatsapp_unavailable": int(contact_metrics["whatsapp_unavailable"] or 0),
        "whatsapp_failed": int(contact_metrics["whatsapp_failed"] or 0),
        "exact_matches": exact_matches,
        "offers_total": len(cards),
        "items_total": len(items),
        "generated_at": generated_at.isoformat(),
    }
    return report, summary


def _assessment(row: Mapping[str, Any], requirement_names: Sequence[str]) -> OfferAssessment:
    status = str(row["offer_status"])
    if status == "exact":
        requirement_status = RequirementStatus.CONFIRMED_MATCH
    elif status == "mismatch":
        requirement_status = RequirementStatus.CONFIRMED_MISMATCH
    else:
        requirement_status = RequirementStatus.UNKNOWN
    label = "Соответствие технической спецификации"
    if requirement_names:
        label = f"Техническая спецификация ({len(requirement_names)} требований)"
    availability = str(row["availability_status"] or "unknown")
    availability_confirmed = (
        True
        if availability in {"in_stock", "in_stock_unquantified"}
        else False
        if availability == "out_of_stock"
        else None
    )
    evidence_level = Decimal("0")
    if row["observed_at"] is not None:
        evidence_level = Decimal("0.7")
    if row["source_url"]:
        evidence_level = Decimal("0.9")
    return OfferAssessment(
        offer_id=str(row["offer_id"]),
        supplier_id=str(row["supplier_id"]),
        supplier_name=str(row["supplier_name"]),
        product_name=(str(row["supplier_product_name"]).strip() or str(row["item_name"])),
        availability_confirmed=availability_confirmed,
        # A quoted unit/package price is not a landed total. Keeping this unknown
        # prevents a deceptively precise rank until MOQ, VAT and logistics are known.
        total_cost=None,
        currency=str(row["currency"]) if row["currency"] else None,
        requirement_checks=[
            RequirementCheck(
                requirement=label,
                hard=True,
                status=requirement_status,
                evidence=(
                    f"Persisted offer status: {status}"
                    if status in {"exact", "mismatch"}
                    else None
                ),
            )
        ],
        evidence_quality=float(evidence_level),
    )


def _questions_remaining(
    row: Mapping[str, Any],
    technical_status: TechnicalStatus,
    requirement_names: Sequence[str],
) -> list[str]:
    questions: list[str] = []
    if technical_status == TechnicalStatus.NEEDS_CONFIRMATION:
        detail = ", ".join(requirement_names[:5])
        suffix = f" ({detail})" if detail else ""
        questions.append(f"Подтвердить соответствие технической спецификации{suffix}.")
    elif technical_status == TechnicalStatus.MISMATCH and row["analogs_allowed"] is not False:
        questions.append("Уточнить, допустим ли предложенный аналог с учётом несоответствия.")
    availability = str(row["availability_status"] or "unknown")
    if availability not in {"in_stock", "in_stock_unquantified", "out_of_stock"}:
        questions.append("Подтвердить фактическое наличие и срок готовности.")
    if row["price_amount"] is None:
        questions.append("Уточнить цену после подтверждения характеристик.")
    elif not row["price_unit"]:
        questions.append("Уточнить, за какую единицу указана цена.")
    if str(row["vat_status"] or "unknown") == "unknown":
        questions.append("Уточнить, включён ли НДС.")
    if str(row["delivery_status"] or "unknown") == "unknown":
        questions.append("Уточнить условия и стоимость доставки или самовывоза.")
    return questions


def _report_evidence(row: Mapping[str, Any]) -> list[ReportEvidence]:
    if row["source_locator"] is None:
        return []
    label = str(row["evidence_title"] or "").strip() or str(row["source_locator"])
    return [
        ReportEvidence(
            label=label[:300],
            url=str(row["source_url"]) if row["source_url"] else None,
        )
    ]


def _location(row: Mapping[str, Any]) -> str | None:
    parts: list[str] = []
    for value in (
        row["location_city"],
        row["market_name"],
        row["pavilion"],
        row["district"],
        row["address_text"],
    ):
        normalized = str(value or "").strip()
        if normalized and normalized not in parts:
            parts.append(normalized)
    return ", ".join(parts)[:1000] or None


def _availability_label(value: object) -> str:
    return {
        "in_stock": "в наличии (количество подтверждено)",
        "in_stock_unquantified": "в наличии (количество не подтверждено)",
        "preorder": "под заказ",
        "out_of_stock": "нет в наличии",
    }.get(str(value or "unknown"), "наличие не подтверждено")


def _technical_label(value: TechnicalStatus) -> str:
    return {
        TechnicalStatus.MATCH: "соответствие подтверждено",
        TechnicalStatus.MISMATCH: "подтверждено несоответствие",
        TechnicalStatus.NEEDS_CONFIRMATION: "требуется подтверждение",
    }[value]


def _items_title(items: Sequence[Mapping[str, Any]]) -> str:
    names = [str(item["name"]).strip() for item in items if str(item["name"]).strip()]
    return ", ".join(names[:3]) or "Отчёт по закупке"


def _generated_at(
    case: Mapping[str, Any],
    run: Mapping[str, Any] | None,
) -> datetime:
    candidates = (
        run["completed_at"] if run is not None else None,
        run["started_at"] if run is not None else None,
        case["updated_at"],
        case["created_at"],
    )
    value = next((candidate for candidate in candidates if candidate is not None), None)
    if not isinstance(value, datetime):
        raise ReportBuildRejected("case has no valid report timestamp")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _json_object(value: object, *, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ReportBuildRejected(f"{field} is not valid JSON") from exc
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ReportBuildRejected(f"{field} must be a JSON object")
    return dict(value)


def _telegram_announcement(summary: Mapping[str, Any]) -> str:
    return (
        "Отчёт готов.\n"
        f"Поставщиков: {summary['suppliers_found']}; ответили: "
        f"{summary['suppliers_responded']}; подтверждённых соответствий: "
        f"{summary['exact_matches']}. Без подтверждённого WhatsApp: "
        f"{summary['whatsapp_unavailable']}; ошибок отправки: "
        f"{summary['whatsapp_failed']}."
    )
