"""Telegram Mini App backed by the procurement PostgreSQL database."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlparse
from uuid import UUID

import asyncpg
from aiohttp import web

from procurement_bot.accounting import (
    AccountingConflict,
    record_case_expense,
    record_customer_acceptance,
)
from procurement_bot.config import Settings
from procurement_bot.item_execution import ItemExecutionConflict, record_item_execution_event

STATIC = Path(__file__).resolve().parents[2] / "mini_app"
MAX_AGE_SECONDS = 24 * 60 * 60
CASE_STATUSES = frozenset(
    ("draft", "needs_clarification", "ready", "researching", "contacting",
     "evaluating", "report_ready", "selected", "closed", "cancelled")
)


def validate_public_url(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
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
    response = web.json_response(payload, status=status, dumps=lambda value: json.dumps(
        value, ensure_ascii=False, default=_serialize
    ))
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
        "style-src 'self'; connect-src 'self'; img-src 'self' data:; "
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
                   count(i.id) FILTER (WHERE i.status='closed')::int AS closed_item_count
            FROM procurement_cases c
            LEFT JOIN customers customer ON customer.id=c.customer_id
            LEFT JOIN request_items i ON i.case_id=c.id
            WHERE c.owner_user_id=$1
              AND ($2='' OR c.status=$2)
              AND ($3='' OR lower(c.city)=lower($3))
              AND ($4='' OR position(lower($4) in lower(c.title || ' ' || c.city))>0)
            GROUP BY c.id, customer.display_name
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT $5 OFFSET $6
            """,
            request["owner_id"], status, city, query, limit, offset,
        )
        cities = await connection.fetch(
            "SELECT DISTINCT city FROM procurement_cases "
            "WHERE owner_user_id=$1 AND city<>'' ORDER BY city",
            request["owner_id"],
        )
    return _json(request, {"rows": [dict(row) for row in rows],
                           "cities": [row["city"] for row in cities],
                           "has_more": len(rows) == limit})


async def case_detail(request: web.Request) -> web.Response:
    try:
        case_id = UUID(request.match_info["case_id"])
    except ValueError as exc:
        raise web.HTTPNotFound from exc
    pool: asyncpg.Pool = request.app["pool"]
    async with pool.acquire() as connection:
        case = await connection.fetchrow(
            """
            SELECT c.id,c.title,c.city,c.search_area_text,c.delivery_address,
                   c.status,c.deadline_at,c.created_at,c.updated_at,
                   customer.display_name AS customer_name
            FROM procurement_cases c
            LEFT JOIN customers customer ON customer.id=c.customer_id
            WHERE c.id=$1 AND c.owner_user_id=$2
            """,
            case_id, request["owner_id"],
        )
        if case is None:
            raise web.HTTPNotFound
        items = await connection.fetch(
            """
            SELECT i.id,i.line_number,i.name,i.specification_text,i.quantity,
                   i.unit,i.status,f.tender_unit_price_private,
                   snap.reported_delivered_quantity,snap.reported_accepted_quantity,
                   snap.reported_rejected_quantity,snap.reported_remaining_quantity,
                   snap.movement_label,snap.reconciliation_label,snap.location_label,
                   snap.source_url AS source_record_url,
                   purchased.purchased_quantity,accepted.accepted_quantity,
                   delivery.delivery_cost,expenses.actual_cost,expenses.planned_cost,
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
                SELECT sum(a.allocated_amount) FILTER (WHERE e.cost_kind='actual')
                           AS actual_cost,
                       sum(a.allocated_amount) FILTER (WHERE e.cost_kind='planned')
                           AS planned_cost
                FROM case_expense_allocations a
                JOIN case_expenses e ON e.id=a.expense_id
                WHERE a.request_item_id=i.id
            ) expenses ON TRUE
            LEFT JOIN request_item_execution_totals execution
              ON execution.request_item_id=i.id
            WHERE i.case_id=$1
            GROUP BY i.id,f.tender_unit_price_private,
                     snap.reported_delivered_quantity,snap.reported_accepted_quantity,
                     snap.reported_rejected_quantity,snap.reported_remaining_quantity,
                     snap.movement_label,snap.reconciliation_label,snap.location_label,
                     snap.source_url,purchased.purchased_quantity,
                     accepted.accepted_quantity,delivery.delivery_cost,
                     expenses.actual_cost,expenses.planned_cost,
                     execution.event_count,execution.warehouse_received_quantity,
                     execution.delivered_quantity,execution.rejected_quantity,
                     execution.returned_quantity,execution.cancelled_quantity
            ORDER BY i.line_number
            """,
            case_id,
        )
        offers = await connection.fetch(
            """
            SELECT o.id,o.request_item_id,o.status,o.supplier_product_name,o.model_sku,
                   s.display_name AS supplier_name,sl.city AS supplier_city,
                   obs.price_amount,obs.currency,obs.price_unit,obs.pack_qty,obs.moq,
                   obs.availability_status,
                   obs.observed_at,obs.evidence_level,obs.document_status,
                   obs.delivery_status,obs.vat_status,obs.source_type,obs.source_url
            FROM offers o
            JOIN request_items i ON i.id=o.request_item_id
            JOIN suppliers s ON s.id=o.supplier_id
            LEFT JOIN supplier_locations sl ON sl.id=o.supplier_location_id
            LEFT JOIN LATERAL (
                SELECT ob.price_amount,ob.currency,ob.price_unit,ob.pack_qty,ob.moq,
                       ob.availability_status,ob.source_type,
                       ob.observed_at,ob.evidence_level,ob.document_status,
                       ob.delivery_status,ob.vat_status,se.source_url
                FROM offer_observations ob
                JOIN source_evidence se ON se.id=ob.source_evidence_id
                WHERE ob.offer_id=o.id ORDER BY ob.observed_at DESC,ob.id DESC LIMIT 1
            ) obs ON TRUE
            WHERE i.case_id=$1 ORDER BY i.line_number,o.updated_at DESC
            LIMIT 1001
            """,
            case_id,
        )
        expense_rows = await connection.fetch(
            """
            SELECT a.request_item_id,a.allocated_amount,e.category,e.cost_kind,
                   e.amount AS expense_total,e.currency,e.allocation_method,
                   e.provenance,e.note,e.incurred_at
            FROM case_expense_allocations a
            JOIN case_expenses e ON e.id=a.expense_id
            WHERE e.case_id=$1
            ORDER BY e.created_at DESC,a.request_item_id
            """,
            case_id,
        )
    return _json(request, {"case": dict(case), "items": [dict(row) for row in items],
                           "offers": [dict(row) for row in offers[:1000]],
                           "offers_truncated": len(offers) > 1000,
                           "expenses": [dict(row) for row in expense_rows]})


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
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            if kind == "customer_accepted":
                if quantity <= 0:
                    raise ValueError("Укажите положительное принятое количество")
                customer_name = await connection.fetchval(
                    "SELECT customer.display_name FROM procurement_cases c "
                    "JOIN customers customer ON customer.id=c.customer_id "
                    "WHERE c.id=$1 AND c.owner_user_id=$2", case_id, request["owner_id"],
                )
                if customer_name is None:
                    raise AccountingConflict("У заявки не указан заказчик")
                result = await record_customer_acceptance(
                    connection, case_id=case_id, request_item_id=item_id,
                    owner_user_id=request["owner_id"], customer_name=customer_name,
                    accepted_quantity=quantity, event_type="accepted", idempotency_key=key,
                )
                event_id = result.event_id
            else:
                event_id = await record_item_execution_event(
                    connection, case_id=case_id, request_item_id=item_id,
                    owner_user_id=request["owner_id"], event_kind=kind,
                    quantity_delta=quantity, idempotency_key=key, reason=reason,
                )
        return _json(request, {"id": event_id}, status=201)
    except (ValueError, ItemExecutionConflict, AccountingConflict) as exc:
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
        if not isinstance(category, str) or not isinstance(cost_kind, str) \
                or not isinstance(method, str) or not isinstance(item_values, list) \
                or len(item_values) > 200:
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
        pool: asyncpg.Pool = request.app["pool"]
        async with pool.acquire() as connection:
            expense_id = await record_case_expense(
                connection, case_id=case_id, owner_user_id=request["owner_id"],
                category=category, cost_kind=cost_kind,
                total_cost=_decimal(body.get("total_cost")),
                request_item_ids=item_ids, allocation_method=method,
                idempotency_key=key, manual_amounts=manual, note=note,
            )
        return _json(request, {"id": expense_id}, status=201)
    except (ValueError, AccountingConflict) as exc:
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
            request["owner_id"], query, limit, offset,
        )
    return _json(request, {"rows": [dict(row) for row in rows],
                           "has_more": len(rows) == limit})


async def css(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "app.css")


async def javascript(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "app.js")


def create_mini_app(pool: asyncpg.Pool, settings: Settings) -> web.Application:
    app = web.Application(middlewares=[security_headers, authenticate],
                          client_max_size=16 * 1024)
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
    app.router.add_post("/api/cases/{case_id}/items/{item_id}/events", add_item_event)
    app.router.add_post("/api/cases/{case_id}/expenses", add_case_expense)
    app.router.add_get("/api/suppliers", suppliers)
    return app
