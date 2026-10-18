from __future__ import annotations

import asyncio
import json
import os
import posixpath
import re
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.phone import normalize_phone


class WahaDisabledError(PermanentProviderError):
    pass


MAX_MEDIA_DOWNLOAD_BYTES = 20 * 1024 * 1024


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

    async def download(
        self,
        *,
        url: str,
        destination: Path,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> None:
        await asyncio.to_thread(
            self._download,
            url=url,
            destination=destination,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )

    @staticmethod
    def _download(
        *,
        url: str,
        destination: Path,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> None:
        if urlparse(url).scheme not in {"http", "https"}:
            raise PermanentProviderError("WAHA media transport only permits HTTP(S)")

        class _RejectRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirect)
        request = urllib.request.Request(  # noqa: S310 - scheme and origin are validated above.
            url=url, headers=dict(headers), method="GET"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        deadline = time.monotonic() + timeout_seconds
        try:
            with opener.open(request, timeout=max(0.001, deadline - time.monotonic())) as response:
                if not 200 <= response.status < 300:
                    raise _classify_download_status(response.status)
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=destination.parent, prefix=".media-", delete=False
                ) as target:
                    temporary_path = target.name
                    total = 0
                    read_chunk = getattr(response, "read1", response.read)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RetryableProviderError("WAHA media download deadline elapsed")
                        _set_response_read_timeout(response, remaining)
                        chunk = read_chunk(min(64 * 1024, max_bytes + 1 - total))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise PermanentProviderError("WAHA media exceeds configured size limit")
                        target.write(chunk)
            os.replace(temporary_path, destination)
            temporary_path = None
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise PermanentProviderError("WAHA media redirects are not allowed") from exc
            raise _classify_download_status(exc.code) from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise RetryableProviderError("WAHA media download failed") from exc
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass


def _classify_download_status(status: int) -> Exception:
    if status == 429 or status >= 500:
        return RetryableProviderError(f"WAHA media temporary error ({status})")
    return PermanentProviderError(f"WAHA media request rejected ({status})")


def _set_response_read_timeout(response: Any, timeout_seconds: float) -> None:
    """Apply the remaining whole-transfer budget to the underlying socket."""

    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    settimeout = getattr(sock, "settimeout", None)
    if callable(settimeout):
        settimeout(timeout_seconds)


@dataclass(frozen=True)
class WahaSendResult:
    external_message_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class WahaContactCheck:
    number_exists: bool
    chat_id: str | None


@dataclass(frozen=True, slots=True)
class WahaAccountIdentity:
    account_jid: str
    observed_at: datetime


def normalize_chat_id(recipient: str) -> str:
    canonical = normalize_phone(recipient)
    return f"{canonical.removeprefix('+')}@c.us"


def normalize_account_jid(value: object) -> str:
    if not isinstance(value, str):
        raise PermanentProviderError("WAHA authenticated account identity is missing")
    candidate = value.strip()
    if not re.fullmatch(r"[0-9]{6,20}@c\.us", candidate):
        raise PermanentProviderError("WAHA authenticated account identity is invalid")
    return candidate


class WahaClient:
    """Narrow WAHA adapter. It cannot send unless explicitly enabled."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        session: str = "default",
        enabled: bool = False,
        outbound_enabled: bool | None = None,
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
        self._outbound_enabled = enabled if outbound_enabled is None else outbound_enabled
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

        if not self._enabled or not self._outbound_enabled:
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

    @property
    def session_name(self) -> str:
        return self._session

    @property
    def outbound_enabled(self) -> bool:
        return self._enabled and self._outbound_enabled

    async def _session_get(self, suffix: str = "") -> dict[str, Any] | None:
        if not self._enabled or not self._api_key:
            raise WahaDisabledError("WAHA access is disabled or unconfigured")
        response = await self._transport.request(
            method="GET",
            url=urljoin(self._base_url, f"api/sessions/{quote(self._session, safe='')}{suffix}"),
            headers={"Accept": "application/json", "X-Api-Key": self._api_key},
            body=None,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status == 404:
            return None
        return self._decode(response)

    async def get_account_restrictions(self) -> dict[str, Any]:
        """Capability-aware GET probe; WORKING and endpoint absence are not clearance."""
        session = await self._session_get()
        status = (session or {}).get("status", "UNAVAILABLE")
        result: dict[str, Any] = {"session_status": status, "state": "unavailable"}
        if status != "WORKING":
            return result
        timelock = await self._session_get("/timelock")
        capping = await self._session_get("/capping")
        result.update(
            timelock_supported=timelock is not None, capping_supported=capping is not None
        )
        quota_exhausted = False
        if capping is not None:
            total, used = capping.get("totalQuota"), capping.get("usedQuota")
            quota_exhausted = (
                type(total) is int and type(used) is int and total >= 0 and used >= total
            )
        if timelock is not None and timelock.get("isActive") is True:
            result.update(state="timelock", restriction_until=timelock.get("timeEnforcementEnds"))
        elif quota_exhausted or (
            capping is not None
            and capping.get("cappingStatus")
            in {
                "FIRST_WARNING",
                "SECOND_WARNING",
                "CAPPED",
            }
        ):
            result.update(state="capped", restriction_until=capping.get("cycleEnd"))
        elif (
            timelock is not None
            and timelock.get("isActive") is False
            and capping is not None
            and capping.get("cappingStatus") == "NONE"
        ):
            result["state"] = "clear"
        else:
            result["state"] = "unknown"
        return result

    async def get_account_identity(self) -> WahaAccountIdentity:
        """Read the authenticated self JID; this is never an outbound operation."""

        if not self._enabled or not self._api_key:
            raise WahaDisabledError("WAHA access is disabled or unconfigured")
        response = await self._transport.request(
            method="GET",
            url=urljoin(
                self._base_url,
                f"api/sessions/{quote(self._session, safe='')}/me",
            ),
            headers={"Accept": "application/json", "X-Api-Key": self._api_key},
            body=None,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status == 404:
            raise RetryableProviderError("WAHA authenticated account identity is unavailable")
        if response.status == 429 or response.status >= 500:
            raise RetryableProviderError(f"WAHA temporary error ({response.status})")
        if not 200 <= response.status < 300:
            raise PermanentProviderError(f"WAHA rejected the request ({response.status})")
        try:
            value = json.loads(response.body or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PermanentProviderError("WAHA returned an invalid account identity") from exc
        if not isinstance(value, dict):
            raise RetryableProviderError("WAHA authenticated account identity is unavailable")
        account_jid = normalize_account_jid(value.get("id"))
        return WahaAccountIdentity(account_jid, datetime.now(UTC))

    async def get_session_details(self) -> dict[str, Any]:
        result = await self._session_get()
        if result is None:
            raise RetryableProviderError("WAHA session is unavailable")
        return result

    async def fetch_lid_mappings(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        """Read verified provider mappings; no phone digits are inferred from a LID."""
        if not self._enabled or not self._api_key:
            raise WahaDisabledError("WAHA access is disabled or unconfigured")
        if not 1 <= limit <= 100 or not 0 <= offset <= 10_000:
            raise ValueError("LID mapping pagination is outside bounded limits")
        response = await self._transport.request(
            method="GET",
            url=urljoin(
                self._base_url,
                f"api/{quote(self._session, safe='')}/lids?"
                + urlencode({"limit": limit, "offset": offset}),
            ),
            headers={"Accept": "application/json", "X-Api-Key": self._api_key},
            body=None,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status == 429 or response.status >= 500:
            raise RetryableProviderError("WAHA LID mappings temporarily unavailable")
        if not 200 <= response.status < 300:
            raise PermanentProviderError("WAHA LID mapping API unavailable")
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PermanentProviderError("WAHA LID mappings invalid") from exc
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise PermanentProviderError("WAHA LID mappings must be a list")
        return payload[:limit]

    async def get_session_status(self) -> str:
        """Read session availability without changing authentication or configuration."""
        if not self._enabled or not self._api_key:
            raise WahaDisabledError("WAHA access is disabled or unconfigured")
        response = await self._transport.request(
            method="GET",
            url=urljoin(self._base_url, f"api/sessions/{quote(self._session, safe='')}"),
            headers={"Accept": "application/json", "X-Api-Key": self._api_key},
            body=None,
            timeout_seconds=self._timeout_seconds,
        )
        status = self._decode(response).get("status")
        if not isinstance(status, str) or not status:
            raise PermanentProviderError("WAHA session has no status")
        return status

    async def fetch_message(
        self,
        chat_id: str,
        message_id: str,
        *,
        download_media: bool = False,
    ) -> dict[str, Any] | None:
        """Read one persisted provider identity without changing session storage."""
        if not self._enabled or not self._api_key:
            raise WahaDisabledError("WAHA access is disabled or unconfigured")
        if not chat_id.strip() or not message_id.strip():
            raise ValueError("chat and message identities must be nonempty")
        path = (
            f"api/{quote(self._session, safe='')}/chats/{quote(chat_id, safe='')}"
            f"/messages/{quote(message_id, safe='')}?downloadMedia={str(download_media).lower()}"
        )
        response = await self._transport.request(
            method="GET",
            url=urljoin(self._base_url, path),
            headers={"Accept": "application/json", "X-Api-Key": self._api_key},
            body=None,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status == 404:
            return None
        if response.status == 429 or response.status >= 500:
            raise RetryableProviderError(f"WAHA temporary error ({response.status})")
        if not 200 <= response.status < 300:
            raise PermanentProviderError(f"WAHA rejected message lookup ({response.status})")
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PermanentProviderError("WAHA returned invalid message") from exc
        if not isinstance(payload, dict):
            raise PermanentProviderError("WAHA message must be an object")
        return payload

    async def fetch_chat_messages(
        self,
        chat_id: str,
        *,
        limit: int = 20,
        download_media: bool = False,
        offset: int = 0,
        timestamp_gte: int | None = None,
        timestamp_lte: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch a bounded read-only chat history from WAHA."""

        if not self._enabled:
            raise WahaDisabledError("WAHA access is disabled")
        if not self._api_key:
            raise WahaDisabledError("WAHA API key is not configured")
        if not chat_id.strip():
            raise ValueError("chat_id must be non-empty")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if type(offset) is not int or not 0 <= offset <= 100_000:
            raise ValueError("offset must be between 0 and 100000")
        query_values: dict[str, Any] = {
            "limit": limit,
            "downloadMedia": str(download_media).lower(),
        }
        if offset:
            query_values["offset"] = offset
        for key, value in (("gte", timestamp_gte), ("lte", timestamp_lte)):
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError("history timestamps must be nonnegative integer seconds")
                query_values[f"filter.timestamp.{key}"] = value
        query = urlencode(query_values)
        encoded_chat_id = quote(chat_id, safe="")
        response = await self._transport.request(
            method="GET",
            url=urljoin(
                self._base_url,
                f"api/{quote(self._session, safe='')}/chats/{encoded_chat_id}/messages?{query}",
            ),
            headers={"Accept": "application/json", "X-Api-Key": self._api_key},
            body=None,
            timeout_seconds=self._timeout_seconds,
        )
        if response.status == 429 or response.status >= 500:
            raise RetryableProviderError(f"WAHA temporary error ({response.status})")
        try:
            payload = json.loads(response.body or b"[]")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PermanentProviderError("WAHA returned invalid chat messages") from exc
        if not 200 <= response.status < 300:
            raise PermanentProviderError(f"WAHA rejected the request ({response.status})")
        if isinstance(payload, dict):
            payload = payload.get("messages", payload.get("data"))
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise PermanentProviderError("WAHA chat messages must be a list of objects")
        return payload[:limit]

    async def download_media(
        self,
        media_url: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> None:
        """Download one WAHA file after strict origin/path validation."""

        if not self._enabled:
            raise WahaDisabledError("WAHA access is disabled")
        if not self._api_key:
            raise WahaDisabledError("WAHA API key is not configured")
        if not 1 <= max_bytes <= MAX_MEDIA_DOWNLOAD_BYTES:
            raise ValueError(f"max_bytes must be between 1 and {MAX_MEDIA_DOWNLOAD_BYTES}")
        trusted_url = self._validate_media_url(media_url)
        download = getattr(self._transport, "download", None)
        if download is None:
            raise PermanentProviderError(
                "configured WAHA transport does not support media downloads"
            )
        await download(
            url=trusted_url,
            destination=destination,
            headers={"Accept": "*/*", "X-Api-Key": self._api_key},
            timeout_seconds=self._timeout_seconds,
            max_bytes=max_bytes,
        )

    def _validate_media_url(self, media_url: str) -> str:
        if not isinstance(media_url, str) or not media_url.strip():
            raise PermanentProviderError("WAHA media URL is missing")
        try:
            base = urlparse(self._base_url)
            target = urlparse(urljoin(self._base_url, media_url))
            base_port = base.port or (443 if base.scheme == "https" else 80)
            target_port = target.port or (443 if target.scheme == "https" else 80)
        except ValueError as exc:
            raise PermanentProviderError("WAHA media URL is malformed") from exc
        if target.scheme != base.scheme or target.username or target.password or target.fragment:
            raise PermanentProviderError("WAHA media URL is outside the configured origin")
        local_hosts = {"localhost", "127.0.0.1"}
        same_host = target.hostname == base.hostname
        if base.hostname in local_hosts and target.hostname in local_hosts:
            same_host = True
        if not same_host or target_port != base_port:
            raise PermanentProviderError("WAHA media URL is outside the configured origin")
        trusted_path = (base.path.rstrip("/") + "/api/files").rstrip("/")
        decoded_path = posixpath.normpath(unquote(target.path))
        if decoded_path != trusted_path and not decoded_path.startswith(trusted_path + "/"):
            raise PermanentProviderError("WAHA media URL is not under /api/files")
        return target.geturl()

    async def send_text(
        self,
        *,
        recipient: str,
        text: str,
        idempotency_key: str,
        provider_message_id: str,
    ) -> WahaSendResult:
        if not self._enabled or not self._outbound_enabled:
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
