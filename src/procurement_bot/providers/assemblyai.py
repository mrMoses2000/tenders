from __future__ import annotations

import asyncio
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.transcription import Transcript


class AssemblyAITranscriber:
    """Pre-recorded audio transcription through AssemblyAI's REST API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.assemblyai.com",
        timeout_seconds: int = 10_800,
        poll_interval_seconds: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("AssemblyAI API key is required")
        if timeout_seconds < 30:
            raise ValueError("AssemblyAI timeout must be at least 30 seconds")
        if poll_interval_seconds <= 0:
            raise ValueError("AssemblyAI poll interval must be positive")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._transport = transport

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._api_key,
            "User-Agent": "procurement-agent-bot/0.1",
        }

    @staticmethod
    def _request_timeout() -> httpx.Timeout:
        # Every network operation is bounded, including durable one-shot polls.
        return httpx.Timeout(connect=30.0, read=120.0, write=300.0, pool=30.0)

    async def transcribe(self, path: Path, language: str = "auto") -> Transcript:
        if not path.is_file() or path.stat().st_size <= 0:
            raise PermanentProviderError("audio file is missing or empty")
        try:
            async with httpx.AsyncClient(
                timeout=self._request_timeout(),
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                upload_url = await self._upload(client, path)
                transcript_id = await self._submit(client, upload_url, language)
                return await self._wait(client, transcript_id)
        except httpx.HTTPStatusError as exc:
            self._classify_http(exc)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError("AssemblyAI network error") from exc
        except (TypeError, ValueError) as exc:
            raise PermanentProviderError("AssemblyAI returned malformed JSON") from exc
        raise AssertionError("unreachable")

    async def submit(self, path: Path, language: str = "auto") -> str:
        """Upload audio and create a transcript job without waiting for completion."""
        if not path.is_file() or path.stat().st_size <= 0:
            raise PermanentProviderError("audio file is missing or empty")
        try:
            async with httpx.AsyncClient(
                timeout=self._request_timeout(),
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                upload_url = await self._upload(client, path)
                return await self._submit(client, upload_url, language)
        except httpx.HTTPStatusError as exc:
            self._classify_http(exc)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError("AssemblyAI network error") from exc
        except (TypeError, ValueError) as exc:
            raise PermanentProviderError("AssemblyAI returned malformed JSON") from exc
        raise AssertionError("unreachable")

    async def poll(self, transcript_id: str) -> Transcript | None:
        """Fetch one transcript status; queued work returns ``None`` immediately."""
        safe_id = self._validate_transcript_id(transcript_id)
        try:
            async with httpx.AsyncClient(
                timeout=self._request_timeout(),
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = await client.get(
                    f"{self._base_url}/v2/transcript/{quote(safe_id, safe='')}",
                    headers=self._headers,
                )
                response.raise_for_status()
                data = response.json()
                return self._parse_result(data, safe_id)
        except httpx.HTTPStatusError as exc:
            self._classify_http(exc)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableProviderError("AssemblyAI network error") from exc
        except (TypeError, ValueError) as exc:
            raise PermanentProviderError("AssemblyAI returned malformed JSON") from exc
        raise AssertionError("unreachable")

    @staticmethod
    def _validate_transcript_id(transcript_id: str) -> str:
        # AssemblyAI IDs are UUIDs. Permit safe provider/test identifiers while
        # rejecting path, query, fragment, and percent-encoding delimiters.
        if not isinstance(transcript_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,200}", transcript_id
        ):
            raise PermanentProviderError("invalid AssemblyAI transcript ID")
        return transcript_id

    async def _upload(self, client: httpx.AsyncClient, path: Path) -> str:
        response = await client.post(
            f"{self._base_url}/v2/upload",
            headers={**self._headers, "Content-Length": str(path.stat().st_size)},
            content=self._stream_file(path),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PermanentProviderError("AssemblyAI returned malformed upload JSON")
        upload_url = str(payload.get("upload_url") or "")
        if not upload_url.startswith("https://"):
            raise PermanentProviderError("AssemblyAI upload response has no HTTPS URL")
        return upload_url

    async def _submit(
        self,
        client: httpx.AsyncClient,
        upload_url: str,
        language: str,
    ) -> str:
        payload: dict[str, Any] = {
            "audio_url": upload_url,
            "punctuate": True,
            "format_text": True,
            "speech_models": ["universal-3-pro", "universal-2"],
        }
        normalized_language = language.casefold().split("-", 1)[0]
        if normalized_language in {"ru", "kk"}:
            payload["language_code"] = normalized_language
        else:
            payload.update(
                language_detection=True,
                language_detection_options={
                    "expected_languages": ["ru", "kk"],
                    "fallback_language": "auto",
                },
            )
        response = await client.post(
            f"{self._base_url}/v2/transcript",
            headers=self._headers,
            json=payload,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PermanentProviderError("AssemblyAI returned malformed transcript JSON")
        transcript_id = str(payload.get("id") or "")
        if not transcript_id:
            raise PermanentProviderError("AssemblyAI response has no transcript ID")
        return self._validate_transcript_id(transcript_id)

    async def _wait(self, client: httpx.AsyncClient, transcript_id: str) -> Transcript:
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            response = await client.get(
                f"{self._base_url}/v2/transcript/{transcript_id}",
                headers=self._headers,
            )
            response.raise_for_status()
            data = response.json()
            result = self._parse_result(data, transcript_id)
            if result is not None:
                return result
            await asyncio.sleep(self._poll_interval_seconds)
        raise RetryableProviderError("AssemblyAI polling timeout")

    @staticmethod
    def _confidence(value: Any, label: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise PermanentProviderError(f"AssemblyAI returned invalid {label}")
        try:
            confidence = float(value)
        except (TypeError, ValueError) as exc:
            raise PermanentProviderError(f"AssemblyAI returned invalid {label}") from exc
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise PermanentProviderError(f"AssemblyAI returned invalid {label}")
        return confidence

    @classmethod
    def _parse_result(cls, data: Any, transcript_id: str) -> Transcript | None:
        if not isinstance(data, dict):
            raise PermanentProviderError("AssemblyAI returned malformed JSON")
        status = data.get("status")
        if status in {"queued", "processing"}:
            return None
        if status == "error":
            error = str(data.get("error") or "AssemblyAI transcription failed")
            raise PermanentProviderError(error[:500])
        if status != "completed":
            raise PermanentProviderError("unexpected AssemblyAI transcript status")
        text = str(data.get("text") or "").strip()
        if not text:
            raise PermanentProviderError("AssemblyAI returned an empty transcript")
        language_confidence = cls._confidence(
            data.get("language_confidence"), "language confidence"
        )
        speech_confidence = cls._confidence(data.get("confidence"), "speech confidence")
        words = data.get("words") or []
        if not isinstance(words, list):
            raise PermanentProviderError("AssemblyAI returned invalid word confidence data")
        low_confidence_words: list[str] = []
        for word in words:
            if not isinstance(word, dict):
                continue
            token = word.get("text")
            confidence = cls._confidence(word.get("confidence"), "word confidence")
            if (
                isinstance(token, str)
                and token.strip()
                and confidence is not None
                and confidence < 0.8
            ):
                low_confidence_words.append(token.strip())
        model = str(data.get("speech_model_used") or data.get("speech_model") or "unknown")
        return Transcript(
            text=text,
            language=str(data.get("language_code") or "unknown"),
            confidence=language_confidence,
            provider=f"assemblyai:{model}:{transcript_id}",
            speech_confidence=speech_confidence,
            low_confidence_words=tuple(low_confidence_words),
        )

    @staticmethod
    async def _stream_file(path: Path, chunk_size: int = 1024 * 1024):
        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while chunk := await asyncio.to_thread(handle.read, chunk_size):
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    @staticmethod
    def _classify_http(exc: httpx.HTTPStatusError) -> None:
        status = exc.response.status_code
        if status in {408, 425, 429, 500, 502, 503, 504}:
            raise RetryableProviderError(f"AssemblyAI HTTP {status}") from exc
        raise PermanentProviderError(f"AssemblyAI HTTP {status}") from exc
