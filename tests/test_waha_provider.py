from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
        self.downloads: list[dict[str, object]] = []

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

    async def download(
        self,
        *,
        url: str,
        destination: Path,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> None:
        self.downloads.append(
            {
                "url": url,
                "destination": destination,
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
                "max_bytes": max_bytes,
            }
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"media")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 700 000-00-03", "77000000003@c.us"),
        ("77000000003@c.us", "77000000003@c.us"),
        ("87000000003", "77000000003@c.us"),
        ("+1 (202) 555-0114", "12025550114@c.us"),
    ],
)
def test_normalize_chat_id_accepts_international_phone_formats(raw: str, expected: str) -> None:
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
            recipient="+77000000003",
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
        recipient="+7 (700) 000-00-03",
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
        "chatId": "77000000003@c.us",
        "text": "Здравствуйте",
        "id": "reserved-42",
    }


@pytest.mark.asyncio
async def test_waha_uses_reserved_id_when_success_response_omits_message_id() -> None:
    transport = RecordingTransport(HttpResponse(201, b"{}"))
    client = WahaClient(
        base_url="http://localhost:3000",
        api_key="secret",
        enabled=True,
        transport=transport,
    )
    result = await client.send_text(
        recipient="+77000000003",
        text="Тест",
        idempotency_key="outbox-42",
        provider_message_id="reserved-42",
    )
    assert result.external_message_id == "true_77000000003@c.us_reserved-42"
    assert result.raw == {}


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
    assert transport.requests[0]["url"].endswith("/api/procurement/new-message-id")


@pytest.mark.asyncio
async def test_waha_session_422_is_retryable_before_send() -> None:
    transport = RecordingTransport(HttpResponse(422, b'{"status":"FAILED"}'))
    client = WahaClient(
        base_url="http://localhost:3000",
        api_key="secret",
        enabled=True,
        transport=transport,
    )
    with pytest.raises(RetryableProviderError, match="not ready"):
        await client.new_message_id()


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
            recipient="+77000000003",
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
            recipient="+77000000003",
            text="Здравствуйте",
            idempotency_key="message-1",
            provider_message_id="reserved-1",
        )


@pytest.mark.asyncio
async def test_fetch_chat_messages_is_read_only_bounded_and_url_encodes_chat_id() -> None:
    transport = RecordingTransport(HttpResponse(200, b'[{"id":"m1"},{"id":"m2"}]'))
    client = WahaClient(
        base_url="http://localhost:3000",
        api_key="secret",
        session="procurement",
        enabled=True,
        transport=transport,
    )

    messages = await client.fetch_chat_messages("120@c.us?x", limit=10, download_media=False)

    assert messages == [{"id": "m1"}, {"id": "m2"}]
    request = transport.requests[0]
    assert request["method"] == "GET"
    assert "/chats/120%40c.us%3Fx/messages?" in request["url"]
    assert "limit=10" in request["url"]
    assert "downloadMedia=false" in request["url"]
    assert "X-Api-Key" in request["headers"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_media_download_sends_credentials_only_to_validated_waha_file_url(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(HttpResponse(200, b"{}"))
    client = WahaClient(
        base_url="http://127.0.0.1:3000",
        api_key="secret",
        enabled=True,
        transport=transport,
    )
    destination = tmp_path / "nested" / "message.ogg"

    await client.download_media(
        "http://localhost:3000/api/files/message.ogg",
        destination,
        max_bytes=128,
    )

    assert destination.read_bytes() == b"media"
    assert transport.downloads[0]["headers"]["X-Api-Key"] == "secret"  # type: ignore[index]
    assert transport.downloads[0]["max_bytes"] == 128


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/api/files/message.ogg",
        "http://127.0.0.1:3001/api/files/message.ogg",
        "http://127.0.0.1:3000/api/contacts/message.ogg",
        "http://127.0.0.1:3000/api/files/%2e%2e/sendText",
        "http://user:pass@127.0.0.1:3000/api/files/message.ogg",
    ],
)
async def test_media_download_rejects_untrusted_url_before_transport_call(
    tmp_path: Path,
    url: str,
) -> None:
    transport = RecordingTransport(HttpResponse(200, b"{}"))
    client = WahaClient(
        base_url="http://127.0.0.1:3000",
        api_key="secret",
        enabled=True,
        transport=transport,
    )

    with pytest.raises(PermanentProviderError):
        await client.download_media(url, tmp_path / "file", max_bytes=128)
    assert transport.downloads == []


@pytest.mark.asyncio
async def test_media_download_rejects_limits_above_twenty_mebibytes(
    tmp_path: Path,
) -> None:
    transport = RecordingTransport(HttpResponse(200, b"{}"))
    client = WahaClient(
        base_url="http://127.0.0.1:3000", api_key="secret", enabled=True, transport=transport
    )

    with pytest.raises(ValueError, match="max_bytes"):
        await client.download_media(
            "http://127.0.0.1:3000/api/files/file",
            tmp_path / "file",
            max_bytes=20 * 1024 * 1024 + 1,
        )


@pytest.mark.asyncio
async def test_default_download_stream_enforces_size_limit_and_cleans_partial_file(
    tmp_path: Path,
) -> None:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            hits.append(self.path)
            if self.path.endswith("redirect"):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leak")
                self.end_headers()
                return
            if self.path == "/leak":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"should-not-be-requested")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"too-large")

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = WahaClient(
        base_url=f"http://127.0.0.1:{server.server_port}",
        api_key="secret",
        enabled=True,
    )
    try:
        with pytest.raises(PermanentProviderError, match="size limit"):
            await client.download_media(
                f"http://127.0.0.1:{server.server_port}/api/files/large",
                tmp_path / "large.bin",
                max_bytes=3,
            )
        assert not (tmp_path / "large.bin").exists()
        assert not list(tmp_path.glob(".media-*"))

        with pytest.raises(PermanentProviderError, match="redirects"):
            await client.download_media(
                f"http://127.0.0.1:{server.server_port}/api/files/redirect",
                tmp_path / "redirect.bin",
                max_bytes=64,
            )
        assert "/leak" not in hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_default_download_enforces_whole_transfer_deadline_on_slow_stream(
    tmp_path: Path,
) -> None:
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            for _ in range(5):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except BrokenPipeError:
                    return
                time.sleep(0.12)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = WahaClient(
        base_url=f"http://127.0.0.1:{server.server_port}",
        api_key="secret",
        enabled=True,
        timeout_seconds=0.3,
    )
    try:
        with pytest.raises(RetryableProviderError):
            await client.download_media(
                f"http://127.0.0.1:{server.server_port}/api/files/slow",
                tmp_path / "slow.bin",
                max_bytes=128,
            )
        assert not (tmp_path / "slow.bin").exists()
        assert not list(tmp_path.glob(".media-*"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_message_lookup_is_read_only_and_preserves_unavailable_evidence():
    transport = RecordingTransport(HttpResponse(status=404, body=b"{}"))
    client = WahaClient(
        base_url="http://127.0.0.1:3000",
        api_key="test-marker",
        session="test",
        enabled=True,
        transport=transport,
    )
    assert await client.fetch_message("77000000003@c.us", "sent/1") is None
    request = transport.requests[-1]
    assert request["method"] == "GET" and request["body"] is None
    assert "/sent%2F1?downloadMedia=false" in request["url"]
    transport.response = HttpResponse(status=200, body=b'{"id":"sent/1","ack":2,"fromMe":true}')
    assert (await client.fetch_message("77000000003@c.us", "sent/1"))["ack"] == 2
    transport.response = HttpResponse(status=429, body=b"{}")
    with pytest.raises(RetryableProviderError):
        await client.fetch_message("77000000003@c.us", "sent/1")


@pytest.mark.asyncio
async def test_session_status_is_read_only_and_requires_provider_status():
    transport = RecordingTransport(HttpResponse(status=200, body=b'{"status":"FAILED"}'))
    client = WahaClient(
        base_url="http://127.0.0.1:3000",
        api_key="test-marker",
        session="test",
        enabled=True,
        transport=transport,
    )
    assert await client.get_session_status() == "FAILED"
    request = transport.requests[-1]
    assert request["method"] == "GET" and request["body"] is None
    assert request["url"].endswith("api/sessions/test")
    transport.response = HttpResponse(status=200, body=b"{}")
    with pytest.raises(PermanentProviderError, match="no status"):
        await client.get_session_status()


@pytest.mark.asyncio
async def test_receive_only_fuse_keeps_gets_and_denies_new_message_identity():
    transport = RecordingTransport(HttpResponse(200, b'{"status":"WORKING"}'))
    client = WahaClient(
        base_url="http://localhost:3000",
        api_key="secret",
        enabled=True,
        outbound_enabled=False,
        transport=transport,
    )
    assert await client.get_session_status() == "WORKING"
    with pytest.raises(WahaDisabledError):
        await client.new_message_id()
    with pytest.raises(WahaDisabledError):
        await client.send_text(
            recipient="+77000000003",
            text="Hello",
            idempotency_key="never",
            provider_message_id="never",
        )
    assert all(request["method"] == "GET" for request in transport.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_restriction_capabilities_are_required_for_clear(supported):
    class Transport:
        requests = []

        async def request(self, **kwargs):
            self.requests.append(kwargs)
            if kwargs["url"].endswith("/timelock"):
                return (
                    HttpResponse(200, b'{"isActive":false}')
                    if supported
                    else HttpResponse(404, b"{}")
                )
            if kwargs["url"].endswith("/capping"):
                return (
                    HttpResponse(200, b'{"cappingStatus":"NONE"}')
                    if supported
                    else HttpResponse(404, b"{}")
                )
            return HttpResponse(200, b'{"status":"WORKING"}')

    transport = Transport()
    client = WahaClient(
        base_url="http://localhost:3000",
        api_key="secret",
        enabled=True,
        outbound_enabled=False,
        transport=transport,
    )
    state = await client.get_account_restrictions()
    assert state["state"] == ("clear" if supported else "unknown")
    assert all(request["method"] == "GET" for request in transport.requests)
