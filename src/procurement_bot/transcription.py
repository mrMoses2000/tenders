from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from procurement_bot.errors import PermanentProviderError


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str
    confidence: float | None
    provider: str


SUPPORTED_PROCUREMENT_LANGUAGES = frozenset({"ru", "kk"})


def transcript_review_reason(
    transcript: Transcript,
    *,
    minimum_language_confidence: float,
) -> str | None:
    """Return why a transcript must not automatically drive procurement actions."""

    if not 0 <= minimum_language_confidence <= 1:
        raise ValueError("minimum_language_confidence must be between 0 and 1")
    language = transcript.language.casefold().split("-", 1)[0]
    if language not in SUPPORTED_PROCUREMENT_LANGUAGES:
        return f"unsupported_language:{language or 'unknown'}"
    if transcript.confidence is None:
        return "language_confidence_missing"
    if transcript.confidence < minimum_language_confidence:
        return "language_confidence_low"
    return None


class Transcriber(Protocol):
    async def transcribe(self, path: Path, language: str = "auto") -> Transcript: ...


class DisabledTranscriber:
    async def transcribe(self, path: Path, language: str = "auto") -> Transcript:
        raise PermanentProviderError("voice transcription is disabled")
