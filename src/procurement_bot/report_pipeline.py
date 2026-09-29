from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from procurement_bot.offer_comparison import rank_goods_offers
from procurement_bot.offer_technical import load_case_technical_reviews, technical_states
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
    refresh_key: str | None = None,
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
    if refresh_key is not None and (not refresh_key.strip() or len(refresh_key) > 200):
        raise ValueError("refresh_key must be a short nonempty value")

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
        event_key = stable_key(
            "workflow.report-built", case_id, selected_run_id or "no-run",
            *((refresh_key,) if refresh_key else ()),
        )
        existing = await _load_existing_result(
            connection,
            case_id=case_id,
            research_run_id=selected_run_id,
            event_key=event_key,
        )
        if existing is not None:
            return existing
        allowed_statuses = (
            {"evaluating", "contacting", "report_ready", "selected"}
            if refresh_key else {"evaluating"}
        )
        if case["status"] not in allowed_statuses:
            raise ReportBuildRejected("procurement case is not ready for reporting")

        items = await connection.fetch(
            """
            SELECT i.id,i.line_number,i.name,i.specification_text,i.quantity,i.unit,
                i.analogs_allowed,i.status,f.tender_unit_price_private,
                COALESCE(f.currency,'KZT') AS tender_currency
            FROM request_items i
            LEFT JOIN request_item_financials f ON f.request_item_id=i.id
            WHERE i.case_id=$1 ORDER BY i.line_number,i.id
            """,
            case_id,
        )
        if not items:
            raise ReportBuildRejected("procurement case has no current request items")
        requirements, technical_reviews = await load_case_technical_reviews(
            connection, case_id
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
                priced.price_amount,priced.currency,
                COALESCE(terms.price_unit,priced.price_unit) AS price_unit,
                COALESCE(terms.pack_qty,priced.pack_qty) AS pack_qty,
                COALESCE(terms.moq,priced.moq) AS moq,
                COALESCE(terms.order_multiple,priced.order_multiple) AS order_multiple,
                stock.available_qty,
                COALESCE(stock.availability_status,'unknown') AS availability_status,
                stock.source_type AS stock_source_type,
                priced.source_type,
                contact.normalized_value AS supplier_phone,
                contact.is_whatsapp_confirmed,
                COALESCE(priced.vat_status,latest.vat_status) AS vat_status,
                COALESCE(priced.delivery_status,latest.delivery_status) AS delivery_status,
                COALESCE(priced.observed_at,latest.observed_at) AS observed_at,
                evidence.title AS evidence_title,evidence.source_locator,evidence.source_url
            FROM offers AS o
            JOIN request_items AS ri ON ri.id=o.request_item_id
            JOIN suppliers AS s ON s.id=o.supplier_id
            LEFT JOIN supplier_locations AS location ON location.id=o.supplier_location_id
            LEFT JOIN LATERAL (
                SELECT contact.normalized_value,
                       CASE WHEN (
                           EXISTS (
                             SELECT 1 FROM research_queries rq
                             JOIN research_runs rr ON rr.id=rq.research_run_id
                             WHERE rq.id=contact_evidence.research_query_id
                               AND rr.case_id=ri.case_id
                           )
                           OR EXISTS (
                             SELECT 1 FROM offer_observations own_observation
                             JOIN offers own_offer ON own_offer.id=own_observation.offer_id
                             JOIN request_items own_item
                               ON own_item.id=own_offer.request_item_id
                             WHERE own_observation.source_evidence_id=contact_evidence.id
                               AND own_item.case_id=ri.case_id
                           )
                           OR EXISTS (
                             SELECT 1 FROM workflow_events enrichment
                             WHERE enrichment.case_id=ri.case_id
                               AND enrichment.event_type='supplier_contact_enrichment_result'
                               AND enrichment.data->>'supplier_id'=contact.supplier_id::text
                               AND (
                                 (
                                   enrichment.data ? 'contact_evidence_ids'
                                   AND enrichment.data->'contact_evidence_ids'
                                       @> jsonb_build_array(contact_evidence.id::text)
                                 )
                                 OR (
                                   NOT (enrichment.data ? 'contact_evidence_ids')
                                   AND enrichment.data->>'outcome'='contact_found'
                                   AND enrichment.data->>'firm_id'=contact_evidence.external_id
                                   AND contact_evidence.source_type='2gis'
                                   AND (
                                     contact_evidence.source_url=enrichment.data->>'card_url'
                                     OR contact_evidence.source_url=
                                          (enrichment.data->>'card_url') || '/'
                                   )
                                 )
                               )
                           )
                       ) THEN contact.is_whatsapp_confirmed ELSE FALSE END
                         AS is_whatsapp_confirmed
                FROM supplier_contacts contact
                JOIN source_evidence contact_evidence
                  ON contact_evidence.id=contact.source_evidence_id
                WHERE contact.supplier_id=o.supplier_id AND contact.active
                  AND contact.contact_type IN ('phone','whatsapp')
                  AND (
                    EXISTS (
                      SELECT 1 FROM research_queries rq
                      JOIN research_runs rr ON rr.id=rq.research_run_id
                      WHERE rq.id=contact_evidence.research_query_id
                        AND rr.case_id=ri.case_id
                    )
                    OR EXISTS (
                      SELECT 1 FROM offer_observations own_observation
                      JOIN offers own_offer ON own_offer.id=own_observation.offer_id
                      JOIN request_items own_item ON own_item.id=own_offer.request_item_id
                      WHERE own_observation.source_evidence_id=contact_evidence.id
                        AND own_item.case_id=ri.case_id
                    )
                    OR EXISTS (
                      SELECT 1 FROM workflow_events enrichment
                      WHERE enrichment.case_id=ri.case_id
                        AND enrichment.event_type='supplier_contact_enrichment_result'
                        AND enrichment.data->>'supplier_id'=contact.supplier_id::text
                        AND (
                          (
                            enrichment.data ? 'contact_evidence_ids'
                            AND enrichment.data->'contact_evidence_ids'
                                @> jsonb_build_array(contact_evidence.id::text)
                          )
                          OR (
                            NOT (enrichment.data ? 'contact_evidence_ids')
                            AND enrichment.data->>'outcome'='contact_found'
                            AND enrichment.data->>'firm_id'=contact_evidence.external_id
                            AND contact_evidence.source_type='2gis'
                            AND (
                              contact_evidence.source_url=enrichment.data->>'card_url'
                              OR contact_evidence.source_url=
                                   (enrichment.data->>'card_url') || '/'
                            )
                          )
                        )
                    )
                  )
                ORDER BY contact.is_whatsapp_confirmed DESC,
                         contact.first_seen_at DESC,contact.id DESC LIMIT 1
            ) contact ON TRUE
            LEFT JOIN LATERAL (
                SELECT ob.* FROM offer_observations ob WHERE ob.offer_id=o.id
                ORDER BY ob.observed_at DESC,ob.created_at DESC,ob.id DESC LIMIT 1
            ) latest ON TRUE
            LEFT JOIN LATERAL (
                SELECT ob.* FROM offer_observations ob
                WHERE ob.offer_id=o.id AND ob.price_amount IS NOT NULL
                ORDER BY ob.observed_at DESC,ob.created_at DESC,ob.id DESC LIMIT 1
            ) priced ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    (array_agg(ob.price_unit ORDER BY ob.observed_at DESC,
                        ob.created_at DESC,ob.id DESC)
                        FILTER (WHERE ob.price_unit IS NOT NULL))[1] AS price_unit,
                    (array_agg(ob.pack_qty ORDER BY ob.observed_at DESC,
                        ob.created_at DESC,ob.id DESC)
                        FILTER (WHERE ob.pack_qty IS NOT NULL))[1] AS pack_qty,
                    (array_agg(ob.moq ORDER BY ob.observed_at DESC,
                        ob.created_at DESC,ob.id DESC)
                        FILTER (WHERE ob.moq IS NOT NULL))[1] AS moq,
                    (array_agg(ob.order_multiple ORDER BY ob.observed_at DESC,
                        ob.created_at DESC,ob.id DESC)
                        FILTER (WHERE ob.order_multiple IS NOT NULL))[1] AS order_multiple
                FROM offer_observations ob
                WHERE ob.offer_id=o.id AND ob.observed_at>=priced.observed_at
                  AND ob.source_type IN ('supplier_message','manual')
            ) terms ON TRUE
            LEFT JOIN LATERAL (
                SELECT ob.available_qty,ob.availability_status,ob.source_type
                FROM offer_observations ob
                WHERE ob.offer_id=o.id AND
                  (ob.availability_status<>'unknown' OR ob.available_qty IS NOT NULL)
                ORDER BY ob.observed_at DESC,ob.created_at DESC,ob.id DESC LIMIT 1
            ) stock ON TRUE
            LEFT JOIN source_evidence AS evidence
                ON evidence.id=COALESCE(priced.source_evidence_id,latest.source_evidence_id)
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
                     JOIN source_evidence contact_evidence
                       ON contact_evidence.id=contact.source_evidence_id
                     WHERE contact.supplier_id=supplier.supplier_id
                       AND contact.active AND contact.contact_type='whatsapp'
                       AND (
                         EXISTS (
                           SELECT 1 FROM research_queries rq
                           JOIN research_runs rr ON rr.id=rq.research_run_id
                           WHERE rq.id=contact_evidence.research_query_id
                             AND rr.case_id=$1
                         )
                         OR EXISTS (
                           SELECT 1 FROM offer_observations own_observation
                           JOIN offers own_offer ON own_offer.id=own_observation.offer_id
                           JOIN request_items own_item
                             ON own_item.id=own_offer.request_item_id
                           WHERE own_observation.source_evidence_id=contact_evidence.id
                             AND own_item.case_id=$1
                         )
                         OR EXISTS (
                           SELECT 1 FROM workflow_events enrichment
                           WHERE enrichment.case_id=$1
                             AND enrichment.event_type='supplier_contact_enrichment_result'
                             AND enrichment.data->>'supplier_id'=contact.supplier_id::text
                             AND (
                               (
                                 enrichment.data ? 'contact_evidence_ids'
                                 AND enrichment.data->'contact_evidence_ids'
                                     @> jsonb_build_array(contact_evidence.id::text)
                               )
                               OR (
                                 NOT (enrichment.data ? 'contact_evidence_ids')
                                 AND enrichment.data->>'outcome'='contact_found'
                                 AND enrichment.data->>'firm_id'=contact_evidence.external_id
                                 AND contact_evidence.source_type='2gis'
                                 AND (
                                   contact_evidence.source_url=enrichment.data->>'card_url'
                                   OR contact_evidence.source_url=
                                        (enrichment.data->>'card_url') || '/'
                                 )
                               )
                             )
                         )
                       )
                 )) AS whatsapp_unavailable,
                (SELECT count(*) FROM outbox_events AS event
                 WHERE event.event_type='whatsapp.send_text' AND event.status='dead'
                   AND event.payload->>'case_id'=$1::text) AS whatsapp_failed
            """,
            case_id,
        )

        generated_at = datetime.now(UTC) if refresh_key else _generated_at(case, run)
        report, summary = _build_report_projection(
            case=case,
            items=items,
            requirements=requirements,
            technical_reviews=technical_reviews,
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
                *((refresh_key,) if refresh_key else ()),
            ),
        )
        if case["status"] == "evaluating":
            transitioned = await connection.execute(
                """UPDATE procurement_cases SET status='report_ready',updated_at=now()
                   WHERE id=$1 AND status='evaluating'""",
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
        for item in items:
            ranked_cards = sorted(
                (card for card in report.cards
                 if card.item_number == item["line_number"] and card.rank is not None),
                key=lambda card: card.rank or 10**9,
            )[:5]
            if not ranked_cards:
                continue
            lines = [
                f"Позиция {item['line_number']}: {item['name']}",
                "Лучшие проверенные цены товара (без доставки и расчёта НДС):",
            ]
            for card in ranked_cards:
                difference = (
                    f"; разница с тендером {card.tender_difference:,.2f} KZT"
                    if card.tender_difference is not None else ""
                )
                lines.append(
                    f"{card.rank}. {card.supplier_name}: {card.goods_total:,.2f} "
                    f"{card.currency}{difference}"
                )
            lines.append("Карточки поставщиков и доказательства — в приложенном отчёте.")
            await enqueue_outbox(
                connection,
                event_type="telegram.send_message",
                payload={"chat_id": int(case["telegram_id"]),
                         "text": "\n".join(lines)[:3500]},
                idempotency_key=stable_key(
                    "telegram.report-top-five", artifact.id, item["id"]
                ),
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
    technical_reviews: Sequence[Mapping[str, Any]],
    offers: Sequence[Mapping[str, Any]],
    suppliers_responded: int,
    contact_metrics: Mapping[str, Any],
    generated_at: datetime,
) -> tuple[ProcurementReport, dict[str, Any]]:
    technical_by_offer = technical_states(offers, requirements, technical_reviews)
    item_by_id = {row["id"]: row for row in items}
    comparison_rows = [
        {
            **dict(row),
            "tender_unit_price_private": item_by_id[row["request_item_id"]][
                "tender_unit_price_private"
            ],
            "tender_currency": item_by_id[row["request_item_id"]]["tender_currency"],
            "technical_state": technical_by_offer[str(row["offer_id"])],
        }
        for row in offers
    ]
    comparisons = {
        value.offer_id: value for value in rank_goods_offers(comparison_rows)
    }
    cards: list[SupplierReportCard] = []
    for row in offers:
        value = comparisons[str(row["offer_id"])]
        cards.append(
            SupplierReportCard(
                item_number=int(row["line_number"]),
                item_name=str(row["item_name"]),
                supplier_name=str(row["supplier_name"]),
                supplier_phone=row["supplier_phone"],
                whatsapp_confirmed=bool(row["is_whatsapp_confirmed"]),
                location=_location(row),
                product_name=(
                    str(row["supplier_product_name"]).strip()
                    or str(row["item_name"])
                ),
                technical_status=_technical_label(value.technical_state),
                availability=_availability_label(
                    row["availability_status"], row["stock_source_type"]
                ),
                price=row["price_amount"],
                currency=str(row["currency"] or "KZT"),
                price_unit=row["price_unit"],
                required_quantity=row["quantity"],
                available_quantity=row["available_qty"],
                purchased_quantity=value.purchased_quantity,
                goods_total=value.goods_total,
                tender_total=value.tender_total,
                tender_difference=value.tender_difference,
                price_state={
                    "supplier_confirmed": "цена из ответа поставщика",
                    "operator_recorded": "цена внесена оператором",
                    "published": "публичная цена",
                }.get(value.price_state, "цена не получена"),
                rank=value.rank if value.rank is not None and value.rank <= 5 else None,
                evidence=_report_evidence(row),
                questions_remaining=_questions_remaining(value.blockers),
            )
        )

    suppliers_found = len({row["supplier_id"] for row in offers})
    exact_matches = sum(state == "verified" for state in technical_by_offer.values())
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


_BLOCKER_QUESTIONS = {
    "technical_not_verified": "Проверить ТЗ и сохранить результат по этому товару.",
    "price_not_confirmed": "Уточнить действующую цену у поставщика.",
    "required_quantity_unknown": "Указать требуемое количество.",
    "price_unknown": "Получить цену товара с валютой.",
    "currency_not_comparable": "Уточнить цену в валюте тендера.",
    "stock_quantity_unknown": "Подтвердить доступное количество у поставщика.",
    "stock_not_confirmed": "Подтвердить рекламируемый остаток у поставщика.",
    "out_of_stock": "Товара сейчас нет в наличии.",
    "pack_quantity_unknown": "Уточнить количество единиц в упаковке.",
    "price_unit_unknown_or_mismatch": "Уточнить единицу цены и её соответствие позиции.",
    "order_multiple_unknown": "Уточнить кратность заказа.",
    "insufficient_stock": "Уточнить, когда будет доступна вся нужная партия.",
    "invalid_moq": "Исправить минимальную партию.",
    "invalid_order_multiple": "Исправить кратность заказа.",
}


def _questions_remaining(blockers: Sequence[str]) -> list[str]:
    return [_BLOCKER_QUESTIONS.get(code, code) for code in blockers]


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


def _availability_label(value: object, source_type: object) -> str:
    if str(value or "unknown") == "unknown":
        return "наличие не подтверждено"
    if source_type not in {"supplier_message", "manual"}:
        return "наличие указано в источнике, поставщиком не подтверждено"
    return {
        "in_stock": "в наличии (количество подтверждено)",
        "in_stock_unquantified": "в наличии (количество не подтверждено)",
        "preorder": "под заказ",
        "out_of_stock": "нет в наличии",
    }.get(str(value or "unknown"), "наличие не подтверждено")


def _technical_label(value: str) -> str:
    return {
        "verified": "ТЗ проверено оператором",
        "mismatch": "несоответствие ТЗ",
        "needs_confirmation": "ТЗ не проверено",
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
