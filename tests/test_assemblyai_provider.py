from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.assemblyai import AssemblyAITranscriber


async def test_transcribes_with_ru_kk_language_detection(tmp_path: Path) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"not-real-audio-but-provider-boundary-does-not-decode")
    submitted: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "private-test-key"
        if request.url.path == "/v2/upload":
            assert await request.aread() == audio.read_bytes()
            return httpx.Response(200, json={"upload_url": "https://cdn.example/audio"})
        if request.url.path == "/v2/transcript" and request.method == "POST":
            submitted.update(json.loads(await request.aread()))
            return httpx.Response(200, json={"id": "transcript-1"})
        if request.url.path == "/v2/transcript/transcript-1":
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "text": "Найдите десять коробок товара.",
                    "language_code": "ru",
                    "language_confidence": 0.97,
                    "speech_model_used": "universal-2",
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transcriber = AssemblyAITranscriber(
        "private-test-key",
        transport=httpx.MockTransport(handler),
        poll_interval_seconds=0.001,
    )
    result = await transcriber.transcribe(audio)

    assert result.text == "Найдите десять коробок товара."
    assert result.language == "ru"
    assert result.confidence == 0.97
    assert result.provider == "assemblyai:universal-2:transcript-1"
    assert submitted["language_detection"] is True
    assert submitted["language_detection_options"] == {
        "expected_languages": ["ru", "kk"],
        "fallback_language": "auto",
    }


async def test_explicit_kazakh_language_is_forwarded(tmp_path: Path) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")
    submitted: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/upload":
            return httpx.Response(200, json={"upload_url": "https://cdn.example/audio"})
        if request.method == "POST":
            submitted.update(json.loads(await request.aread()))
            return httpx.Response(200, json={"id": "transcript-2"})
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "text": "Тауарды табыңыз.",
                "language_code": "kk",
                "language_confidence": 0.99,
            },
        )

    transcriber = AssemblyAITranscriber(
        "private-test-key",
        transport=httpx.MockTransport(handler),
    )
    result = await transcriber.transcribe(audio, language="kk-KZ")

    assert result.language == "kk"
    assert submitted["language_code"] == "kk"
    assert "language_detection" not in submitted


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
async def test_temporary_http_statuses_are_retryable(tmp_path: Path, status: int) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")
    transcriber = AssemblyAITranscriber(
        "private-test-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(status)),
    )
    with pytest.raises(RetryableProviderError, match=f"HTTP {status}"):
        await transcriber.transcribe(audio)


async def test_authentication_failure_is_permanent(tmp_path: Path) -> None:
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")
    transcriber = AssemblyAITranscriber(
        "private-test-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(401)),
    )
    with pytest.raises(PermanentProviderError, match="HTTP 401"):
        await transcriber.transcribe(audio)
