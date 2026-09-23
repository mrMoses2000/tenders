"""Read-only Telegram Mini App backed by the procurement PostgreSQL database."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl, urlparse
from uuid import UUID

import asyncpg
from aiohttp import web

from procurement_bot.config import Settings

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
                   i.unit,i.status,
                   count(o.id)::int AS offer_count
            FROM request_items i LEFT JOIN offers o ON o.request_item_id=i.id
            WHERE i.case_id=$1
            GROUP BY i.id ORDER BY i.line_number
            """,
            case_id,
        )
        offers = await connection.fetch(
            """
            SELECT o.id,o.request_item_id,o.status,o.supplier_product_name,
                   s.display_name AS supplier_name,sl.city AS supplier_city,
                   obs.price_amount,obs.currency,obs.availability_status,
                   obs.observed_at
            FROM offers o
            JOIN request_items i ON i.id=o.request_item_id
            JOIN suppliers s ON s.id=o.supplier_id
            LEFT JOIN supplier_locations sl ON sl.id=o.supplier_location_id
            LEFT JOIN LATERAL (
                SELECT price_amount,currency,availability_status,observed_at
                FROM offer_observations
                WHERE offer_id=o.id ORDER BY observed_at DESC,id DESC LIMIT 1
            ) obs ON TRUE
            WHERE i.case_id=$1 ORDER BY i.line_number,o.updated_at DESC
            LIMIT 200
            """,
            case_id,
        )
    return _json(request, {"case": dict(case), "items": [dict(row) for row in items],
                           "offers": [dict(row) for row in offers]})


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
    app.router.add_get("/api/suppliers", suppliers)
    return app
