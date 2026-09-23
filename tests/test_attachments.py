from __future__ import annotations

from types import SimpleNamespace

import pytest

from procurement_bot.attachments import (
    AttachmentValidationError,
    attachment_from_message,
    validate_audio_type,
    validate_document_type,
)


def msg(**values: object) -> SimpleNamespace:
    defaults = {"voice": None, "audio": None, "document": None, "photo": None}
    defaults.update(values)
    return SimpleNamespace(**defaults)


@pytest.mark.parametrize(
    ("filename", "mime", "expected"),
    [
        ("spec.PDF", "application/pdf", (".pdf", "application/pdf")),
        (
            "request.docx",
            "application/octet-stream",
            (
                ".docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        ),
    ],
)
def test_validate_document_type(filename: str, mime: str, expected: tuple[str, str]) -> None:
    assert validate_document_type(filename, mime) == expected


@pytest.mark.parametrize(
    ("filename", "mime"),
    [("x.exe", "application/octet-stream"), ("x.pdf", "text/plain"), (None, "application/pdf")],
)
def test_validate_document_rejects_unsupported_or_contradictory_metadata(
    filename: str | None, mime: str
) -> None:
    with pytest.raises(AttachmentValidationError):
        validate_document_type(filename, mime)


def test_audio_validation_is_deterministic() -> None:
    assert validate_audio_type("note.MP3", "audio/mpeg") == (".mp3", "audio/mpeg")
    assert validate_audio_type(None, "audio/ogg") == (".ogg", "audio/ogg")
    with pytest.raises(AttachmentValidationError):
        validate_audio_type("note.wav", "audio/mpeg")


def test_document_metadata_is_extracted_without_download() -> None:
    document = SimpleNamespace(
        file_id="file-1",
        file_unique_id="unique-1",
        file_name="request.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size=123,
    )
    result = attachment_from_message(msg(document=document))
    assert result is not None
    assert result.kind == "document"
    assert result.file_id == "file-1"
    assert result.suffix == ".docx"


def test_largest_photo_variant_is_selected() -> None:
    small = SimpleNamespace(file_id="small", file_unique_id="u1", file_size=10)
    large = SimpleNamespace(file_id="large", file_unique_id="u2", file_size=20)
    result = attachment_from_message(msg(photo=[small, large]))
    assert result is not None
    assert result.file_id == "large"
    assert result.mime_type == "image/jpeg"
