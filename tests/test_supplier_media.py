from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from procurement_bot.supplier_media import (
    SupplierMediaDeferred,
    WhatsAppMediaInput,
    effective_supplier_message_text,
    media_storage_path,
    persist_supplier_message_media,
)


class _Connection:
    def __init__(self, *, inserted: bool, existing: dict[str, Any] | None = None) -> None:
        self.inserted = inserted
        self.existing = existing
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, sql: str, *args: object) -> dict[str, Any] | None:
        self.calls.append((sql, args))
        if "INSERT INTO supplier_message_media" in sql:
            return {"message_id": args[0]} if self.inserted else None
        return self.existing


@pytest.mark.asyncio
async def test_media_record_insert_is_idempotent_and_persists_pending_null_url() -> None:
    connection = _Connection(inserted=True)
    media = WhatsAppMediaInput(kind="audio", url=None, mimetype="audio/ogg")
    message_id = uuid4()

    created = await persist_supplier_message_media(
        connection,
        message_id=message_id,
        media=media,  # type: ignore[arg-type]
    )

    sql, args = connection.calls[0]
    assert created is True
    assert "ON CONFLICT (message_id) DO NOTHING" in sql
    assert args == (message_id, "audio", None, "audio/ogg", None, "")


@pytest.mark.asyncio
async def test_media_record_duplicate_requires_matching_immutable_evidence() -> None:
    media = WhatsAppMediaInput(kind="image", url="http://localhost:3000/api/files/a.jpg")
    connection = _Connection(
        inserted=False,
        existing={
            "kind": "image",
            "remote_url": media.url,
            "mime_type": None,
            "filename": None,
        },
    )

    created = await persist_supplier_message_media(
        connection,
        message_id=uuid4(),
        media=media,  # type: ignore[arg-type]
    )

    assert created is False
    assert len(connection.calls) == 2


@pytest.mark.asyncio
async def test_conflicting_duplicate_media_fails_closed() -> None:
    connection = _Connection(
        inserted=False,
        existing={"kind": "image", "remote_url": "other", "mime_type": None, "filename": None},
    )

    with pytest.raises(ValueError, match="conflicts"):
        await persist_supplier_message_media(
            connection,
            message_id=uuid4(),
            media=WhatsAppMediaInput(kind="image", url="expected"),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_effective_text_uses_ready_transcription_only() -> None:
    class TranscriptionConnection:
        async def fetchrow(self, _sql: str, *_args: object) -> dict[str, Any]:
            return {"state": "ready", "extracted_text": "  размеры 20 × 10  "}

    assert (
        await effective_supplier_message_text(
            TranscriptionConnection(),
            message_id=uuid4(),
            original_text="caption",  # type: ignore[arg-type]
        )
        == "caption\nразмеры 20 × 10"
    )
    assert (
        await effective_supplier_message_text(
            TranscriptionConnection(),
            message_id=uuid4(),
            original_text="[Фотография поставщика]",  # type: ignore[arg-type]
        )
        == "размеры 20 × 10"
    )


def test_storage_path_rejects_traversal_and_deferred_time_requires_timezone() -> None:
    root = Path("/var/lib/procurement/media")
    assert media_storage_path(root, "2026/10/file.ogg") == root.resolve() / "2026/10/file.ogg"
    with pytest.raises(ValueError, match="escapes"):
        media_storage_path(root, "../../etc/passwd")
    with pytest.raises(ValueError, match="timezone-aware"):
        SupplierMediaDeferred(datetime(2026, 10, 1), "retry")
