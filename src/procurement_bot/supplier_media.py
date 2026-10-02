from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg


class SupplierMediaDeferred(RuntimeError):
    """A media operation should be retried at a durable future time."""

    def __init__(self, available_at: datetime, reason: str) -> None:
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        if not reason.strip():
            raise ValueError("reason must be non-empty")
        self.available_at = available_at
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class WhatsAppMediaInput:
    kind: str
    url: str | None = None
    mimetype: str | None = None
    filename: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"image", "audio"}:
            raise ValueError("kind must be image or audio")
        for name, value, limit in (
            ("url", self.url, 4096),
            ("mimetype", self.mimetype, 255),
            ("filename", self.filename, 1024),
            ("error", self.error, 1000),
        ):
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                raise ValueError(f"{name} is invalid or too long")

    def as_evidence(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "mimetype": self.mimetype,
            "filename": self.filename,
            "error": self.error,
        }


async def persist_supplier_message_media(
    connection: asyncpg.Connection,
    *,
    message_id: UUID,
    media: WhatsAppMediaInput,
) -> bool:
    """Add the media work record once; return whether this call created it."""

    row = await connection.fetchrow(
        """
        INSERT INTO supplier_message_media(
            message_id,kind,remote_url,mime_type,filename,last_error_code
        ) VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING message_id
        """,
        message_id,
        media.kind,
        media.url,
        media.mimetype,
        media.filename,
        "waha_media_download_failed" if media.error else "",
    )
    if row is not None:
        return True
    existing = await connection.fetchrow(
        "SELECT kind,remote_url,mime_type,filename FROM supplier_message_media WHERE message_id=$1",
        message_id,
    )
    expected = (media.kind, media.url, media.mimetype, media.filename)
    actual = (
        (
            existing["kind"],
            existing["remote_url"],
            existing["mime_type"],
            existing["filename"],
        )
        if existing is not None
        else None
    )
    if actual != expected:
        raise ValueError("existing supplier media record conflicts with immutable message evidence")
    return False


async def effective_supplier_message_text(
    connection: asyncpg.Connection,
    *,
    message_id: UUID,
    original_text: str,
) -> str:
    """Use completed transcription when present; otherwise preserve message text."""

    row = await connection.fetchrow(
        "SELECT state,extracted_text FROM supplier_message_media WHERE message_id=$1",
        message_id,
    )
    if row is not None and row["state"] == "ready":
        extracted = row["extracted_text"]
        if isinstance(extracted, str) and extracted.strip():
            derived = extracted.strip()
            caption = original_text.strip()
            if caption and caption not in {
                "[Фотография поставщика]",
                "[Голосовое сообщение поставщика]",
            }:
                return f"{caption}\n{derived}"
            return derived
    return original_text


def media_storage_path(root: Path, object_key: str) -> Path:
    """Resolve a private object key without permitting traversal outside root."""

    if not object_key or Path(object_key).is_absolute():
        raise ValueError("object_key must be a non-empty relative path")
    resolved_root = root.resolve()
    resolved = (resolved_root / object_key).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("object_key escapes media storage root")
    return resolved
