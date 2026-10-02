"""Telegram Mini App backed by the procurement PostgreSQL database."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlparse
from uuid import UUID

import asyncpg
from aiohttp import web

from procurement_bot.accounting import (
    AccountingConflict,
    PurchaseLine,
    load_financial_snapshot,
    record_case_expense,
    record_customer_acceptance,
    record_purchase,
)
from procurement_bot.case_closure import (
    CaseClosureConflict,
    change_case_closure,
    ensure_case_editable,
    load_case_closure_readiness,
)
from procurement_bot.config import Settings
from procurement_bot.delivery_settlement import (
    DeliverySettlementConflict,
    record_delivery_payment,
    record_delivery_terms,
)
from procurement_bot.delivery_tracking import (
    DeliveryConflict,
    DeliveryLine,
    create_delivery_run,
    transition_delivery_run,
)
from procurement_bot.imported_research import (
    ImportedResearchConflict,
    start_imported_item_research,
)
from procurement_bot.item_execution import ItemExecutionConflict, record_item_execution_event
from procurement_bot.offer_comparison import rank_goods_offers
from procurement_bot.offer_technical import (
    TechnicalReviewConflict,
    enqueue_technical_assessment,
    load_case_technical_reviews,
    read_latest_technical_assessment,
    record_technical_review,
    technical_states,
)
from procurement_bot.outreach_batch import (
    OutreachBatchConflict,
    approve_initial_outreach_batch,
    load_initial_outreach_drafts,
    refresh_initial_outreach_drafts,
)
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.research_workflow import ResearchStartDenied
from procurement_bot.supplier_contact_scope import case_owned_supplier_contact_evidence
from procurement_bot.supplier_media import media_storage_path

STATIC = Path(__file__).resolve().parents[2] / "mini_app"
MAX_AGE_SECONDS = 24 * 60 * 60
CASE_STATUSES = frozenset(
    (
        "draft",
        "needs_clarification",
        "ready",
        "researching",
        "contacting",
        "evaluating",
        "report_ready",
        "selected",
        "closed",
        "cancelled",
    )
)


def validate_public_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MINI_APP_PUBLIC_URL must be an HTTPS URL without credentials")
    return value.rstrip("/") + "/"


def verify_init_data(raw: str, bot_token: str, *, now: int | None = None) -> int | None:
    """Return Telegram user ID only for a fresh, correctly signed initData payload."""
    if not raw or len(raw) > 8192:
        return None
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        if len(pairs) != len(values) or "hash" not in values:
            return None
        supplied_hash = values.pop("hash")
        if len(supplied_hash) != 64:
            return None
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied_hash):
            return None
        auth_date = int(values["auth_date"])
        current = int(time.time()) if now is None else now
        if auth_date > current + 60 or current - auth_date > MAX_AGE_SECONDS:
            return None
        user = json.loads(values["user"])
        telegram_id = user["id"]
        if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
            return None
        return telegram_id
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _json(request: web.Request, payload: object, *, status: int = 200) -> web.Response:
    response = web.json_response(
        payload,
        status=status,
        dumps=lambda value: json.dumps(value, ensure_ascii=False, default=_serialize),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _serialize(value: object) -> str:
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


@web.middleware
async def security_headers(request: web.Request, handler):
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self' https://telegram.org; "
        "style-src 'self'; connect-src 'self'; img-src 'self' data: blob:; media-src blob:; "
        "base-uri 'none'; frame-ancestors https://web.telegram.org"
    )
    return response


@web.middleware
async def authenticate(request: web.Request, handler):
    if not request.path.startswith("/api/") or request.path == "/api/health":
        return await handler(request)
    settings: Settings = request.app["settings"]
    raw = request.headers.get("Authorization", "")
    if not raw.startswith("tma "):
        return _json(request, {"error": "unauthorized"}, status=401)
    telegram_id = verify_init_data(raw[4:], settings.require_telegram_token())
    if telegram_id is None:
        return _json(request, {"error": "unauthorized"}, status=401)
    allowed_ids, _ = settings.telegram_access_config()
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as connection:
        granted = telegram_id in allowed_ids or await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM telegram_access_grants "
            "WHERE telegram_id=$1 AND active=TRUE)",
            telegram_id,
        )
        if not granted:
            return _json(request, {"error": "forbidden"}, status=403)
        owner_id = await connection.fetchval(
            "SELECT id FROM users WHERE telegram_id=$1 AND active=TRUE", telegram_id
        )
    if owner_id is None:
        return _json(request, {"error": "forbidden"}, status=403)
    request["owner_id"] = owner_id
    return await handler(request)


def _page_params(request: web.Request) -> tuple[int, int]:
    try:
        limit = int(request.query.get("limit", "20"))
        offset = int(request.query.get("offset", "0"))
    except ValueError as exc:
        raise web.HTTPBadRequest(text="Invalid pagination") from exc
    if not 1 <= limit <= 50 or not 0 <= offset <= 10000:
        raise web.HTTPBadRequest(text="Invalid pagination")
    return limit, offset


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def me(request: web.Request) -> web.Response:
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT display_name, language_code FROM users WHERE id=$1", request["owner_id"]
        )
    return _json(request, dict(row))


async def summary(request: web.Request) -> web.Response:
    pool: asyncpg.Pool = request.app["pool"]
    owner = request["owner_id"]
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT count(*)::int AS cases,
                   count(*) FILTER (
                       WHERE status NOT IN ('closed','cancelled')
                   )::int AS active_cases,
                   count(*) FILTER (WHERE status='needs_clarification')::int AS needs_attention
            FROM procurement_cases WHERE owner_user_id=$1
            """,
            owner,
        )
        items = await connection.fetchval(
            """
            SELECT count(*)::int FROM request_items i
            JOIN procurement_cases c ON c.id=i.case_id
            WHERE c.owner_user_id=$1 AND c.status<>'cancelled'
            """,
            owner,
        )
    return _json(request, {**dict(row), "items": items})


async def _load_research_progress(connection: asyncpg.Connection, case_id: UUID) -> list[dict]:
    """Read item, source, discovery-card, and saved-profile progress additively."""

    rows = await connection.fetch(
        """
        SELECT grp.request_item_id,grp.status,grp.target_suppliers,
               grp.eligible_suppliers,grp.reason,grp.updated_at,
               grp.found_leads,grp.qualified_price_suppliers,
               grp.branches_succeeded,grp.branches_failed,
               grp.branches_blocked,grp.branches_total,
               (SELECT jsonb_build_object('scope',revision.scope,'city',revision.city,
                    'preferred_area_text',revision.preferred_area_text,
                    'revision',revision.revision)
                FROM research_scope_revisions revision
                WHERE revision.research_run_id=grp.research_run_id
                ORDER BY revision.revision DESC LIMIT 1) AS scope_policy,
               COALESCE(search_profile.categories,'[]'::jsonb)
                   AS search_profile_categories,
               COALESCE(search_profile.intent_count,0)::int
                   AS search_profile_intent_count,
               COALESCE((SELECT jsonb_agg(jsonb_build_object(
                   'source',q.provider,'status',cp.status,
                   'batch',cp.current_batch_no+1,
                   'error',cp.last_error_code,'reason',cp.stop_reason,
                   'failure_code',COALESCE(discovery.failure_code,NULLIF(cp.last_error_code,'')),
                   'discovered_cards',COALESCE(discovery.discovered_cards,0),
                   'pending_cards',COALESCE(discovery.pending_cards,0),
                   'extracted_cards',COALESCE(discovery.extracted_cards,0),
                   'failed_cards',COALESCE(discovery.failed_cards,0),
                   'contact_attempts',cp.candidate_receipts
               ) ORDER BY cp.lane_no)
               FROM research_branch_checkpoints cp
               JOIN research_queries q ON q.id=cp.query_id
               LEFT JOIN LATERAL (
                   WITH latest_cards AS (
                       SELECT DISTINCT ON(card.external_id)
                              card.external_id,card.status,card.failure_code,card.updated_at
                       FROM research_discovery_pages page
                       JOIN research_discovery_cards card ON card.page_id=page.id
                       WHERE page.query_id=cp.query_id
                       ORDER BY card.external_id,card.updated_at DESC,
                                page.created_at DESC,page.id DESC,card.ordinal DESC
                   )
                   SELECT count(*)::int AS discovered_cards,
                          count(*) FILTER (WHERE status='pending')::int AS pending_cards,
                          count(*) FILTER (WHERE status='extracted')::int AS extracted_cards,
                          count(*) FILTER (WHERE status='failed')::int AS failed_cards,
                          (array_agg(failure_code ORDER BY updated_at DESC)
                              FILTER (WHERE failure_code IS NOT NULL))[1] AS failure_code
                   FROM latest_cards
               ) discovery ON TRUE
               WHERE cp.group_id=grp.id),'[]'::jsonb) AS sources
        FROM research_item_groups grp
        LEFT JOIN LATERAL (
            SELECT profile.profile->'categories' AS categories,
                   count(intent.id)::int AS intent_count
            FROM (
                SELECT id,profile,profile_version
                FROM research_search_profiles
                WHERE research_run_id=grp.research_run_id
                  AND request_item_id=grp.request_item_id
                ORDER BY profile_version DESC
                LIMIT 1
            ) profile
            LEFT JOIN research_search_intents intent ON intent.profile_id=profile.id
            GROUP BY profile.id,profile.profile
        ) search_profile ON TRUE
        WHERE grp.research_run_id=(SELECT id FROM research_runs
            WHERE case_id=$1 ORDER BY created_at DESC,id DESC LIMIT 1)
        ORDER BY grp.created_at,grp.id
        """,
        case_id,
    )
    return [dict(row) for row in rows]


def _public_quote_facts(raw_facts: object) -> dict[str, dict[str, object]]:
    """Expose fact values and provenance without leaking database identifiers."""

    if isinstance(raw_facts, str):
        try:
            raw_facts = json.loads(raw_facts)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw_facts, dict):
        return {}
    public: dict[str, dict[str, object]] = {}
    for name, fact in raw_facts.items():
        if name not in {
            "price_amount",
            "currency",
            "price_unit",
            "pack_qty",
            "moq",
            "order_multiple",
            "availability_status",
            "available_qty",
            "availability_date",
        } or not isinstance(fact, dict):
            continue
        public[name] = {
            key: fact[key] for key in ("value", "source_type", "observed_at") if key in fact
        }
    return public


async def _load_case_offer_rows(connection: asyncpg.Connection, case_id: UUID) -> list[dict]:
    rows = await connection.fetch(
        """
        SELECT o.id,o.request_item_id,o.supplier_id,o.status,
               o.supplier_product_name,o.model_sku,
               s.display_name AS supplier_name,sl.city AS supplier_city,
               sl.source_provider,sl.provider_place_id,
               location_evidence.source_url AS location_source_url,
               CASE WHEN quote.version IS NOT NULL
                    THEN NULLIF(quote.snapshot#>>'{facts,price_amount,value}','')::numeric
                    ELSE priced.price_amount END AS price_amount,
               CASE WHEN quote.version IS NOT NULL
                    THEN quote.snapshot#>>'{facts,currency,value}'
                    ELSE priced.currency END AS currency,
               CASE WHEN quote.version IS NOT NULL
                    THEN quote.snapshot#>>'{facts,price_unit,value}'
                    ELSE COALESCE(terms.price_unit,priced.price_unit) END AS price_unit,
               CASE WHEN quote.version IS NOT NULL
                    THEN NULLIF(quote.snapshot#>>'{facts,pack_qty,value}','')::numeric
                    ELSE COALESCE(terms.pack_qty,priced.pack_qty) END AS pack_qty,
               CASE WHEN quote.version IS NOT NULL
                    THEN NULLIF(quote.snapshot#>>'{facts,moq,value}','')::numeric
                    ELSE COALESCE(terms.moq,priced.moq) END AS moq,
               CASE WHEN quote.version IS NOT NULL
                    THEN NULLIF(quote.snapshot#>>'{facts,order_multiple,value}','')::numeric
                    ELSE COALESCE(terms.order_multiple,priced.order_multiple) END AS order_multiple,
               CASE WHEN quote.version IS NOT NULL
                    THEN NULLIF(quote.snapshot#>>'{facts,available_qty,value}','')::numeric
                    ELSE stock.available_qty END AS available_qty,
               CASE WHEN quote.version IS NOT NULL
                    THEN COALESCE(quote.snapshot#>>'{facts,availability_status,value}','unknown')
                    ELSE COALESCE(stock.availability_status,'unknown') END AS availability_status,
               CASE WHEN quote.version IS NOT NULL
                    THEN COALESCE(quote.snapshot#>>'{facts,availability_status,source_type}',
                                  quote.snapshot#>>'{facts,available_qty,source_type}')
                    ELSE stock.source_type END AS stock_source_type,
               CASE WHEN quote.version IS NOT NULL
                    THEN COALESCE(
                        NULLIF(quote.snapshot#>>'{facts,availability_status,observed_at}','')::timestamptz,
                        NULLIF(quote.snapshot#>>'{facts,available_qty,observed_at}','')::timestamptz)
                    ELSE stock.observed_at END AS stock_observed_at,
               CASE WHEN quote.version IS NOT NULL
                    THEN COALESCE(
                        NULLIF(quote.snapshot#>>'{facts,price_amount,observed_at}','')::timestamptz,
                        NULLIF(quote.snapshot#>>'{facts,availability_status,observed_at}','')::timestamptz,
                        NULLIF(quote.snapshot#>>'{facts,available_qty,observed_at}','')::timestamptz)
                    ELSE COALESCE(priced.observed_at,latest.observed_at) END AS observed_at,
               COALESCE(priced.evidence_level,latest.evidence_level) AS evidence_level,
               COALESCE(priced.document_status,latest.document_status) AS document_status,
               COALESCE(priced.delivery_status,latest.delivery_status) AS delivery_status,
               COALESCE(priced.vat_status,latest.vat_status) AS vat_status,
               CASE WHEN quote.version IS NOT NULL
                    THEN quote.snapshot#>>'{facts,price_amount,source_type}'
                    ELSE priced.source_type END AS source_type,
               CASE WHEN quote.version IS NOT NULL
                    THEN snapshot_price_evidence.source_url
                    ELSE COALESCE(priced.source_url,latest.source_url) END AS source_url,
               CASE WHEN quote.version IS NOT NULL
                    THEN NULLIF(quote.snapshot#>>'{facts,price_amount,source_evidence_id}','')::uuid
                    ELSE COALESCE(priced.source_evidence_id,latest.source_evidence_id)
                    END AS source_evidence_id,
               quote.version AS quote_snapshot_version,
               quote.created_at AS quote_snapshot_created_at,
               quote.snapshot->'facts' AS quote_snapshot_facts_raw
        FROM offers o
        JOIN request_items i ON i.id=o.request_item_id
        JOIN suppliers s ON s.id=o.supplier_id
        LEFT JOIN supplier_locations sl ON sl.id=o.supplier_location_id
        LEFT JOIN source_evidence location_evidence
          ON location_evidence.id=sl.source_evidence_id
        LEFT JOIN quote_snapshot_current current_quote
          ON current_quote.offer_id=o.id AND current_quote.request_item_id=o.request_item_id
        LEFT JOIN quote_snapshot_versions quote
          ON quote.id=current_quote.snapshot_id AND quote.offer_id=o.id
         AND quote.request_item_id=o.request_item_id
        LEFT JOIN source_evidence snapshot_price_evidence
          ON snapshot_price_evidence.id=
             NULLIF(quote.snapshot#>>'{facts,price_amount,source_evidence_id}','')::uuid
        LEFT JOIN LATERAL (
            SELECT ob.*,se.source_url
            FROM offer_observations ob
            JOIN source_evidence se ON se.id=ob.source_evidence_id
            WHERE ob.offer_id=o.id ORDER BY ob.observed_at DESC,ob.id DESC LIMIT 1
        ) latest ON TRUE
        LEFT JOIN LATERAL (
            SELECT ob.*,se.source_url
            FROM offer_observations ob
            JOIN source_evidence se ON se.id=ob.source_evidence_id
            WHERE ob.offer_id=o.id AND ob.price_amount IS NOT NULL
            ORDER BY ob.observed_at DESC,ob.id DESC LIMIT 1
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
            SELECT ob.available_qty,ob.availability_status,ob.source_type,ob.observed_at
            FROM offer_observations ob
            WHERE ob.offer_id=o.id AND
              (ob.availability_status<>'unknown' OR ob.available_qty IS NOT NULL)
            ORDER BY ob.observed_at DESC,ob.id DESC LIMIT 1
        ) stock ON TRUE
        WHERE i.case_id=$1 ORDER BY i.line_number,o.updated_at DESC LIMIT 1001
        """,
        case_id,
    )
    result: list[dict] = []
    for row in rows:
        offer = dict(row)
        offer["quote_snapshot_facts"] = _public_quote_facts(
            offer.pop("quote_snapshot_facts_raw", None)
        )
        result.append(offer)
    return result


async def cases(request: web.Request) -> web.Response:
    limit, offset = _page_params(request)
    status = request.query.get("status", "")
    if status and status not in CASE_STATUSES:
        raise web.HTTPBadRequest(text="Invalid status")
    city = request.query.get("city", "").strip()[:100]
    query = request.query.get("q", "").strip()[:120]
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT c.id, c.title, c.city, c.status, c.deadline_at, c.updated_at,
                   customer.display_name AS customer_name,
                   count(i.id)::int AS item_count,
                   count(i.id) FILTER (
                       WHERE i.quantity IS NOT NULL
                         AND COALESCE(accepted.quantity,0)
                             + COALESCE(cancelled.quantity,0)=i.quantity
                   )::int AS closed_item_count
            FROM procurement_cases c
            LEFT JOIN customers customer ON customer.id=c.customer_id
            LEFT JOIN request_items i ON i.case_id=c.id
            LEFT JOIN LATERAL (
                SELECT sum(CASE WHEN event_type='accepted' THEN accepted_quantity
                                ELSE -accepted_quantity END) AS quantity
                FROM customer_acceptance_events WHERE request_item_id=i.id
            ) accepted ON TRUE
            LEFT JOIN LATERAL (
                SELECT sum(quantity_delta) AS quantity
                FROM request_item_execution_events
                WHERE request_item_id=i.id AND event_kind='cancelled'
            ) cancelled ON TRUE
            WHERE c.owner_user_id=$1
              AND ($2='' OR c.status=$2)
              AND ($3='' OR lower(c.city)=lower($3))
              AND ($4='' OR position(lower($4) in lower(c.title || ' ' || c.city))>0)
            GROUP BY c.id, customer.display_name
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT $5 OFFSET $6
            """,
            request["owner_id"],
            status,
            city,
            query,
            limit,
            offset,
        )
        cities = await connection.fetch(
            "SELECT DISTINCT city FROM procurement_cases "
            "WHERE owner_user_id=$1 AND city<>'' ORDER BY city",
            request["owner_id"],
        )
    return _json(
        request,
        {
            "rows": [dict(row) for row in rows],
            "cities": [row["city"] for row in cities],
            "has_more": len(rows) == limit,
        },
    )


async def case_detail(request: web.Request) -> web.Response:
    try:
        case_id = UUID(request.match_info["case_id"])
    except ValueError as exc:
        raise web.HTTPNotFound from exc
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as connection:
        case = await connection.fetchrow(
            """
            SELECT c.id,c.title,c.city,c.source_kind,c.search_area_text,c.delivery_address,
                   c.status,c.deadline_at,c.created_at,c.updated_at,
                   (c.source_kind='notion_import' OR EXISTS(
                       SELECT 1 FROM case_derivations d WHERE d.derived_case_id=c.id
                   )) AS item_research_enabled,
                   customer.display_name AS customer_name
            FROM procurement_cases c
            LEFT JOIN customers customer ON customer.id=c.customer_id
            WHERE c.id=$1 AND c.owner_user_id=$2
            """,
            case_id,
            request["owner_id"],
        )
        if case is None:
            raise web.HTTPNotFound
        items = await connection.fetch(
            """
            SELECT i.id,i.line_number,i.name,i.specification_text,i.quantity,
                   i.unit,i.status,f.tender_unit_price_private,
                   COALESCE(f.currency,'KZT') AS tender_currency,
                   snap.reported_delivered_quantity,snap.reported_accepted_quantity,
                   snap.reported_rejected_quantity,snap.reported_remaining_quantity,
                   snap.movement_label,snap.reconciliation_label,snap.location_label,
                   snap.source_url AS source_record_url,
                   purchased.purchased_quantity,accepted.accepted_quantity,
                   CASE WHEN delivery.delivery_cost IS NULL
                             AND expenses.delivery_expense_cost IS NULL THEN NULL
                        ELSE COALESCE(delivery.delivery_cost,0)
                             + COALESCE(expenses.delivery_expense_cost,0) END AS delivery_cost,
                   expenses.actual_cost,expenses.planned_cost,
                   transit.in_transit_quantity,
                   execution.event_count,execution.warehouse_received_quantity,
                   execution.delivered_quantity,execution.rejected_quantity,
                   execution.returned_quantity,execution.cancelled_quantity,
                   count(o.id)::int AS offer_count
            FROM request_items i LEFT JOIN offers o ON o.request_item_id=i.id
            LEFT JOIN request_item_financials f ON f.request_item_id=i.id
            LEFT JOIN LATERAL (
                SELECT reported_delivered_quantity,reported_accepted_quantity,
                       reported_rejected_quantity,reported_remaining_quantity,
                       movement_label,reconciliation_label,location_label,source_url
                FROM request_item_source_snapshots
                WHERE request_item_id=i.id ORDER BY captured_at DESC,imported_at DESC LIMIT 1
            ) snap ON TRUE
            LEFT JOIN LATERAL (
                SELECT sum(purchased_quantity) AS purchased_quantity FROM purchase_items
                WHERE request_item_id=i.id
            ) purchased ON TRUE
            LEFT JOIN LATERAL (
                SELECT sum(CASE WHEN event_type='accepted' THEN accepted_quantity
                                ELSE -accepted_quantity END) AS accepted_quantity
                FROM customer_acceptance_events WHERE request_item_id=i.id
            ) accepted ON TRUE
            LEFT JOIN LATERAL (
                SELECT sum(allocated_amount) AS delivery_cost FROM delivery_allocations
                WHERE request_item_id=i.id
            ) delivery ON TRUE
            LEFT JOIN LATERAL (
                SELECT sum(a.allocated_amount) FILTER (
                           WHERE e.cost_kind='actual' AND e.category='delivery'
                       ) AS delivery_expense_cost,
                       sum(a.allocated_amount) FILTER (
                           WHERE e.cost_kind='actual' AND e.category<>'delivery'
                       )
                           AS actual_cost,
                       sum(a.allocated_amount) FILTER (WHERE e.cost_kind='planned')
                           AS planned_cost
                FROM case_expense_allocations a
                JOIN case_expenses e ON e.id=a.expense_id
                WHERE a.request_item_id=i.id
            ) expenses ON TRUE
            LEFT JOIN LATERAL (
                SELECT sum(GREATEST(
                    di.quantity - GREATEST(COALESCE(m.received,0),
                                           COALESCE(m.delivered,0)),0
                )) AS in_transit_quantity
                FROM delivery_run_items di
                JOIN delivery_runs dr ON dr.id=di.delivery_run_id
                LEFT JOIN LATERAL (
                    SELECT sum(e.quantity_delta) FILTER (
                               WHERE e.event_kind='warehouse_received') AS received,
                           sum(e.quantity_delta) FILTER (
                               WHERE e.event_kind='customer_delivered') AS delivered
                    FROM request_item_execution_events e
                    WHERE e.delivery_run_id=di.delivery_run_id
                      AND e.request_item_id=di.request_item_id
                ) m ON TRUE
                WHERE di.request_item_id=i.id AND dr.status='in_transit'
            ) transit ON TRUE
            LEFT JOIN request_item_execution_totals execution
              ON execution.request_item_id=i.id
            WHERE i.case_id=$1
            GROUP BY i.id,f.tender_unit_price_private,f.currency,
                     snap.reported_delivered_quantity,snap.reported_accepted_quantity,
                     snap.reported_rejected_quantity,snap.reported_remaining_quantity,
                     snap.movement_label,snap.reconciliation_label,snap.location_label,
                     snap.source_url,purchased.purchased_quantity,
                     accepted.accepted_quantity,delivery.delivery_cost,
                     expenses.delivery_expense_cost,expenses.actual_cost,
                     expenses.planned_cost,transit.in_transit_quantity,
                     execution.event_count,execution.warehouse_received_quantity,
                     execution.delivered_quantity,execution.rejected_quantity,
                     execution.returned_quantity,execution.cancelled_quantity
            ORDER BY i.line_number
            """,
            case_id,
        )
        offers = await _load_case_offer_rows(connection, case_id)
        requirements, technical_reviews = await load_case_technical_reviews(connection, case_id)
        technical_by_offer = technical_states(offers, requirements, technical_reviews)
        items_by_id = {row["id"]: row for row in items}
        comparison_rows = [
            {
                **dict(offer),
                "quantity": items_by_id[offer["request_item_id"]]["quantity"],
                "unit": items_by_id[offer["request_item_id"]]["unit"],
                "tender_unit_price_private": items_by_id[offer["request_item_id"]][
                    "tender_unit_price_private"
                ],
                "tender_currency": items_by_id[offer["request_item_id"]]["tender_currency"],
                "technical_state": technical_by_offer[str(offer["id"])],
            }
            for offer in offers
        ]
        comparisons = {
            entry.offer_id: entry.as_dict() for entry in rank_goods_offers(comparison_rows)
        }
        outreach_drafts = await load_initial_outreach_drafts(connection, case_id)
        research_progress = []
        if getattr(request.app["settings"], "parallel_research_enabled", False):
            research_progress = await _load_research_progress(connection, case_id)
        expense_rows = await connection.fetch(
            """
            SELECT a.request_item_id,a.allocated_amount,e.category,e.cost_kind,
                   e.amount AS expense_total,e.currency,e.allocation_method,
                   e.provenance,e.note,e.incurred_at,e.delivery_run_id
            FROM case_expense_allocations a
            JOIN case_expenses e ON e.id=a.expense_id
            WHERE e.case_id=$1
            ORDER BY e.created_at DESC,a.request_item_id
            """,
            case_id,
        )
        deliveries = await connection.fetch(
            """SELECT r.id,r.status,r.carrier_name,r.tracking_ref,r.expected_at,
                      r.note,r.created_at,r.total_cost,r.currency,
                      carrier.normalized_phone AS carrier_phone,
                      expense.actual_cost,expense.planned_cost,
                      terms.loading_mode,terms.included_loading_value,
                      terms.note AS loading_note,
                      COALESCE(payments.net_paid,0) AS net_paid,
                      COALESCE(r.total_cost,0)+COALESCE(expense.actual_cost,0)
                        AS charge_total,
                      COALESCE(r.total_cost,0)+COALESCE(expense.actual_cost,0)
                        - COALESCE(payments.net_paid,0) AS balance_due
               FROM delivery_runs r
               LEFT JOIN delivery_carriers carrier ON carrier.id=r.carrier_id
               LEFT JOIN LATERAL (
                   SELECT sum(amount) FILTER (WHERE cost_kind='actual') AS actual_cost,
                          sum(amount) FILTER (WHERE cost_kind='planned') AS planned_cost
                   FROM case_expenses WHERE delivery_run_id=r.id
               ) expense ON TRUE
               LEFT JOIN LATERAL (
                   SELECT loading_mode,included_loading_value,note
                   FROM delivery_run_terms WHERE delivery_run_id=r.id
                   ORDER BY created_at DESC,id DESC LIMIT 1
               ) terms ON TRUE
               LEFT JOIN LATERAL (
                   SELECT sum(CASE WHEN kind='payment' THEN amount ELSE -amount END)
                          AS net_paid
                   FROM delivery_payments WHERE delivery_run_id=r.id
               ) payments ON TRUE
               WHERE r.case_id=$1 ORDER BY r.created_at DESC,r.id DESC""",
            case_id,
        )
        delivery_payments = await connection.fetch(
            """SELECT p.id,p.delivery_run_id,p.kind,p.amount,p.currency,p.paid_at,
                      p.payee,p.note,p.source_artifact_id
               FROM delivery_payments p WHERE p.case_id=$1
               ORDER BY p.paid_at DESC,p.id DESC""",
            case_id,
        )
        delivery_items = await connection.fetch(
            """SELECT di.delivery_run_id,di.request_item_id,di.purchase_item_id,
                      di.quantity,i.line_number,i.name,i.unit
               FROM delivery_run_items di
               JOIN request_items i ON i.id=di.request_item_id
               WHERE di.case_id=$1 ORDER BY di.delivery_run_id,i.line_number""",
            case_id,
        )
        purchase_lines = await connection.fetch(
            """SELECT p.id,p.request_item_id,p.purchased_quantity,p.unit_cost,p.currency,
                      p.offer_id,d.seller_name,d.document_kind,d.document_number,
                      d.provenance,d.note,d.source_artifact_id,d.created_at,
                      supplier.display_name AS supplier_name
               FROM purchase_items p
               JOIN purchase_documents d ON d.id=p.purchase_document_id
               LEFT JOIN offers o ON o.id=p.offer_id
               LEFT JOIN suppliers supplier ON supplier.id=o.supplier_id
               WHERE p.case_id=$1 ORDER BY d.created_at DESC,p.id DESC""",
            case_id,
        )
        artifacts = await connection.fetch(
            """SELECT a.id,a.created_at,attachment.original_filename,
                      attachment.kind,message.text_content
               FROM source_artifacts a
               JOIN incoming_attachments attachment ON attachment.id=a.attachment_id
               JOIN messages message ON message.id=attachment.message_id
               WHERE message.user_id=$1 AND a.artifact_kind='original'
                 AND attachment.kind IN ('document','photo')
               ORDER BY a.created_at DESC,a.id DESC LIMIT 60""",
            request["owner_id"],
        )
        readiness = await load_case_closure_readiness(
            connection,
            case_id=case_id,
            owner_user_id=request["owner_id"],
        )
        financial = await load_financial_snapshot(
            connection,
            case_id=case_id,
            owner_user_id=request["owner_id"],
        )
        last_closure = await connection.fetchrow(
            "SELECT event_kind,reason,created_at FROM case_closure_events "
            "WHERE case_id=$1 ORDER BY created_at DESC,id DESC LIMIT 1",
            case_id,
        )
    return _json(
        request,
        {
            "case": dict(case),
            "items": [dict(row) for row in items],
            "offers": [
                {
                    **dict(row),
                    "technical_state": technical_by_offer[str(row["id"])],
                    "comparison": comparisons[str(row["id"])],
                }
                for row in offers[:1000]
            ],
            "requirements": [dict(row) for row in requirements],
            "technical_reviews": [dict(row) for row in technical_reviews],
            "outreach_drafts": outreach_drafts,
            "research_progress": research_progress,
            "outreach_enabled": request.app["settings"].waha_enabled,
            "offers_truncated": len(offers) > 1000,
            "expenses": [dict(row) for row in expense_rows],
            "deliveries": [dict(row) for row in deliveries],
            "delivery_payments": [dict(row) for row in delivery_payments],
            "delivery_items": [dict(row) for row in delivery_items],
            "purchase_lines": [dict(row) for row in purchase_lines],
            "artifacts": [dict(row) for row in artifacts],
            "closure": {
                "ready": readiness.ready,
                "issues": [vars_issue(issue) for issue in readiness.issues],
                "item_count": readiness.item_count,
                "last_event": dict(last_closure) if last_closure else None,
            },
            "financial": {
                "currency": financial.currency,
                "tender_revenue_private": financial.tender_revenue_private,
                "product_cost_known": financial.product_cost_known,
                "delivery_cost_known": financial.delivery_cost_known,
                "additional_cost_known": financial.additional_cost_known,
                "unknown_purchase_lines": financial.unknown_purchase_lines,
                "unknown_delivery_runs": financial.unknown_delivery_runs,
                "provisional_margin": financial.provisional_margin,
            },
        },
    )


def vars_issue(issue) -> dict[str, object]:
    return {"code": issue.code, "message": issue.message, "request_item_id": issue.request_item_id}


async def control_research(request: web.Request) -> web.Response:
    """Control checkpoints within an already authorized research run."""
    if not getattr(request.app["settings"], "parallel_research_enabled", False):
        raise web.HTTPNotFound
    from procurement_bot.parallel_research import ParallelResearchRunExecutor
    from procurement_bot.research_execution import ResearchExecutionRejected

    try:
        case_id = _uuid(request.match_info["case_id"])
        body = await _body(request)
        action = body.get("action")
        if action not in {"pause", "resume", "retry_failed"}:
            raise ValueError("Выберите приостановку, продолжение или повтор технических ошибок")
        pool = request.app["pool"]
        run_id = await pool.fetchval(
            """SELECT run.id FROM research_runs run
               JOIN procurement_cases c ON c.id=run.case_id
               WHERE c.id=$1 AND c.owner_user_id=$2 AND run.status='running'
                 AND c.status NOT IN ('closed','cancelled')
               ORDER BY run.created_at DESC,run.id DESC LIMIT 1""",
            case_id,
            request["owner_id"],
        )
        if run_id is None:
            return _json(request, {"error": "Нет активного поиска для этой заявки"}, status=409)
        settings = request.app["settings"]
        async with pool.acquire() as connection, connection.transaction():
            executor = ParallelResearchRunExecutor(
                connection,
                None,
                max_active_groups=getattr(settings, "research_active_groups", 3),
                target_suppliers=getattr(settings, "research_target_suppliers", 20),
                max_batches=getattr(settings, "research_max_batches", 20),
            )
            retried: list[UUID] = []
            if action == "pause":
                await executor.pause_run(run_id)
            elif action == "resume":
                await executor.resume_run(run_id)
            else:
                retried = await executor.retry_failed(run_id)
            await connection.execute(
                """INSERT INTO audit_log(
                       actor_user_id,actor_kind,action,entity_type,entity_id,metadata)
                   VALUES ($1,'user',$2,'research_run',$3,$4::jsonb)""",
                request["owner_id"],
                "research_" + action,
                str(run_id),
                {"case_id": str(case_id), "task_ids": [str(task_id) for task_id in retried]},
            )
        return _json(
            request, {"action": action, "research_run_id": run_id, "retried_count": len(retried)}
        )
    except ValueError as exc:
        return _json(request, {"error": str(exc)}, status=400)
    except ResearchExecutionRejected:
        return _json(
            request,
            {"error": "Поиск приостановлен или завершён. Сначала продолжите его."},
            status=409,
        )


async def add_offer_technical_review(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        offer_id = _uuid(request.match_info["offer_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        verdict = body.get("verdict")
        note = body.get("note")
        if not isinstance(verdict, str) or not isinstance(note, str):
            raise ValueError("Укажите итог проверки и основание")
        requirement_id = _uuid(body["requirement_id"]) if body.get("requirement_id") else None
        evidence_id = _uuid(body["source_evidence_id"]) if body.get("source_evidence_id") else None
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection, connection.transaction():
            review_id = await record_technical_review(
                connection,
                case_id=case_id,
                offer_id=offer_id,
                requirement_id=requirement_id,
                owner_user_id=request["owner_id"],
                verdict=verdict,
                note=note,
                source_evidence_id=evidence_id,
                idempotency_key=key,
            )
            await enqueue_job(
                connection,
                kind="refresh_report",
                payload={"case_id": str(case_id), "trigger_id": f"technical-review:{review_id}"},
                idempotency_key=stable_key("refresh-report-review", review_id),
            )
        return _json(request, {"id": review_id}, status=201)
    except (ValueError, TechnicalReviewConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def offer_technical_assessment(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        offer_id = _uuid(request.match_info["offer_id"])
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            proposal = await read_latest_technical_assessment(
                connection,
                case_id=case_id,
                offer_id=offer_id,
                owner_user_id=request["owner_id"],
            )
        return _json(request, {"assessment": proposal})
    except (ValueError, TechnicalReviewConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def request_offer_technical_assessment(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        offer_id = _uuid(request.match_info["offer_id"])
        body = await _body(request)
        request_key = str(_uuid(body.get("request_key")))
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            proposal_id = await enqueue_technical_assessment(
                connection,
                case_id=case_id,
                offer_id=offer_id,
                owner_user_id=request["owner_id"],
                request_key=request_key,
            )
        return _json(request, {"id": proposal_id, "status": "queued"}, status=202)
    except (ValueError, TechnicalReviewConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


def parse_item_requirement(body: dict[str, object]) -> dict[str, object]:
    """Validate one operator-authored, source-referenced requirement."""

    key = body.get("requirement_key")
    value = body.get("expected_value")
    unit = body.get("unit", "")
    source_pointer = body.get("source_pointer")
    operator = body.get("operator", "equals")
    is_hard = body.get("is_hard", True)
    if not isinstance(key, str) or not 1 <= len(key.strip()) <= 160:
        raise ValueError("Укажите название требования (до 160 символов)")
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
        raise ValueError("Укажите ожидаемое значение (до 500 символов)")
    if not isinstance(unit, str) or len(unit.strip()) > 40:
        raise ValueError("Единица измерения слишком длинная")
    if not isinstance(source_pointer, str) or not 1 <= len(source_pointer.strip()) <= 500:
        raise ValueError("Укажите источник в техспеке (страницу, пункт или цитату)")
    if not isinstance(operator, str) or operator not in {
        "equals",
        "contains",
        "minimum",
        "maximum",
        "range",
        "one_of",
        "present",
    }:
        raise ValueError("Недопустимый оператор требования")
    if not isinstance(is_hard, bool):
        raise ValueError("Укажите, является ли требование обязательным")
    return {
        "requirement_key": key.strip(),
        "expected_value": {"value": value.strip(), "unit": unit.strip()},
        "source_pointer": source_pointer.strip(),
        "operator": operator,
        "is_hard": is_hard,
    }


async def add_item_requirement(request: web.Request) -> web.Response:
    """Append an immutable atomic requirement to an owner's editable case item."""

    try:
        case_id = _uuid(request.match_info["case_id"])
        item_id = _uuid(request.match_info["item_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        requirement = parse_item_requirement(body)
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection, connection.transaction():
            item = await connection.fetchrow(
                """SELECT i.id FROM request_items i
                   JOIN procurement_cases c ON c.id=i.case_id
                   WHERE i.id=$1 AND c.id=$2 AND c.owner_user_id=$3
                     AND c.status NOT IN ('closed','cancelled')
                     FOR UPDATE OF i,c""",
                item_id,
                case_id,
                request["owner_id"],
            )
            if item is None:
                raise ValueError("Позиция не найдена или заявка закрыта")

            event_key = stable_key("item-requirement", key)
            previous = await connection.fetchrow(
                "SELECT request_item_id,data FROM workflow_events WHERE idempotency_key=$1",
                event_key,
            )
            facts = {
                **requirement,
                "requirement_id": None,
            }
            if previous is not None:
                data = previous["data"]
                expected = {**requirement, "requirement_id": str(data.get("requirement_id"))}
                if previous["request_item_id"] != item_id or data != expected:
                    raise ValueError("Ключ сохранения уже использован для других данных")
                return _json(request, {"id": data["requirement_id"]}, status=200)

            requirement_id = await connection.fetchval(
                """INSERT INTO item_requirements(
                     request_item_id,requirement_key,operator,expected_value,
                     is_hard,source_pointer
                   ) VALUES ($1,$2,$3,$4::jsonb,$5,$6)
                   ON CONFLICT (request_item_id,requirement_key,source_pointer)
                   DO NOTHING RETURNING id""",
                item_id,
                requirement["requirement_key"],
                requirement["operator"],
                requirement["expected_value"],
                requirement["is_hard"],
                requirement["source_pointer"],
            )
            if requirement_id is None:
                existing = await connection.fetchrow(
                    """SELECT id,operator,expected_value,is_hard FROM item_requirements
                       WHERE request_item_id=$1 AND requirement_key=$2 AND source_pointer=$3""",
                    item_id,
                    requirement["requirement_key"],
                    requirement["source_pointer"],
                )
                if existing is None or any(
                    (
                        existing["operator"] != requirement["operator"],
                        existing["expected_value"] != requirement["expected_value"],
                        existing["is_hard"] != requirement["is_hard"],
                    )
                ):
                    raise ValueError(
                        "Требование с таким источником уже существует с другими данными"
                    )
                requirement_id = existing["id"]

            facts["requirement_id"] = str(requirement_id)
            await connection.execute(
                """INSERT INTO workflow_events(
                     case_id,request_item_id,event_type,actor_type,actor_id,
                     idempotency_key,data
                   ) VALUES ($1,$2,'item_requirement_added','user',$3,$4,$5)""",
                case_id,
                item_id,
                str(request["owner_id"]),
                event_key,
                facts,
            )
            await enqueue_job(
                connection,
                kind="refresh_report",
                payload={"case_id": str(case_id), "trigger_id": f"requirement:{requirement_id}"},
                idempotency_key=stable_key("refresh-report-requirement", requirement_id),
            )
        return _json(request, {"id": requirement_id}, status=201)
    except (ValueError, TechnicalReviewConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def set_item_tender_price(request: web.Request) -> web.Response:
    """Save the owner's tender unit price for an item, then refresh comparisons."""

    try:
        case_id = _uuid(request.match_info["case_id"])
        item_id = _uuid(request.match_info["item_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        price = _decimal(body.get("tender_unit_price_private"))
        if price < 0:
            raise ValueError("Цена тендера не может быть отрицательной")
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection, connection.transaction():
            owned = await connection.fetchval(
                """SELECT EXISTS(SELECT 1 FROM request_items i
                     JOIN procurement_cases c ON c.id=i.case_id
                     WHERE i.id=$1 AND c.id=$2 AND c.owner_user_id=$3
                       AND c.status NOT IN ('closed','cancelled'))""",
                item_id,
                case_id,
                request["owner_id"],
            )
            if not owned:
                raise ValueError("Позиция не найдена или заявка закрыта")
            event_key = stable_key("item-tender-price", key)
            existing = await connection.fetchrow(
                "SELECT data FROM workflow_events WHERE idempotency_key=$1", event_key
            )
            if existing is not None:
                data = existing["data"]
                if str(data.get("request_item_id")) != str(item_id) or (
                    Decimal(str(data.get("tender_unit_price_private"))) != price
                ):
                    raise ValueError("Ключ сохранения использован для другой цены")
            else:
                await connection.execute(
                    """INSERT INTO request_item_financials(
                           request_item_id,tender_unit_price_private,currency,
                           source_system,updated_at)
                       VALUES ($1,$2,'KZT','manual',now())
                       ON CONFLICT (request_item_id) DO UPDATE SET
                           tender_unit_price_private=EXCLUDED.tender_unit_price_private,
                           currency='KZT',source_system='manual',updated_at=now()""",
                    item_id,
                    price,
                )
                await connection.execute(
                    """INSERT INTO workflow_events(
                           case_id,request_item_id,event_type,actor_type,actor_id,
                           idempotency_key,data)
                       VALUES ($1,$2,'tender_unit_price_set','user',$3,$4,$5::jsonb)""",
                    case_id,
                    item_id,
                    str(request["owner_id"]),
                    event_key,
                    {"request_item_id": str(item_id), "tender_unit_price_private": str(price)},
                )
                await enqueue_job(
                    connection,
                    kind="refresh_report",
                    payload={"case_id": str(case_id), "trigger_id": f"tender-price:{key}"},
                    idempotency_key=stable_key("refresh-report-tender-price", key),
                )
        return _json(request, {"request_item_id": item_id, "tender_unit_price_private": price})
    except (ValueError, InvalidOperation) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def approve_outreach_drafts(request: web.Request) -> web.Response:
    """Approve selected visible first-contact drafts with the owner's Mini App auth."""

    try:
        case_id = _uuid(request.match_info["case_id"])
        if not request.app["settings"].waha_enabled:
            raise OutreachBatchConflict("WhatsApp отправка сейчас выключена")
        body = await _body(request)
        raw_ids = body.get("approval_ids")
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 10:
            raise OutreachBatchConflict("Выберите от 1 до 10 сообщений")
        approval_ids = [_uuid(value) for value in raw_ids]
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            approved = await approve_initial_outreach_batch(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
                approval_ids=approval_ids,
            )
        return _json(request, {"approved": approved})
    except (ValueError, OutreachBatchConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def refresh_outreach_drafts(request: web.Request) -> web.Response:
    """Return replacement exact texts for a separate owner approval step."""

    try:
        case_id = _uuid(request.match_info["case_id"])
        body = await _body(request)
        raw_ids = body.get("approval_ids")
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 10:
            raise OutreachBatchConflict("Выберите от 1 до 10 сообщений")
        approval_ids = [_uuid(value) for value in raw_ids]
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            refreshed = await refresh_initial_outreach_drafts(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
                approval_ids=approval_ids,
            )
        return _json(request, {"drafts": [asdict(draft) for draft in refreshed]})
    except (ValueError, OutreachBatchConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("Неверный идентификатор")
    return UUID(value)


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Укажите число")
    try:
        result = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Укажите число") from exc
    if not result.is_finite():
        raise ValueError("Укажите конечное число")
    if result.copy_abs() >= Decimal("1000000000000") or result.as_tuple().exponent < -6:
        raise ValueError("Число вне допустимого диапазона")
    return result


async def _body(request: web.Request) -> dict[str, object]:
    try:
        value = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise web.HTTPBadRequest(text="Неверный JSON") from exc
    if not isinstance(value, dict):
        raise web.HTTPBadRequest(text="Ожидается объект JSON")
    return value


async def add_item_event(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        item_id = _uuid(request.match_info["item_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        kind = body.get("event_kind")
        if not isinstance(kind, str):
            raise ValueError("Выберите тип события")
        quantity = _decimal(body.get("quantity_delta"))
        reason = body.get("reason", "")
        if not isinstance(reason, str) or len(reason) > 1000:
            raise ValueError("Причина слишком длинная")
        artifact_id = (
            None if body.get("source_artifact_id") is None else _uuid(body["source_artifact_id"])
        )
        delivery_id = (
            None if body.get("delivery_run_id") is None else _uuid(body["delivery_run_id"])
        )
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection, connection.transaction():
            await ensure_case_editable(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
            )
            if kind in {"customer_accepted", "customer_revoked"}:
                if quantity <= 0:
                    raise ValueError("Укажите положительное принятое количество")
                customer_name = body.get("customer_name", "")
                if not isinstance(customer_name, str) or len(customer_name) > 200:
                    raise ValueError("Укажите заказчика")
                existing_customer = await connection.fetchval(
                    "SELECT customer.display_name FROM procurement_cases c "
                    "JOIN customers customer ON customer.id=c.customer_id "
                    "WHERE c.id=$1 AND c.owner_user_id=$2",
                    case_id,
                    request["owner_id"],
                )
                customer_name = customer_name.strip() or existing_customer
                if not customer_name:
                    raise AccountingConflict("У заявки не указан заказчик")
                result = await record_customer_acceptance(
                    connection,
                    case_id=case_id,
                    request_item_id=item_id,
                    owner_user_id=request["owner_id"],
                    customer_name=customer_name,
                    accepted_quantity=quantity,
                    event_type="accepted" if kind == "customer_accepted" else "revoked",
                    idempotency_key=key,
                    source_artifact_id=artifact_id,
                    delivery_run_id=delivery_id,
                    reason=reason,
                )
                event_id = result.event_id
            else:
                event_id = await record_item_execution_event(
                    connection,
                    case_id=case_id,
                    request_item_id=item_id,
                    owner_user_id=request["owner_id"],
                    event_kind=kind,
                    quantity_delta=quantity,
                    idempotency_key=key,
                    reason=reason,
                    source_artifact_id=artifact_id,
                    delivery_run_id=delivery_id,
                )
        return _json(request, {"id": event_id}, status=201)
    except (ValueError, ItemExecutionConflict, AccountingConflict, CaseClosureConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def add_purchase(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        values = body.get("lines")
        if not isinstance(values, list) or not 1 <= len(values) <= 200:
            raise ValueError("Укажите позиции и количество закупки")
        lines = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("Неверная строка закупки")
            lines.append(
                PurchaseLine(
                    request_item_id=_uuid(value.get("request_item_id")),
                    purchased_quantity=_decimal(value.get("purchased_quantity")),
                    unit_cost=_decimal(value.get("unit_cost")),
                    offer_id=(None if value.get("offer_id") is None else _uuid(value["offer_id"])),
                )
            )
        kind = body.get("document_kind", "other")
        seller = body.get("seller_name", "")
        number = body.get("document_number", "")
        note = body.get("note", "")
        if not all(isinstance(value, str) for value in (kind, seller, number, note)):
            raise ValueError("Неверные сведения о закупке")
        artifact_id = (
            None if body.get("source_artifact_id") is None else _uuid(body["source_artifact_id"])
        )
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection, connection.transaction():
            await ensure_case_editable(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
            )
            result = await record_purchase(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
                source_artifact_id=artifact_id,
                document_kind=kind,
                lines=tuple(lines),
                idempotency_key=key,
                document_number=number,
                seller_name=seller,
                note=note,
            )
        return _json(
            request, {"id": result.document_id, "recorded_lines": result.recorded_lines}, status=201
        )
    except (ValueError, AccountingConflict, CaseClosureConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def change_case_status(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        action = body.get("action")
        reason = body.get("reason", "")
        if not isinstance(action, str) or not isinstance(reason, str):
            raise ValueError("Неверное действие")
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            event_id = await change_case_closure(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
                action=action,
                idempotency_key=key,
                reason=reason,
            )
        return _json(request, {"id": event_id}, status=201)
    except (ValueError, CaseClosureConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def start_item_research(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        item_id = _uuid(request.match_info["item_id"])
        body = await _body(request)
        if body:
            raise ValueError("Параметры поиска задаются сохранённой позицией")
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            run_id = await start_imported_item_research(
                connection,
                case_id=case_id,
                item_id=item_id,
                owner_user_id=request["owner_id"],
            )
        return _json(request, {"research_run_id": run_id, "status": "running"}, status=202)
    except (ValueError, ImportedResearchConflict, ResearchStartDenied) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def add_case_expense(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        category = body.get("category")
        cost_kind = body.get("cost_kind")
        method = body.get("allocation_method")
        item_values = body.get("request_item_ids")
        if (
            not isinstance(category, str)
            or not isinstance(cost_kind, str)
            or not isinstance(method, str)
            or not isinstance(item_values, list)
            or len(item_values) > 200
        ):
            raise ValueError("Проверьте категорию, тип и позиции расхода")
        item_ids = [_uuid(value) for value in item_values]
        manual_values = body.get("manual_amounts")
        manual = None
        if manual_values is not None:
            if not isinstance(manual_values, dict):
                raise ValueError("Ручные доли должны быть объектом")
            manual = {_uuid(key): _decimal(value) for key, value in manual_values.items()}
        note = body.get("note", "")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("Примечание слишком длинное")
        delivery_run_id = (
            None if body.get("delivery_run_id") is None else _uuid(body["delivery_run_id"])
        )
        source_artifact_id = (
            None if body.get("source_artifact_id") is None else _uuid(body["source_artifact_id"])
        )
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection, connection.transaction():
            await ensure_case_editable(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
            )
            expense_id = await record_case_expense(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
                category=category,
                cost_kind=cost_kind,
                total_cost=_decimal(body.get("total_cost")),
                request_item_ids=item_ids,
                allocation_method=method,
                idempotency_key=key,
                delivery_run_id=delivery_run_id,
                manual_amounts=manual,
                note=note,
                source_artifact_id=source_artifact_id,
            )
        return _json(request, {"id": expense_id}, status=201)
    except (ValueError, AccountingConflict, CaseClosureConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def add_delivery(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        values = body.get("items")
        if not isinstance(values, list) or not 1 <= len(values) <= 200:
            raise ValueError("Укажите позиции и количество груза")
        lines = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("Неверная строка груза")
            lines.append(
                DeliveryLine(
                    request_item_id=_uuid(value.get("request_item_id")),
                    quantity=_decimal(value.get("quantity")),
                    purchase_item_id=(
                        None
                        if value.get("purchase_item_id") is None
                        else _uuid(value["purchase_item_id"])
                    ),
                )
            )
        fields = {}
        for name in ("carrier_name", "carrier_phone", "tracking_ref", "note"):
            value = body.get(name, "")
            if not isinstance(value, str):
                raise ValueError("Неверные данные доставки")
            fields[name] = value
        expected_at = None
        if body.get("expected_at") is not None:
            if not isinstance(body["expected_at"], str):
                raise ValueError("Неверная дата доставки")
            expected_at = datetime.fromisoformat(body["expected_at"].replace("Z", "+00:00"))
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection, connection.transaction():
            await ensure_case_editable(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
            )
            run_id = await create_delivery_run(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
                lines=tuple(lines),
                idempotency_key=key,
                expected_at=expected_at,
                **fields,
            )
            if "loading_mode" in body:
                loading_mode = body["loading_mode"]
                if not isinstance(loading_mode, str):
                    raise ValueError("Неверные условия погрузки")
                included = body.get("included_loading_value")
                await record_delivery_terms(
                    connection,
                    case_id=case_id,
                    delivery_run_id=run_id,
                    owner_user_id=request["owner_id"],
                    loading_mode=loading_mode,
                    included_loading_value=(None if included in (None, "") else _decimal(included)),
                    note="",
                    source_artifact_id=None,
                    idempotency_key=f"{key}:loading",
                )
        return _json(request, {"id": run_id}, status=201)
    except (ValueError, DeliveryConflict, DeliverySettlementConflict, CaseClosureConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def add_delivery_terms(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        delivery_id = _uuid(request.match_info["delivery_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        mode = body.get("loading_mode")
        note = body.get("note", "")
        if not isinstance(mode, str) or not isinstance(note, str):
            raise ValueError("Неверные условия погрузки")
        included = body.get("included_loading_value")
        artifact = body.get("source_artifact_id")
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            event_id = await record_delivery_terms(
                connection,
                case_id=case_id,
                delivery_run_id=delivery_id,
                owner_user_id=request["owner_id"],
                loading_mode=mode,
                included_loading_value=(None if included in (None, "") else _decimal(included)),
                note=note,
                source_artifact_id=(None if artifact is None else _uuid(artifact)),
                idempotency_key=key,
            )
        return _json(request, {"id": event_id}, status=201)
    except (ValueError, DeliverySettlementConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def add_delivery_payment(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        delivery_id = _uuid(request.match_info["delivery_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        paid_at = body.get("paid_at")
        payee = body.get("payee", "")
        note = body.get("note", "")
        kind = body.get("kind")
        if (
            not isinstance(paid_at, str)
            or not isinstance(payee, str)
            or not isinstance(note, str)
            or not isinstance(kind, str)
        ):
            raise ValueError("Неверные данные оплаты")
        artifact = body.get("source_artifact_id")
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            payment_id = await record_delivery_payment(
                connection,
                case_id=case_id,
                delivery_run_id=delivery_id,
                owner_user_id=request["owner_id"],
                kind=kind,
                amount=_decimal(body.get("amount")),
                paid_at=datetime.fromisoformat(paid_at.replace("Z", "+00:00")),
                payee=payee,
                note=note,
                source_artifact_id=(None if artifact is None else _uuid(artifact)),
                idempotency_key=key,
            )
        return _json(request, {"id": payment_id}, status=201)
    except (ValueError, DeliverySettlementConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def change_delivery_status(request: web.Request) -> web.Response:
    try:
        case_id = _uuid(request.match_info["case_id"])
        delivery_id = _uuid(request.match_info["delivery_id"])
        body = await _body(request)
        key = str(_uuid(body.get("idempotency_key")))
        status = body.get("status")
        reason = body.get("reason", "")
        if not isinstance(status, str) or not isinstance(reason, str):
            raise ValueError("Неверный статус доставки")
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection, connection.transaction():
            await ensure_case_editable(
                connection,
                case_id=case_id,
                owner_user_id=request["owner_id"],
            )
            event_id = await transition_delivery_run(
                connection,
                case_id=case_id,
                delivery_run_id=delivery_id,
                owner_user_id=request["owner_id"],
                to_status=status,
                idempotency_key=key,
                reason=reason,
            )
        return _json(request, {"id": event_id}, status=201)
    except (ValueError, DeliveryConflict, CaseClosureConflict) as exc:
        return _json(request, {"error": str(exc)}, status=400)


async def suppliers(request: web.Request) -> web.Response:
    limit, offset = _page_params(request)
    query = request.query.get("q", "").strip()[:120]
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT s.id,s.display_name,s.status,s.reliability_level,
                   count(DISTINCT o.id)::int AS offer_count,
                   max(o.updated_at) AS last_offer_at
            FROM suppliers s
            JOIN offers o ON o.supplier_id=s.id
            JOIN request_items i ON i.id=o.request_item_id
            JOIN procurement_cases c ON c.id=i.case_id
            WHERE c.owner_user_id=$1
              AND ($2='' OR position(lower($2) in lower(s.display_name))>0)
            GROUP BY s.id ORDER BY max(o.updated_at) DESC,s.id DESC
            LIMIT $3 OFFSET $4
            """,
            request["owner_id"],
            query,
            limit,
            offset,
        )
    return _json(request, {"rows": [dict(row) for row in rows], "has_more": len(rows) == limit})


async def supplier_detail(request: web.Request) -> web.Response:
    try:
        supplier_id = _uuid(request.match_info["supplier_id"])
    except ValueError as exc:
        raise web.HTTPNotFound from exc
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as connection:
        supplier = await connection.fetchrow(
            """SELECT s.id,s.display_name,s.legal_name,s.supplier_type,
                      s.status,s.reliability_level
               FROM suppliers s WHERE s.id=$1 AND EXISTS (
                   SELECT 1 FROM offers o
                   JOIN request_items i ON i.id=o.request_item_id
                   JOIN procurement_cases c ON c.id=i.case_id
                   WHERE o.supplier_id=s.id AND c.owner_user_id=$2
               )""",
            supplier_id,
            request["owner_id"],
        )
        if supplier is None:
            raise web.HTTPNotFound
        contacts = await connection.fetch(
            """SELECT sc.id,sc.contact_type,sc.display_value,
                      sc.normalized_value,sc.label,sc.is_whatsapp_confirmed,
                      sc.confidence,sc.last_seen_at,se.source_url
               FROM supplier_contacts sc
               JOIN source_evidence se ON se.id=sc.source_evidence_id
               WHERE sc.supplier_id=$1 AND sc.active
                 AND __CONTACT_SCOPE__
               ORDER BY sc.is_whatsapp_confirmed DESC,sc.last_seen_at DESC,sc.id""".replace(
                "__CONTACT_SCOPE__",
                case_owned_supplier_contact_evidence(
                    contact_alias="sc", evidence_alias="se", scope="owner"
                ),
            ),
            supplier_id,
            request["owner_id"],
        )
        locations = await connection.fetch(
            """SELECT l.city,l.address_text,l.district,l.market_name,l.pavilion,
                      l.latitude,l.longitude,l.source_provider,l.provider_place_id,
                      se.source_url
               FROM supplier_locations l
               JOIN source_evidence se ON se.id=l.source_evidence_id
               WHERE l.supplier_id=$1 AND l.active
                 AND (
                   __CONTACT_SCOPE__
                   OR EXISTS (
                     SELECT 1 FROM offers own_offer
                     JOIN request_items own_item
                       ON own_item.id=own_offer.request_item_id
                     JOIN procurement_cases own_case ON own_case.id=own_item.case_id
                     WHERE own_offer.supplier_location_id=l.id
                       AND own_offer.supplier_id=l.supplier_id
                       AND own_case.owner_user_id=$2
                   )
                 )
               ORDER BY l.last_seen_at DESC,l.id LIMIT 30""".replace(
                "__CONTACT_SCOPE__",
                case_owned_supplier_contact_evidence(
                    contact_alias="l",
                    evidence_alias="se",
                    scope="owner",
                    include_enrichment=False,
                ),
            ),
            supplier_id,
            request["owner_id"],
        )
        offers = await connection.fetch(
            """SELECT o.id,o.status,o.supplier_product_name,
                      i.id AS request_item_id,i.line_number,i.name AS item_name,
                      c.id AS case_id,c.title AS case_title
               FROM offers o JOIN request_items i ON i.id=o.request_item_id
               JOIN procurement_cases c ON c.id=i.case_id
               WHERE o.supplier_id=$1 AND c.owner_user_id=$2
               ORDER BY o.updated_at DESC,o.id LIMIT 100""",
            supplier_id,
            request["owner_id"],
        )
        conversations = await connection.fetch(
            """SELECT sc.id,sc.case_id,sc.state,sc.inbound_count,sc.outbound_count,
                      sc.last_inbound_at,sc.last_outbound_at,sc.opted_out,
                      target.offer_id,target.request_item_id,
                      reply.data->>'classification' AS last_reply_kind,
                      approval.status AS approval_status,
                      latest.direction AS last_message_direction,
                      latest.text_content AS last_message_text,
                      latest.media_kind,latest.media_state,latest.media_text,
                      latest.media_language,latest.media_needs_clarification,
                      latest.media_error,
                      latest.id AS last_message_id,
                      latest.received_at AS last_message_at
               FROM supplier_conversations sc
               JOIN procurement_cases c ON c.id=sc.case_id
               LEFT JOIN supplier_conversation_targets target
                 ON target.conversation_id=sc.id
               LEFT JOIN LATERAL (
                   SELECT e.data FROM workflow_events e
                   WHERE e.case_id=sc.case_id
                     AND e.event_type='supplier_reply_processed'
                     AND e.data->>'conversation_id'=sc.id::text
                   ORDER BY e.created_at DESC,e.id DESC LIMIT 1
               ) reply ON TRUE
               LEFT JOIN LATERAL (
                   SELECT CASE WHEN a.status IN ('requested','approved')
                                 AND a.expires_at<=now() THEN 'expired'
                               ELSE a.status END AS status
                   FROM action_approvals a
                   WHERE a.case_id=sc.case_id AND a.action_type='send_whatsapp'
                     AND a.payload->>'conversation_id'=sc.id::text
                   ORDER BY a.requested_at DESC,a.id DESC LIMIT 1
               ) approval ON TRUE
               LEFT JOIN LATERAL (
                   SELECT m.id,m.direction,m.text_content,m.received_at,
                          media.kind AS media_kind,media.state AS media_state,
                          media.extracted_text AS media_text,media.language AS media_language,
                          media.needs_clarification AS media_needs_clarification,
                          media.last_error_code AS media_error
                   FROM supplier_conversation_messages m
                   LEFT JOIN supplier_message_media media ON media.message_id=m.id
                   WHERE m.conversation_id=sc.id
                   ORDER BY m.received_at DESC,m.id DESC LIMIT 1
               ) latest ON TRUE
               WHERE sc.supplier_id=$1 AND c.owner_user_id=$2
               ORDER BY sc.updated_at DESC,sc.id LIMIT 100""",
            supplier_id,
            request["owner_id"],
        )
        media_messages = await connection.fetch(
            """SELECT m.id AS last_message_id,m.conversation_id,m.received_at,
                      m.text_content AS original_caption,
                      media.kind AS media_kind,media.state AS media_state,
                      media.extracted_text AS media_text,media.language AS media_language,
                      media.needs_clarification AS media_needs_clarification,
                      media.last_error_code AS media_error
               FROM supplier_message_media media
               JOIN supplier_conversation_messages m ON m.id=media.message_id
               JOIN supplier_conversations sc ON sc.id=m.conversation_id
               JOIN procurement_cases c ON c.id=sc.case_id
               WHERE sc.supplier_id=$1 AND c.owner_user_id=$2
               ORDER BY m.received_at DESC,m.id DESC LIMIT 100""",
            supplier_id, request["owner_id"],
        )
        referrals = await connection.fetch(
            """SELECT r.id,r.conversation_id,r.normalized_phone,r.status,r.created_at
               FROM supplier_contact_referrals r
               JOIN supplier_conversations sc ON sc.id=r.conversation_id
               JOIN procurement_cases c ON c.id=sc.case_id
               WHERE sc.supplier_id=$1 AND c.owner_user_id=$2
               ORDER BY r.created_at DESC,r.id LIMIT 100""",
            supplier_id,
            request["owner_id"],
        )
        enrichment = await connection.fetch(
            """SELECT e.created_at,e.data->>'outcome' AS outcome,
                      e.data->>'access_issue' AS access_issue,
                      e.data->>'card_url' AS card_url,
                      e.data->>'invalid_contacts' AS invalid_contacts,
                      e.data->>'conflicting_contacts' AS conflicting_contacts
               FROM workflow_events e
               JOIN procurement_cases c ON c.id=e.case_id
               WHERE e.data->>'supplier_id'=$1::text
                 AND c.owner_user_id=$2
                 AND e.event_type IN (
                     'supplier_contact_enrichment_result','supplier_contact_enriched'
                 )
               ORDER BY e.created_at DESC,e.id DESC LIMIT 10""",
            str(supplier_id),
            request["owner_id"],
        )
    return _json(
        request,
        {
            "supplier": dict(supplier),
            "contacts": [dict(row) for row in contacts],
            "locations": [dict(row) for row in locations],
            "offers": [dict(row) for row in offers],
            "conversations": [dict(row) for row in conversations],
            "media_messages": [dict(row) for row in media_messages],
            "referrals": [dict(row) for row in referrals],
            "contact_enrichment": [dict(row) for row in enrichment],
        },
    )


async def css(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "app.css")


async def supplier_message_media(request: web.Request) -> web.Response:
    try:
        message_id = UUID(request.match_info["message_id"])
    except ValueError:
        raise web.HTTPNotFound() from None
    async with request.app["pool"].acquire() as connection:
        row = await connection.fetchrow(
            """SELECT media.object_key,media.kind,media.mime_type
               FROM supplier_message_media media
               JOIN supplier_conversation_messages m ON m.id=media.message_id
               JOIN supplier_conversations c ON c.id=m.conversation_id
               JOIN procurement_cases p ON p.id=c.case_id
               WHERE m.id=$1 AND p.owner_user_id=$2 AND media.object_key IS NOT NULL""",
            message_id, request["owner_id"],
        )
    if row is None:
        raise web.HTTPNotFound()
    try:
        path = media_storage_path(request.app["settings"].media_root, row["object_key"])
    except ValueError:
        raise web.HTTPNotFound() from None
    if not path.is_file():
        raise web.HTTPNotFound()
    content_type = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(
        path.suffix, "application/octet-stream",
    )
    if row["kind"] == "audio" and row["mime_type"] in {
        "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/flac",
    }:
        content_type = row["mime_type"]
    return web.FileResponse(path, headers={"Content-Type": content_type,
                                         "Cache-Control": "no-store"})


async def javascript(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "app.js")


def create_mini_app(pool: asyncpg.Pool, settings: Settings) -> web.Application:
    app = web.Application(middlewares=[security_headers, authenticate], client_max_size=16 * 1024)
    app["pool"] = pool
    app["settings"] = settings
    app.router.add_get("/", index)
    app.router.add_get("/app.css", css)
    app.router.add_get("/app.js", javascript)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/me", me)
    app.router.add_get("/api/summary", summary)
    app.router.add_get("/api/cases", cases)
    app.router.add_get("/api/cases/{case_id}", case_detail)
    app.router.add_post(
        "/api/cases/{case_id}/offers/{offer_id}/technical-reviews",
        add_offer_technical_review,
    )
    app.router.add_get(
        "/api/cases/{case_id}/offers/{offer_id}/technical-assessment",
        offer_technical_assessment,
    )
    app.router.add_post(
        "/api/cases/{case_id}/offers/{offer_id}/technical-assessment",
        request_offer_technical_assessment,
    )
    app.router.add_post(
        "/api/cases/{case_id}/items/{item_id}/tender-price",
        set_item_tender_price,
    )
    app.router.add_post(
        "/api/cases/{case_id}/items/{item_id}/requirements",
        add_item_requirement,
    )
    app.router.add_post(
        "/api/cases/{case_id}/outreach/approve",
        approve_outreach_drafts,
    )
    app.router.add_post(
        "/api/cases/{case_id}/outreach/refresh",
        refresh_outreach_drafts,
    )
    app.router.add_post("/api/cases/{case_id}/purchases", add_purchase)
    app.router.add_post("/api/cases/{case_id}/closure", change_case_status)
    app.router.add_post("/api/cases/{case_id}/items/{item_id}/research", start_item_research)
    app.router.add_post("/api/cases/{case_id}/research-control", control_research)
    app.router.add_post("/api/cases/{case_id}/items/{item_id}/events", add_item_event)
    app.router.add_post("/api/cases/{case_id}/expenses", add_case_expense)
    app.router.add_post("/api/cases/{case_id}/deliveries", add_delivery)
    app.router.add_post(
        "/api/cases/{case_id}/deliveries/{delivery_id}/status", change_delivery_status
    )
    app.router.add_post("/api/cases/{case_id}/deliveries/{delivery_id}/terms", add_delivery_terms)
    app.router.add_post(
        "/api/cases/{case_id}/deliveries/{delivery_id}/payments", add_delivery_payment
    )
    app.router.add_get("/api/suppliers", suppliers)
    app.router.add_get("/api/suppliers/{supplier_id}", supplier_detail)
    app.router.add_get("/api/supplier-messages/{message_id}/media", supplier_message_media)
    return app
