from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

import procurement_bot.waha_webhook_http as webhook_module
from procurement_bot.waha_webhook_http import (
    MAX_WEBHOOK_BODY_BYTES,
    WAHA_WEBHOOK_AUTH_HEADER,
    WahaWebhookHttpIngress,
    WahaWebhookHttpRequest,
    create_waha_webhook_app,
    parse_waha_message_event,
)
from procurement_bot.whatsapp_inbound import (
    WhatsAppInboundDisposition,
    WhatsAppInboundResult,
)

SHARED_AUTH_VALUE = "a-dedicated-private-webhook-secret"  # noqa: S105
INVALID_AUTH_VALUE = "wrong"


class _Acquire(AbstractAsyncContextManager[object]):
    def __init__(self, connection: object) -> None:
        self.connection = connection

    async def __aenter__(self) -> object:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.connection = object()
        self.acquire_count = 0

    def acquire(self) -> _Acquire:
        self.acquire_count += 1
        return _Acquire(self.connection)


def _document(**payload_overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "msg-42",
        "timestamp": 1_795_008_600,
        "from": "77001234567@c.us",
        "fromMe": False,
        "body": "Есть в наличии",
        "type": "chat",
        "hasMedia": False,
    }
    payload.update(payload_overrides)
    return {"event": "message", "session": "procurement", "payload": payload}


def _request(
    document: object | None = None,
    *,
    secret: str = SHARED_AUTH_VALUE,
    content_type: str = "application/json; charset=utf-8",
    method: str = "POST",
) -> WahaWebhookHttpRequest:
    return WahaWebhookHttpRequest(
        method=method,
        headers={
            WAHA_WEBHOOK_AUTH_HEADER: secret,
            "Content-Type": content_type,
        },
        body=json.dumps(_document() if document is None else document).encode(),
    )


def test_configuration_requires_a_secret_and_bounded_limit() -> None:
    pool = _Pool()
    with pytest.raises(ValueError, match="shared secret"):
        WahaWebhookHttpIngress(pool=pool, shared_secret="")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_body_bytes"):
        WahaWebhookHttpIngress(  # type: ignore[arg-type]
            pool=pool,
            shared_secret=SHARED_AUTH_VALUE,
            max_body_bytes=MAX_WEBHOOK_BODY_BYTES + 1,
        )


@pytest.mark.asyncio
async def test_valid_message_is_normalized_and_durably_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    captured: dict[str, Any] = {}

    async def accept(connection: object, event: object, *, worker_id: str) -> object:
        captured.update(connection=connection, event=event, worker_id=worker_id)
        return WhatsAppInboundResult(
            webhook_event_id=uuid4(),
            disposition=WhatsAppInboundDisposition.PROCESSED,
        )

    monkeypatch.setattr(webhook_module, "accept_whatsapp_inbound", accept)
    ingress = WahaWebhookHttpIngress(  # type: ignore[arg-type]
        pool=pool,
        shared_secret=SHARED_AUTH_VALUE,
        worker_id="http-1",
    )

    response = await ingress.handle(_request())

    assert response.status == 200
    assert response.body == {"status": "accepted", "disposition": "processed"}
    event = captured["event"]
    assert event.provider_event_id == "msg-42"
    assert event.provider_message_id == "msg-42"
    assert event.session_name == "procurement"
    assert event.sender_phone == "+77001234567"
    assert event.received_at == datetime.fromtimestamp(1_795_008_600, tz=UTC)
    assert captured["connection"] is pool.connection
    assert captured["worker_id"] == "http-1"
    assert pool.acquire_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ({"event": "session.status", "session": "procurement", "payload": {}}, "unsupported_event"),
        (_document(fromMe=True), "own_message"),
        (_document(**{"from": "status@broadcast"}), "status_message"),
        (_document(type="image", hasMedia=True), "non_text_message"),
        (_document(**{"from": "120363@g.us"}), "group_message"),
    ],
)
async def test_irrelevant_provider_events_are_acknowledged_without_database_access(
    document: object,
    reason: str,
) -> None:
    pool = _Pool()
    ingress = WahaWebhookHttpIngress(  # type: ignore[arg-type]
        pool=pool, shared_secret=SHARED_AUTH_VALUE
    )

    response = await ingress.handle(_request(document))

    assert response.status == 200
    assert response.body == {"status": "ignored", "reason": reason}
    assert pool.acquire_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("http_request", "status", "error"),
    [
        (_request(method="GET"), 405, "method_not_allowed"),
        (_request(secret=INVALID_AUTH_VALUE), 401, "authentication_failed"),
        (_request(content_type="text/plain"), 415, "content_type_must_be_application_json"),
        (
            WahaWebhookHttpRequest(
                method="POST",
                headers={
                    WAHA_WEBHOOK_AUTH_HEADER: SHARED_AUTH_VALUE,
                    "Content-Type": "application/json",
                },
                body=b"{",
            ),
            400,
            "invalid_json",
        ),
        (_request(_document(fromMe="false")), 422, "invalid_webhook_schema"),
    ],
)
async def test_request_boundary_rejects_unsafe_input_without_database_access(
    http_request: WahaWebhookHttpRequest,
    status: int,
    error: str,
) -> None:
    pool = _Pool()
    ingress = WahaWebhookHttpIngress(  # type: ignore[arg-type]
        pool=pool, shared_secret=SHARED_AUTH_VALUE
    )

    response = await ingress.handle(http_request)

    assert response.status == status
    assert response.body["error"] == error
    assert pool.acquire_count == 0


@pytest.mark.asyncio
async def test_body_limit_is_enforced_before_authentication_or_json_parsing() -> None:
    pool = _Pool()
    ingress = WahaWebhookHttpIngress(  # type: ignore[arg-type]
        pool=pool, shared_secret=SHARED_AUTH_VALUE
    )
    request = WahaWebhookHttpRequest(method="POST", headers={}, body=b"x" * (256 * 1024 + 1))

    response = await ingress.handle(request)

    assert response.status == 413
    assert response.body["error"] == "body_too_large"
    assert pool.acquire_count == 0


@pytest.mark.asyncio
async def test_database_failure_requests_provider_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(webhook_module, "accept_whatsapp_inbound", fail)
    ingress = WahaWebhookHttpIngress(  # type: ignore[arg-type]
        pool=pool, shared_secret=SHARED_AUTH_VALUE
    )

    response = await ingress.handle(_request())

    assert response.status == 503
    assert response.body == {"status": "rejected", "error": "temporarily_unavailable"}


def test_timestamp_seconds_milliseconds_and_iso_are_normalized_to_utc() -> None:
    seconds = parse_waha_message_event(_document(timestamp=1_795_008_600))
    milliseconds = parse_waha_message_event(_document(timestamp=1_795_008_600_000))
    iso = parse_waha_message_event(_document(timestamp="2026-11-16T10:30:00+05:00"))

    assert seconds.received_at == milliseconds.received_at  # type: ignore[union-attr]
    assert iso.received_at == datetime(2026, 11, 16, 5, 30, tzinfo=UTC)  # type: ignore[union-attr]


def test_aiohttp_factory_exposes_only_configured_webhook_route() -> None:
    app = create_waha_webhook_app(  # type: ignore[arg-type]
        pool=_Pool(), shared_secret=SHARED_AUTH_VALUE
    )
    resources = list(app.router.resources())

    assert len(resources) == 1
    assert resources[0].canonical == "/webhooks/waha"
