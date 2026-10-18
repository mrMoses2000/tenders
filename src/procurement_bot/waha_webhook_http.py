from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

import asyncpg
from aiohttp import web

from procurement_bot.phone import normalize_phone
from procurement_bot.supplier_media import WhatsAppMediaInput
from procurement_bot.whatsapp_inbound import (
    AuthenticatedWhatsAppInbound,
    WhatsAppInboundConflict,
    accept_whatsapp_inbound,
)
from procurement_bot.whatsapp_ingress import (
    AuthenticatedLidQuarantine,
    persist_authenticated_lid_quarantine,
    persist_ingress_disposition,
    provider_id_fingerprint,
)
from procurement_bot.whatsapp_ingress import (
    event_kind as canonical_event_kind,
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


@dataclass(frozen=True, slots=True)
class WahaMessageParseOutcome:
    """Privacy-safe summary for history reconciliation and ingress auditing."""

    event: AuthenticatedWhatsAppInbound | None
    disposition: str
    reason: str
    event_kind: str
    provider_id_fingerprint: str | None
    session_name: str | None = None
    restriction_state: Literal["timelock", "capped"] | None = None
    restriction_until: datetime | None = None


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
        known_sessions: Sequence[str] = (),
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
        if (
            isinstance(known_sessions, str)
            or len(known_sessions) > 10
            or any(
                not isinstance(session, str) or not session.strip() or len(session) > 200
                for session in known_sessions
            )
        ):
            raise ValueError("known_sessions must contain at most 10 valid names")
        self._known_sessions = frozenset(session.strip() for session in known_sessions)

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
            if not await self._audit("rejected", "body_too_large", "unknown"):
                return _error(503, "temporarily_unavailable")
            return _error(413, "body_too_large")
        if not self._authenticated(request.headers):
            if not await self._audit("rejected", "authentication_failed", "unknown"):
                return _error(503, "temporarily_unavailable")
            return _error(401, "authentication_failed")
        if _media_type(request.headers) != "application/json":
            if not await self._audit(
                "rejected", "content_type_must_be_application_json", "unknown"
            ):
                return _error(503, "temporarily_unavailable")
            return _error(415, "content_type_must_be_application_json")

        try:
            decoded = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if not await self._audit("rejected", "invalid_json", "unknown"):
                return _error(503, "temporarily_unavailable")
            return _error(400, "invalid_json")

        if isinstance(decoded, dict) and decoded.get("event") == "message.ack":
            from procurement_bot.delivery_receipts import parse_receipt, record_receipt

            try:
                receipt = parse_receipt(decoded)
            except ValueError:
                if not await self._audit(
                    "rejected",
                    "invalid_receipt_schema",
                    "message.ack",
                    _payload_id_fingerprint(decoded),
                ):
                    return _error(503, "temporarily_unavailable")
                return _error(422, "invalid_receipt_schema")
            try:
                async with self._pool.acquire() as connection:
                    matched = await record_receipt(connection, receipt)
            except (asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError, OSError):
                return _error(503, "temporarily_unavailable")
            except Exception:
                return _error(500, "internal_error")
            if not await self._audit(
                "accepted",
                "receipt_recorded",
                "message.ack",
                provider_id_fingerprint(receipt.external_message_id),
            ):
                return _error(503, "temporarily_unavailable")
            return WahaWebhookHttpResponse(
                status=200, body={"status": "accepted", "matched": matched},
            )

        outcome = parse_waha_message_outcome(decoded)
        if outcome.disposition == "rejected":
            if not await self._audit(
                "rejected", outcome.reason, outcome.event_kind,
                outcome.provider_id_fingerprint,
            ):
                return _error(503, "temporarily_unavailable")
            return _error(422, "invalid_webhook_schema")

        if outcome.event_kind == "session.status" and outcome.disposition == "accepted":
            session_name = outcome.session_name
            if session_name not in self._known_sessions:
                if not await self._audit(
                    "ignored", "unknown_session_status", "session.status"
                ):
                    return _error(503, "temporarily_unavailable")
                return WahaWebhookHttpResponse(
                    status=200,
                    body={"status": "ignored", "reason": "unknown_session_status"},
                )
            assert session_name is not None
            try:
                from procurement_bot.pipeline_runtime import wake_session_pipelines
                from procurement_bot.whatsapp_account import (
                    observe_provider_state,
                    request_account_probe,
                )

                async with self._pool.acquire() as connection:
                    if outcome.restriction_state is not None:
                        await observe_provider_state(
                            connection,
                            session_name,
                            state=outcome.restriction_state,
                            observed_at=datetime.now(UTC),
                            source="webhook_hint",
                            reason="provider_session_status_restriction_hint",
                            restriction_until=outcome.restriction_until,
                        )
                    await request_account_probe(
                        connection,
                        session_name,
                        reason="provider_session_status_webhook_hint",
                    )
                    await wake_session_pipelines(
                        connection,
                        session_name=session_name,
                        reason="whatsapp_session_status_hint",
                    )
            except (asyncpg.InterfaceError, asyncpg.PostgresError, TimeoutError, OSError):
                return _error(503, "temporarily_unavailable")
            except Exception:
                return _error(500, "internal_error")
            reason = (
                "restriction_hint_recorded"
                if outcome.restriction_state is not None
                else "probe_requested"
            )
            if not await self._audit("accepted", reason, "session.status"):
                return _error(503, "temporarily_unavailable")
            return WahaWebhookHttpResponse(
                status=200,
                body={"status": "accepted", "disposition": reason},
            )

        if outcome.event is None:
            quarantine = (
                _lid_quarantine_from(decoded)
                if outcome.reason == "unsupported_sender"
                else None
            )
            if not await self._audit(
                "ignored", outcome.reason, outcome.event_kind,
                outcome.provider_id_fingerprint, quarantine=quarantine,
            ):
                return _error(503, "temporarily_unavailable")
            return WahaWebhookHttpResponse(
                status=200,
                body={"status": "ignored", "reason": outcome.reason},
            )

        event = outcome.event
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

        result_reason = result.disposition.value
        disposition = (
            "ignored"
            if result.disposition.value.startswith("quarantined_")
            else "accepted"
        )
        if not await self._audit(
            disposition, result_reason, outcome.event_kind,
            outcome.provider_id_fingerprint,
        ):
            return _error(503, "temporarily_unavailable")

        return WahaWebhookHttpResponse(
            status=200,
            body={"status": "accepted", "disposition": result.disposition.value},
        )

    async def _audit(
        self,
        disposition: str,
        reason: str,
        kind: str,
        fingerprint: str | None = None,
        *,
        quarantine: AuthenticatedLidQuarantine | None = None,
    ) -> bool:
        try:
            async with self._pool.acquire() as connection:
                if quarantine is not None and not await persist_authenticated_lid_quarantine(
                    connection, quarantine
                ):
                    await persist_ingress_disposition(
                        connection,
                        disposition="ignored",
                        reason_code="quarantine_capacity_exceeded",
                        kind=kind,
                        provider_fingerprint=fingerprint,
                    )
                    return False
                await persist_ingress_disposition(
                    connection,
                    disposition=disposition,  # type: ignore[arg-type]
                    reason_code=reason,
                    kind=kind,
                    provider_fingerprint=fingerprint,
                )
        except Exception:
            return False
        return True

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
    has_media = payload.get("hasMedia")
    if has_media is not None and not isinstance(has_media, bool):
        raise WahaWebhookSchemaError("payload.hasMedia must be boolean")
    body = payload.get("body")
    if body is not None and not isinstance(body, str):
        raise WahaWebhookSchemaError("payload.body must be a string")
    if body is None:
        body = ""

    media: WhatsAppMediaInput | None = None
    media_value = payload.get("media")
    is_media = (
        bool(has_media)
        or isinstance(media_value, Mapping)
        or message_type in {"image", "audio", "voice", "ptt"}
    )
    if is_media:
        if media_value is not None and not isinstance(media_value, Mapping):
            raise WahaWebhookSchemaError("payload.media must be an object or null")
        media_fields = media_value if isinstance(media_value, Mapping) else {}
        mimetype = media_fields.get("mimetype")
        filename = media_fields.get("filename")
        media_url = media_fields.get("url")
        media_error = media_fields.get("error")
        for field, field_value in (
            ("mimetype", mimetype),
            ("filename", filename),
            ("url", media_url),
            ("error", media_error),
        ):
            if field_value is not None and not isinstance(field_value, str):
                raise WahaWebhookSchemaError(f"payload.media.{field} must be a string or null")
        mime_kind = (
            "image"
            if isinstance(mimetype, str) and mimetype.lower().startswith("image/")
            else "audio"
            if isinstance(mimetype, str) and mimetype.lower().startswith("audio/")
            else None
        )
        type_kind = (
            "image"
            if message_type == "image"
            else "audio"
            if message_type in {"audio", "voice", "ptt"}
            else None
        )
        kind = mime_kind or type_kind
        if kind is None:
            return _IgnoredEvent("unsupported_media_type")
        try:
            media = WhatsAppMediaInput(
                kind=kind,
                url=media_url,
                mimetype=mimetype,
                filename=filename,
                error=media_error,
            )
        except ValueError as exc:
            raise WahaWebhookSchemaError("media fields violate the inbound contract") from exc
    elif message_type not in {None, "chat", "text"}:
        return _IgnoredEvent("non_text_message")
    elif not body.strip():
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
            media=media,
        )
    except ValueError as exc:
        raise WahaWebhookSchemaError("message fields violate the inbound contract") from exc


def parse_waha_message_outcome(value: object) -> WahaMessageParseOutcome:
    """Expose parser disposition and a redacted id fingerprint to reconciliation.

    Ignored and rejected outcomes contain no raw provider id, sender, text, or
    media URL. A supported outcome includes the normalized private inbound DTO
    so the caller can pass it into the durable ledger.
    """

    kind = canonical_event_kind(value.get("event") if isinstance(value, Mapping) else None)
    if kind == "session.status":
        return _session_status_outcome(value)
    payload = value.get("payload") if isinstance(value, Mapping) else None
    candidate_id = payload.get("id") if isinstance(payload, Mapping) else None
    fingerprint = provider_id_fingerprint(candidate_id)
    try:
        parsed = parse_waha_message_event(value)
    except WahaWebhookSchemaError:
        return WahaMessageParseOutcome(
            event=None,
            disposition="rejected",
            reason="invalid_webhook_schema",
            event_kind=kind,
            provider_id_fingerprint=fingerprint,
        )
    if isinstance(parsed, _IgnoredEvent):
        return WahaMessageParseOutcome(
            event=None,
            disposition="ignored",
            reason=parsed.reason,
            event_kind=kind,
            provider_id_fingerprint=fingerprint,
        )
    return WahaMessageParseOutcome(
        event=parsed,
        disposition="accepted",
        reason="parsed",
        event_kind=kind,
        provider_id_fingerprint=fingerprint,
    )


def _payload_id_fingerprint(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    payload = value.get("payload")
    return provider_id_fingerprint(
        payload.get("id") if isinstance(payload, Mapping) else None
    )


def _session_status_outcome(value: object) -> WahaMessageParseOutcome:
    if not isinstance(value, Mapping):
        return WahaMessageParseOutcome(
            None, "rejected", "invalid_webhook_schema", "session.status", None
        )
    session_name = value.get("session")
    payload = value.get("payload")
    if (
        not isinstance(session_name, str)
        or not session_name.strip()
        or len(session_name.strip()) > 200
        or not isinstance(payload, Mapping)
        or not isinstance(payload.get("status"), str)
        or not payload.get("status", "").strip()
    ):
        return WahaMessageParseOutcome(
            None, "rejected", "invalid_webhook_schema", "session.status", None
        )
    data = payload.get("data")
    if data is not None and not isinstance(data, Mapping):
        return WahaMessageParseOutcome(
            None, "rejected", "invalid_webhook_schema", "session.status", None
        )
    data = data if isinstance(data, Mapping) else {}
    restriction_state: Literal["timelock", "capped"] | None = None
    restriction_until: datetime | None = None
    timelock = data.get("reachoutTimelock")
    if timelock is not None:
        if not isinstance(timelock, Mapping):
            return WahaMessageParseOutcome(
                None, "rejected", "invalid_webhook_schema", "session.status", None
            )
        active = timelock.get("isActive")
        end = timelock.get("timeEnforcementEnds")
        if not isinstance(active, bool) or (
            end is not None
            and (
                isinstance(end, bool)
                or not isinstance(end, (int, float))
                or not math.isfinite(float(end))
                or float(end) <= 0
            )
        ):
            return WahaMessageParseOutcome(
                None, "rejected", "invalid_webhook_schema", "session.status", None
            )
        if end is not None:
            try:
                restriction_until = datetime.fromtimestamp(float(end), tz=UTC)
            except (OverflowError, OSError, ValueError):
                return WahaMessageParseOutcome(
                    None, "rejected", "invalid_webhook_schema", "session.status", None
                )
        if active:
            restriction_state = "timelock"
    capping = data.get("messageCapping")
    if capping is not None:
        if not isinstance(capping, Mapping):
            return WahaMessageParseOutcome(
                None, "rejected", "invalid_webhook_schema", "session.status", None
            )
        capping_status = capping.get("cappingStatus")
        if capping_status is not None and not isinstance(capping_status, str):
            return WahaMessageParseOutcome(
                None, "rejected", "invalid_webhook_schema", "session.status", None
            )
        if (
            restriction_state is None
            and isinstance(capping_status, str)
            and capping_status.strip().upper() == "CAPPED"
        ):
            restriction_state = "capped"
    return WahaMessageParseOutcome(
        event=None,
        disposition="accepted",
        reason="session_status_hint",
        event_kind="session.status",
        provider_id_fingerprint=None,
        session_name=session_name.strip(),
        restriction_state=restriction_state,
        restriction_until=restriction_until if restriction_state == "timelock" else None,
    )


def _lid_quarantine_from(value: object) -> AuthenticatedLidQuarantine | None:
    """Build a strict allowlist record only after the authenticated parser accepted shape."""

    if not isinstance(value, Mapping):
        return None
    payload = value.get("payload")
    session = value.get("session")
    if not isinstance(payload, Mapping) or not isinstance(session, str):
        return None
    sender = payload.get("from")
    provider_id = payload.get("id")
    body = payload.get("body")
    if (
        not isinstance(sender, str)
        or not sender.endswith("@lid")
        or not isinstance(provider_id, str)
        or not isinstance(body, str)
    ):
        return None
    try:
        timestamp = _parse_timestamp(payload.get("timestamp"))
    except WahaWebhookSchemaError:
        return None
    media_value = payload.get("media")
    media = media_value if isinstance(media_value, Mapping) else {}
    mime = media.get("mimetype")
    message_type = payload.get("type")
    if (
        isinstance(mime, str) and mime.casefold().startswith("image/")
    ) or message_type == "image":
        kind = "image"
    elif (
        isinstance(mime, str) and mime.casefold().startswith("audio/")
    ) or message_type in {"audio", "voice", "ptt"}:
        kind = "audio"
    elif message_type in {None, "chat", "text"}:
        kind = "text"
    else:
        kind = "other"
    try:
        return AuthenticatedLidQuarantine(
            session_name=session.strip(),
            provider_event_id=provider_id.strip(),
            sender_jid=sender.strip(),
            received_at=timestamp,
            message_kind=kind,
            text_content=body,
        )
    except ValueError:
        return None


def create_waha_webhook_app(
    *,
    pool: _ConnectionPool,
    shared_secret: str,
    worker_id: str = "waha-webhook-http",
    path: str = WAHA_WEBHOOK_PATH,
    known_sessions: Sequence[str] = (),
) -> web.Application:
    """Build an aiohttp application; process startup/CLI remains the caller's job."""

    if not path.startswith("/"):
        raise ValueError("webhook path must be absolute")
    ingress = WahaWebhookHttpIngress(
        pool=pool,
        shared_secret=shared_secret,
        worker_id=worker_id,
        known_sessions=known_sessions,
    )
    # aiohttp rejects when the accumulated size reaches client_max_size, while
    # our public contract permits a body exactly equal to the configured limit.
    app = web.Application(client_max_size=ingress.max_body_bytes + 1)

    async def endpoint(request: web.Request) -> web.Response:
        if request.content_length is not None and request.content_length > ingress.max_body_bytes:
            response = await ingress.handle(
                WahaWebhookHttpRequest(
                    method=request.method,
                    headers=request.headers,
                    body=b"x" * (ingress.max_body_bytes + 1),
                )
            )
        else:
            try:
                body = await request.read()
            except web.HTTPRequestEntityTooLarge:
                response = await ingress.handle(
                    WahaWebhookHttpRequest(
                        method=request.method,
                        headers=request.headers,
                        body=b"x" * (ingress.max_body_bytes + 1),
                    )
                )
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
