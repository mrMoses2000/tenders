from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.waha import (
    HttpResponse,
    WahaClient,
    WahaDisabledError,
    normalize_chat_id,
)


class RecordingTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.response


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 700 123-45-67", "77001234567@c.us"),
        ("77001234567@c.us", "77001234567@c.us"),
        ("87001234567", "77001234567@c.us"),
        ("+1 (202) 555-0114", "12025550114@c.us"),
    ],
)
def test_normalize_chat_id_accepts_international_phone_formats(
    raw: str, expected: str
) -> None:
    assert normalize_chat_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "12345", "supplier", "+7 700 ext 2"])
def test_normalize_chat_id_rejects_ambiguous_or_non_international_values(raw: str) -> None:
    with pytest.raises(ValueError, match="international phone"):
        normalize_chat_id(raw)


@pytest.mark.asyncio
async def test_waha_is_fail_closed_when_disabled() -> None:
    transport = RecordingTransport(HttpResponse(200, b'{"id":"not-used"}'))
    client = WahaClient(
        base_url="http://localhost:3000",
        api_key="secret",
        enabled=False,
        transport=transport,
    )

    with pytest.raises(WahaDisabledError, match="disabled"):
        await client.send_text(
            recipient="+77001234567",
            text="Здравствуйте",
            idempotency_key="message-1",
            provider_message_id="reserved-1",
        )
    assert transport.requests == []


@pytest.mark.asyncio
async def test_waha_sends_normalized_payload_and_preserves_idempotency_key() -> None:
    transport = RecordingTransport(HttpResponse(200, b'{"_data":{"id":"wa-123"}}'))
    client = WahaClient(
        base_url="http://localhost:3000/root/",
        api_key="secret",
        session="procurement",
        enabled=True,
        timeout_seconds=3,
        transport=transport,
    )

    result = await client.send_text(
        recipient="+7 (700) 123-45-67",
        text="  Здравствуйте  ",
        idempotency_key="outbox-42",
        provider_message_id="reserved-42",
    )

    assert result.external_message_id == "wa-123"
    recorded = transport.requests[0]
    assert recorded["url"] == "http://localhost:3000/root/api/sendText"
    assert recorded["headers"]["X-Idempotency-Key"] == "outbox-42"  # type: ignore[index]
    assert json.loads(recorded["body"]) == {  # type: ignore[arg-type]
        "session": "procurement",
        "chatId": "77001234567@c.us",
        "text": "Здравствуйте",
        "id": "reserved-42",
    }


@pytest.mark.asyncio
async def test_waha_reserves_provider_message_id_before_send() -> None:
    transport = RecordingTransport(HttpResponse(200, b'{"id":"reserved-42"}'))
    client = WahaClient(
        base_url="http://localhost:3000",
        api_key="secret",
        session="procurement",
        enabled=True,
        transport=transport,
    )

    assert await client.new_message_id() == "reserved-42"
    assert transport.requests[0]["method"] == "GET"
    assert transport.requests[0]["url"].endswith(
        "/api/procurement/new-message-id"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_waha_classifies_temporary_http_statuses_as_retryable(status: int) -> None:
    transport = RecordingTransport(HttpResponse(status, b"upstream unavailable"))
    client = WahaClient(
        base_url="http://localhost:3000",
        api_key="secret",
        enabled=True,
        transport=transport,
    )

    with pytest.raises(RetryableProviderError):
        await client.send_text(
            recipient="+77001234567",
            text="Здравствуйте",
            idempotency_key="message-1",
            provider_message_id="reserved-1",
        )


@pytest.mark.asyncio
async def test_waha_classifies_client_rejection_as_permanent() -> None:
    transport = RecordingTransport(HttpResponse(400, b'{"error":"invalid chat"}'))
    client = WahaClient(
        base_url="http://localhost:3000",
        api_key="secret",
        enabled=True,
        transport=transport,
    )

    with pytest.raises(PermanentProviderError):
        await client.send_text(
            recipient="+77001234567",
            text="Здравствуйте",
            idempotency_key="message-1",
            provider_message_id="reserved-1",
        )
