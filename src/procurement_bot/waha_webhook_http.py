from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import asyncpg
from aiohttp import web

from procurement_bot.phone import normalize_phone
from procurement_bot.whatsapp_inbound import (
    AuthenticatedWhatsAppInbound,
    WhatsAppInboundConflict,
    accept_whatsapp_inbound,
)

WAHA_WEBHOOK_PATH = "/webhooks/waha"
WAHA_WEBHOOK_AUTH_HEADER = "X-Webhook-Secret"
MAX_WEBHOOK_BODY_BYTES = 256 * 1024


class _ConnectionPool(Protocol):
    def acquire(self) -> AbstractAsyncContextManager[asyncpg.Connection]: ...


@dataclass(frozen=True, slots=True)
class WahaWebhookHttpRequest:
    method: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class WahaWebhookHttpResponse:
    status: int
    body: dict[str, str]
    headers: Mapping[str, str] | None = None


class WahaWebhookSchemaError(ValueError):
    """The authenticated request is not a supported WAHA webhook document."""


@dataclass(frozen=True, slots=True)
class _IgnoredEvent:
    reason: str


class WahaWebhookHttpIngress:
    """Small authenticated HTTP boundary around the durable WhatsApp inbox.

    The shared secret authenticates the private WAHA-to-application hop. It is
    deliberately distinct from the WAHA REST API key used for outbound calls.
    """

    def __init__(
        self,
        *,
        pool: _ConnectionPool,
        shared_secret: str,
        worker_id: str = "waha-webhook-http",
        max_body_bytes: int = MAX_WEBHOOK_BODY_BYTES,
    ) -> None:
        if not isinstance(shared_secret, str) or not shared_secret.strip():
            raise ValueError("WAHA webhook shared secret must be configured")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if not 1 <= max_body_bytes <= MAX_WEBHOOK_BODY_BYTES:
            raise ValueError(f"max_body_bytes must be between 1 and {MAX_WEBHOOK_BODY_BYTES}")
        self._pool = pool
        self._secret_digest = hashlib.sha256(shared_secret.encode("utf-8")).digest()
        self._worker_id = worker_id.strip()
        self._max_body_bytes = max_body_bytes

    @property
    def max_body_bytes(self) -> int:
        return self._max_body_bytes

    async def handle(self, request: WahaWebhookHttpRequest) -> WahaWebhookHttpResponse:
        method = request.method.upper().strip()
        if method != "POST":
            return WahaWebhookHttpResponse(
                status=405,
                body={"status": "rejected", "error": "method_not_allowed"},
                headers={"Allow": "POST"},
            )
        if len(request.body) > self._max_body_bytes:
            return _error(413, "body_too_large")
        if not self._authenticated(request.headers):
            return _error(401, "authentication_failed")
        if _media_type(request.headers) != "application/json":
            return _error(415, "content_type_must_be_application_json")

        try:
            decoded = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "invalid_json")

        try:
            event = parse_waha_message_event(decoded)
        except WahaWebhookSchemaError:
            return _error(422, "invalid_webhook_schema")

        if isinstance(event, _IgnoredEvent):
            return WahaWebhookHttpResponse(
                status=200,
                body={"status": "ignored", "reason": event.reason},
            )

        try:
            async with self._pool.acquire() as connection:
                result = await accept_whatsapp_inbound(
                    connection,
                    event,
                    worker_id=self._worker_id,
                )
        except (
            asyncpg.InterfaceError,
            asyncpg.PostgresError,
            WhatsAppInboundConflict,
            TimeoutError,
            OSError,
        ):
            return _error(503, "temporarily_unavailable")
        except Exception:  # Defensive HTTP boundary: never expose internals to WAHA.
            return _error(500, "internal_error")

        return WahaWebhookHttpResponse(
            status=200,
            body={"status": "accepted", "disposition": result.disposition.value},
        )

    def _authenticated(self, headers: Mapping[str, str]) -> bool:
        supplied = _header(headers, WAHA_WEBHOOK_AUTH_HEADER) or ""
        supplied_digest = hashlib.sha256(supplied.encode("utf-8")).digest()
        return hmac.compare_digest(supplied_digest, self._secret_digest)


def parse_waha_message_event(value: object) -> AuthenticatedWhatsAppInbound | _IgnoredEvent:
    """Map the narrow WAHA `message` contract into the provider-neutral DTO.

    Unknown event kinds and valid but irrelevant messages are acknowledged and
    ignored. A malformed `message` event is rejected so configuration/schema
    drift is visible instead of silently losing supplier replies.
    """

    if not isinstance(value, Mapping):
        raise WahaWebhookSchemaError("webhook must be an object")
    event_name = value.get("event")
    if not isinstance(event_name, str) or not event_name.strip():
        raise WahaWebhookSchemaError("event must be a non-empty string")
    if event_name != "message":
        return _IgnoredEvent("unsupported_event")

    session = value.get("session")
    payload = value.get("payload")
    if not isinstance(session, str) or not session.strip():
        raise WahaWebhookSchemaError("session must be a non-empty string")
    if not isinstance(payload, Mapping):
        raise WahaWebhookSchemaError("payload must be an object")

    from_me = payload.get("fromMe")
    if not isinstance(from_me, bool):
        raise WahaWebhookSchemaError("payload.fromMe must be boolean")
    if from_me:
        return _IgnoredEvent("own_message")

    sender = payload.get("from")
    if not isinstance(sender, str) or not sender.strip():
        raise WahaWebhookSchemaError("payload.from must be a non-empty string")
    sender = sender.strip()
    if sender == "status@broadcast":
        return _IgnoredEvent("status_message")
    if sender.endswith("@g.us"):
        return _IgnoredEvent("group_message")

    message_type = payload.get("type")
    if message_type is not None and not isinstance(message_type, str):
        raise WahaWebhookSchemaError("payload.type must be a string")
    if message_type not in {None, "chat", "text"}:
        return _IgnoredEvent("non_text_message")

    has_media = payload.get("hasMedia")
    if has_media is not None and not isinstance(has_media, bool):
        raise WahaWebhookSchemaError("payload.hasMedia must be boolean")
    if has_media:
        return _IgnoredEvent("non_text_message")

    body = payload.get("body")
    if body is None:
        return _IgnoredEvent("non_text_message")
    if not isinstance(body, str):
        raise WahaWebhookSchemaError("payload.body must be a string")
    if not body.strip():
        return _IgnoredEvent("empty_text_message")

    message_id = payload.get("id")
    if not isinstance(message_id, str) or not message_id.strip():
        raise WahaWebhookSchemaError("payload.id must be a non-empty string")
    received_at = _parse_timestamp(payload.get("timestamp"))
    sender_phone = _sender_phone(sender)
    if sender_phone is None:
        return _IgnoredEvent("unsupported_sender")

    try:
        return AuthenticatedWhatsAppInbound(
            provider_event_id=message_id.strip(),
            session_name=session.strip(),
            sender_phone=sender_phone,
            provider_message_id=message_id.strip(),
            received_at=received_at,
            text=body,
        )
    except ValueError as exc:
        raise WahaWebhookSchemaError("message fields violate the inbound contract") from exc


def create_waha_webhook_app(
    *,
    pool: _ConnectionPool,
    shared_secret: str,
    worker_id: str = "waha-webhook-http",
    path: str = WAHA_WEBHOOK_PATH,
) -> web.Application:
    """Build an aiohttp application; process startup/CLI remains the caller's job."""

    if not path.startswith("/"):
        raise ValueError("webhook path must be absolute")
    ingress = WahaWebhookHttpIngress(
        pool=pool,
        shared_secret=shared_secret,
        worker_id=worker_id,
    )
    # aiohttp rejects when the accumulated size reaches client_max_size, while
    # our public contract permits a body exactly equal to the configured limit.
    app = web.Application(client_max_size=ingress.max_body_bytes + 1)

    async def endpoint(request: web.Request) -> web.Response:
        if request.content_length is not None and request.content_length > ingress.max_body_bytes:
            response = _error(413, "body_too_large")
        else:
            try:
                body = await request.read()
            except web.HTTPRequestEntityTooLarge:
                response = _error(413, "body_too_large")
            else:
                response = await ingress.handle(
                    WahaWebhookHttpRequest(
                        method=request.method,
                        headers=request.headers,
                        body=body,
                    )
                )
        return web.json_response(
            response.body,
            status=response.status,
            headers=response.headers,
        )

    app.router.add_route("*", path, endpoint)
    return app


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, bool):
        raise WahaWebhookSchemaError("payload.timestamp must be a Unix or ISO timestamp")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds) or seconds <= 0:
            raise WahaWebhookSchemaError("payload.timestamp is outside the supported range")
        if seconds >= 100_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise WahaWebhookSchemaError(
                "payload.timestamp is outside the supported range"
            ) from exc
    if isinstance(value, str) and value.strip():
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise WahaWebhookSchemaError("payload.timestamp is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise WahaWebhookSchemaError("payload.timestamp must include a timezone")
        return parsed.astimezone(UTC)
    raise WahaWebhookSchemaError("payload.timestamp must be a Unix or ISO timestamp")


def _sender_phone(sender: str) -> str | None:
    if sender.endswith("@c.us"):
        candidate = sender.removesuffix("@c.us")
    elif sender.endswith("@s.whatsapp.net"):
        candidate = sender.removesuffix("@s.whatsapp.net")
    elif "@" not in sender:
        candidate = sender
    else:
        return None
    try:
        return normalize_phone(candidate)
    except ValueError:
        return None


def _media_type(headers: Mapping[str, str]) -> str:
    return (_header(headers, "Content-Type") or "").partition(";")[0].strip().casefold()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    expected = name.casefold()
    for key, value in headers.items():
        if key.casefold() == expected:
            return value
    return None


def _error(status: int, code: str) -> WahaWebhookHttpResponse:
    return WahaWebhookHttpResponse(
        status=status,
        body={"status": "rejected", "error": code},
    )
