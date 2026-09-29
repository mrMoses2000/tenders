from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin, urlparse

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.phone import normalize_phone


class WahaDisabledError(PermanentProviderError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class JsonTransport(Protocol):
    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse: ...


class UrllibJsonTransport:
    """Tiny async boundary backed by the standard library."""

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        return await asyncio.to_thread(
            self._request,
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _request(
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        if urlparse(url).scheme not in {"http", "https"}:
            raise PermanentProviderError("WAHA transport only permits HTTP(S)")
        request = urllib.request.Request(  # noqa: S310
            url=url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                return HttpResponse(status=response.status, body=response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(status=exc.code, body=exc.read())
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise RetryableProviderError("WAHA request failed") from exc


@dataclass(frozen=True)
class WahaSendResult:
    external_message_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class WahaContactCheck:
    number_exists: bool
    chat_id: str | None


def normalize_chat_id(recipient: str) -> str:
    canonical = normalize_phone(recipient)
    return f"{canonical.removeprefix('+')}@c.us"


class WahaClient:
    """Narrow WAHA adapter. It cannot send unless explicitly enabled."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        session: str = "default",
        enabled: bool = False,
        timeout_seconds: float = 15.0,
        transport: JsonTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("WAHA base_url must be an absolute HTTP(S) URL")
        if not session.strip():
            raise ValueError("WAHA session must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("WAHA timeout must be positive")
        self._base_url = base_url.rstrip("/") + "/"
        self._api_key = api_key.strip()
        self._session = session.strip()
        self._enabled = enabled
        self._timeout_seconds = timeout_seconds
        self._transport = transport or UrllibJsonTransport()

    async def check_phone_exists(self, recipient: str) -> WahaContactCheck:
        """Check registration without creating a chat or sending a message."""

        if not self._enabled:
            raise WahaDisabledError("WAHA contact checking is disabled")
        if not self._api_key:
            raise WahaDisabledError("WAHA API key is not configured")
        phone = normalize_phone(recipient).removeprefix("+")
        query = urlencode({"phone": phone, "session": self._session})
        response = await self._transport.request(
            method="GET",
            url=urljoin(self._base_url, f"api/contacts/check-exists?{query}"),
            headers={"Accept": "application/json", "X-Api-Key": self._api_key},
            body=None,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status == 422:
            raise RetryableProviderError("WAHA session is unavailable for contact checking")
        decoded = self._decode(response)
        exists = decoded.get("numberExists")
        chat_id = decoded.get("chatId")
        if not isinstance(exists, bool):
            raise PermanentProviderError("WAHA contact check has no boolean numberExists")
        if exists and (not isinstance(chat_id, str) or not chat_id.strip()):
            raise PermanentProviderError("WAHA registered contact has no chatId")
        return WahaContactCheck(number_exists=exists, chat_id=chat_id if exists else None)

    async def new_message_id(self) -> str:
        """Reserve a WAHA message id before sending (GOWS/NOWEB, WAHA 2026.4.3+)."""

        if not self._enabled:
            raise WahaDisabledError("WAHA sending is disabled")
        if not self._api_key:
            raise WahaDisabledError("WAHA API key is not configured")
        response = await self._transport.request(
            method="GET",
            url=urljoin(self._base_url, f"api/{self._session}/new-message-id"),
            headers={"Accept": "application/json", "X-Api-Key": self._api_key},
            body=None,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status == 422:
            raise RetryableProviderError("WAHA session is not ready to reserve a message id")
        return self._extract_message_id(self._decode(response))

    async def send_text(
        self,
        *,
        recipient: str,
        text: str,
        idempotency_key: str,
        provider_message_id: str,
    ) -> WahaSendResult:
        if not self._enabled:
            raise WahaDisabledError("WAHA sending is disabled")
        if not self._api_key:
            raise WahaDisabledError("WAHA API key is not configured")
        clean_text = text.strip()
        if not clean_text:
            raise ValueError("WhatsApp text must be non-empty")
        if len(clean_text) > 4096:
            raise ValueError("WhatsApp text exceeds the 4096 character safety limit")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        if not provider_message_id.strip():
            raise ValueError("provider_message_id must be non-empty")

        payload = {
            "session": self._session,
            "chatId": normalize_chat_id(recipient),
            "text": clean_text,
            "id": provider_message_id,
        }
        response = await self._transport.request(
            method="POST",
            url=urljoin(self._base_url, "api/sendText"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Api-Key": self._api_key,
                "X-Idempotency-Key": idempotency_key,
            },
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            timeout_seconds=self._timeout_seconds,
        )
        decoded = self._decode(response)
        if "error" in decoded:
            raise PermanentProviderError("WAHA sendText returned an error")
        try:
            external_id = self._extract_message_id(decoded)
        except RetryableProviderError:
            # NOWEB can accept the send with a 2xx response before returning a
            # WAMessage. The caller reserved this exact id before the request.
            external_id = f"true_{normalize_chat_id(recipient)}_{provider_message_id}"
        return WahaSendResult(external_message_id=external_id, raw=decoded)

    @staticmethod
    def _decode(response: HttpResponse) -> dict[str, Any]:
        # Classify transport status before parsing. Proxies commonly return
        # plain text/HTML for 429 and 5xx; those responses are still retryable.
        if response.status == 429 or response.status >= 500:
            raise RetryableProviderError(f"WAHA temporary error ({response.status})")
        try:
            value = json.loads(response.body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if response.status >= 500:
                raise RetryableProviderError("WAHA returned an invalid response") from exc
            raise PermanentProviderError("WAHA returned an invalid response") from exc
        if not 200 <= response.status < 300:
            raise PermanentProviderError(f"WAHA rejected the request ({response.status})")
        if not isinstance(value, dict):
            raise PermanentProviderError("WAHA response must be an object")
        return value

    @staticmethod
    def _extract_message_id(payload: Mapping[str, Any]) -> str:
        direct = payload.get("id")
        nested = payload.get("_data")
        if isinstance(direct, str) and direct:
            return direct
        if isinstance(nested, Mapping):
            nested_id = nested.get("id")
            if isinstance(nested_id, str) and nested_id:
                return nested_id
        raise RetryableProviderError("WAHA response did not contain a message id")
