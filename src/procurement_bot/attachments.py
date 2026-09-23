"""Pure Telegram attachment classification and validation.

This module deliberately does not download files.  Ingress only records the
metadata returned by Telegram; a durable worker performs all network and disk
I/O after the accepting transaction commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

AttachmentKind = Literal["voice", "audio", "document", "photo"]

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GENERIC_MIMES = {"", "application/octet-stream", "binary/octet-stream"}

DOCUMENT_TYPES: dict[str, tuple[str, str]] = {
    ".pdf": (PDF_MIME, "application/pdf"),
    ".docx": (DOCX_MIME, DOCX_MIME),
}

AUDIO_MIME_SUFFIXES: dict[str, frozenset[str]] = {
    "audio/ogg": frozenset({".ogg", ".oga", ".opus"}),
    "audio/opus": frozenset({".opus", ".ogg"}),
    "audio/mpeg": frozenset({".mp3"}),
    "audio/mp3": frozenset({".mp3"}),
    "audio/mp4": frozenset({".m4a", ".mp4"}),
    "audio/x-m4a": frozenset({".m4a"}),
    "audio/wav": frozenset({".wav"}),
    "audio/x-wav": frozenset({".wav"}),
    "audio/webm": frozenset({".webm"}),
    "audio/aac": frozenset({".aac"}),
    "audio/flac": frozenset({".flac"}),
}

DEFAULT_SUFFIX_BY_AUDIO_MIME = {
    mime: sorted(suffixes, key=lambda value: (len(value), value))[0]
    for mime, suffixes in AUDIO_MIME_SUFFIXES.items()
}
DEFAULT_SUFFIX_BY_AUDIO_MIME.update({"audio/ogg": ".ogg", "audio/mp4": ".m4a"})


class AttachmentValidationError(ValueError):
    """Telegram media metadata is absent, unsupported, or contradictory."""


@dataclass(frozen=True, slots=True)
class AttachmentMetadata:
    kind: AttachmentKind
    file_id: str
    file_unique_id: str
    filename: str
    mime_type: str
    declared_size: int | None
    declared_duration_seconds: int | None
    suffix: str


def _normalise_mime(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _normalise_suffix(filename: str | None) -> str:
    return Path(filename or "").suffix.lower()


def validate_document_type(filename: str | None, mime_type: str | None) -> tuple[str, str]:
    """Return canonical ``(suffix, mime)`` for PDF/DOCX metadata.

    A known filename suffix is mandatory. Telegram MIME may be absent/generic,
    but a specific, contradictory MIME is rejected before a download is queued.
    The downloaded worker must still inspect magic bytes; metadata alone is not
    a security boundary.
    """

    suffix = _normalise_suffix(filename)
    if suffix not in DOCUMENT_TYPES:
        raise AttachmentValidationError("Поддерживаются только документы PDF и DOCX.")
    expected_mime, canonical_mime = DOCUMENT_TYPES[suffix]
    mime = _normalise_mime(mime_type)
    if mime not in GENERIC_MIMES and mime != expected_mime:
        raise AttachmentValidationError(
            "Этот файл не похож на PDF/DOCX. Пришлите его заново или "
            "сохраните в поддерживаемом формате."
        )
    return suffix, canonical_mime


def validate_audio_type(filename: str | None, mime_type: str | None) -> tuple[str, str]:
    """Return canonical audio suffix and MIME, rejecting known mismatches."""

    mime = _normalise_mime(mime_type)
    suffix = _normalise_suffix(filename)
    if mime in GENERIC_MIMES:
        matching = [known for known, suffixes in AUDIO_MIME_SUFFIXES.items() if suffix in suffixes]
        if not matching:
            raise AttachmentValidationError("Неподдерживаемый формат аудио.")
        return suffix, matching[0]
    allowed_suffixes = AUDIO_MIME_SUFFIXES.get(mime)
    if not allowed_suffixes:
        raise AttachmentValidationError("Неподдерживаемый формат аудио.")
    if suffix and suffix not in allowed_suffixes:
        raise AttachmentValidationError(
            "Не удалось распознать формат аудио. Пришлите запись как "
            "голосовое сообщение или обычный аудиофайл."
        )
    return suffix or DEFAULT_SUFFIX_BY_AUDIO_MIME[mime], mime


def attachment_from_message(message: Any) -> AttachmentMetadata | None:
    """Classify supported media from an aiogram ``Message`` (duck typed in tests)."""

    if getattr(message, "voice", None) is not None:
        value = message.voice
        mime = _normalise_mime(getattr(value, "mime_type", None)) or "audio/ogg"
        suffix, mime = validate_audio_type("voice.ogg", mime)
        return AttachmentMetadata(
            kind="voice",
            file_id=value.file_id,
            file_unique_id=getattr(value, "file_unique_id", "") or "",
            filename=f"voice{suffix}",
            mime_type=mime,
            declared_size=getattr(value, "file_size", None),
            declared_duration_seconds=getattr(value, "duration", None),
            suffix=suffix,
        )

    if getattr(message, "audio", None) is not None:
        value = message.audio
        original = getattr(value, "file_name", None)
        suffix, mime = validate_audio_type(original, getattr(value, "mime_type", None))
        return AttachmentMetadata(
            kind="audio",
            file_id=value.file_id,
            file_unique_id=getattr(value, "file_unique_id", "") or "",
            filename=original or f"audio{suffix}",
            mime_type=mime,
            declared_size=getattr(value, "file_size", None),
            declared_duration_seconds=getattr(value, "duration", None),
            suffix=suffix,
        )

    if getattr(message, "document", None) is not None:
        value = message.document
        original = getattr(value, "file_name", None)
        suffix, mime = validate_document_type(original, getattr(value, "mime_type", None))
        return AttachmentMetadata(
            kind="document",
            file_id=value.file_id,
            file_unique_id=getattr(value, "file_unique_id", "") or "",
            filename=original or f"document{suffix}",
            mime_type=mime,
            declared_size=getattr(value, "file_size", None),
            declared_duration_seconds=None,
            suffix=suffix,
        )

    photos = getattr(message, "photo", None)
    if photos:
        value = max(photos, key=lambda photo: (getattr(photo, "file_size", 0) or 0))
        return AttachmentMetadata(
            kind="photo",
            file_id=value.file_id,
            file_unique_id=getattr(value, "file_unique_id", "") or "",
            filename="photo.jpg",
            mime_type="image/jpeg",
            declared_size=getattr(value, "file_size", None),
            declared_duration_seconds=None,
            suffix=".jpg",
        )

    return None
