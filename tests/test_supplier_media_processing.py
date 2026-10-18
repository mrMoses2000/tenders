from __future__ import annotations

import asyncio
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import procurement_bot.supplier_media_processing as media
from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.providers.supplier_image import SupplierImageReading
from procurement_bot.supplier_media import SupplierMediaDeferred
from procurement_bot.supplier_media_evidence import media_clarification, supplier_evidence_text
from procurement_bot.transcription import Transcript


def test_original_and_derived_evidence_are_separate_and_pending_text_is_not_used():
    original = "[Голосовое сообщение поставщика]"
    row = {"media_state": "ready", "media_extracted_text": "Бағасы 500 теңге"}
    assert supplier_evidence_text(original, row) == original + "\n\nБағасы 500 теңге"
    row["media_state"] = "submitted"
    assert supplier_evidence_text(original, row) == original


def test_media_clarification_is_bilingual_and_never_guesses_a_price():
    row = {"media_kind": "image", "media_state": "ready", "media_needs_clarification": True}
    assert "валюту" in media_clarification(row, language="ru")
    assert "валютасын" in media_clarification(row, language="kk")
    assert media_clarification({}, language="ru") is None


class Pool:
    def __init__(self, *, kind="audio", request_id=None):
        self.message_id, self.conversation_id = uuid4(), uuid4()
        self.row = {
            "kind": kind,
            "provider_request_id": request_id,
            "object_key": "recording",
            "state": "submitted" if request_id else "downloaded",
            "opted_out": False,
            "case_status": "researching",
            "created_at": datetime.now(UTC),
            "case_id": uuid4(),
        }
        self.writes = []

    async def fetchrow(self, *_):
        return self.row

    async def execute(self, query, *args):
        self.writes.append((query, args))


async def run(
    pool, tmp_path, monkeypatch, transcriber, *, image_reader=None, kazakh_transcriber=None,
):
    replies = []

    async def enqueue(_pool, conversation_id, message_id):
        replies.append((conversation_id, message_id))

    monkeypatch.setattr(media, "_enqueue_reply", enqueue)
    await media.process_supplier_media_payload(
        {"conversation_id": str(pool.conversation_id), "message_id": str(pool.message_id)},
        pool=pool,
        storage=SimpleNamespace(resolve=lambda _: tmp_path / "recording"),
        waha=None,
        transcriber=transcriber,
        image_reader=image_reader,
        audio_probe=None,
        kazakh_transcriber=kazakh_transcriber,
    )
    return replies


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RetryableProviderError, PermanentProviderError, TimeoutError])
async def test_optional_fallback_failure_preserves_primary_kazakh_evidence(
    tmp_path, monkeypatch, failure,
):
    pool = Pool(request_id="durable-request")

    class Primary:
        async def poll(self, request):
            assert request == "durable-request"
            return Transcript("Бағасы 500 теңге", "kk", None, "assemblyai", 0.9, ())

    class Fallback:
        async def transcribe(self, path, language):
            assert language == "kk"
            raise failure("provider unavailable")

    replies = await run(pool, tmp_path, monkeypatch, Primary(), kazakh_transcriber=Fallback())
    saved = pool.writes[-1][1]
    assert saved[1] == "ready"
    assert saved[2] == "Бағасы 500 теңге"
    assert saved[3] == "assemblyai"
    assert saved[6] is True
    assert saved[8]["fallback_error"] == failure.__name__
    assert replies == [(pool.conversation_id, pool.message_id)]


@pytest.mark.asyncio
async def test_audio_submit_is_persisted_before_nonblocking_poll_deferral(tmp_path, monkeypatch):
    pool = Pool()

    class Transcriber:
        async def submit(self, path: Path, language):
            assert language == "auto"
            return "durable-request"

        async def poll(self, request):
            assert request == "durable-request"
            assert pool.writes[-1][1][1] == request
            return None

    with pytest.raises(SupplierMediaDeferred) as deferred:
        await run(pool, tmp_path, monkeypatch, Transcriber())
    assert deferred.value.available_at > datetime.now(UTC)
    assert len(pool.writes) == 1


@pytest.mark.asyncio
async def test_audio_resume_never_resubmits_and_confident_transcript_drives_reply(
    tmp_path, monkeypatch
):
    pool = Pool(request_id="durable-request")

    class Transcriber:
        async def submit(self, *_):
            pytest.fail("persisted submission must not be repeated")

        async def poll(self, request):
            return Transcript("Цена 500 тенге за штуку", "ru", 0.98, "assemblyai", 0.95, ())

    replies = await run(pool, tmp_path, monkeypatch, Transcriber())
    assert replies == [(pool.conversation_id, pool.message_id)]
    assert pool.writes[-1][1][6] is False


@pytest.mark.asyncio
async def test_uncertain_spoken_digits_require_text_confirmation(tmp_path, monkeypatch):
    pool = Pool(request_id="durable-request")

    class Transcriber:
        async def poll(self, request):
            return Transcript("Цена 500 тенге", "ru", 0.98, "assemblyai", 0.9, ("500",))

    await run(pool, tmp_path, monkeypatch, Transcriber())
    assert pool.writes[-1][1][6] is True
    assert pool.writes[-1][1][7] == "speech_words_uncertain"


@pytest.mark.asyncio
async def test_photograph_reading_is_stored_but_never_confirms_a_quote(tmp_path, monkeypatch):
    pool = Pool(kind="image")

    class ImageReader:
        async def read(self, _):
            return SupplierImageReading(
                readable_text="Лак 500 тг",
                observations="Банка",
                language="ru",
                confidence=0.99,
                uncertain_numbers=False,
            )

    await run(pool, tmp_path, monkeypatch, None, image_reader=ImageReader())
    assert pool.writes[-1][1][2] == "Лак 500 тг"
    assert pool.writes[-1][1][6] is True


@pytest.mark.asyncio
async def test_missing_image_reader_clarifies_without_dropping_message(tmp_path, monkeypatch):
    pool = Pool(kind="image")
    replies = await run(pool, tmp_path, monkeypatch, None)
    assert replies
    assert pool.writes[-1][1][1] == "failed"


@pytest.mark.asyncio
async def test_image_download_is_bounded_private_and_stored_with_verified_suffix(
    tmp_path, monkeypatch
):
    pool = Pool(kind="image")
    pool.row.update(
        object_key=None,
        remote_url="http://127.0.0.1:3000/api/files/image.jpg",
        external_chat_id="7700@c.us",
        external_message_id="true_7700@c.us_msg",
    )
    downloads = []
    stored = []
    download_root = []

    class Waha:
        async def download_media(self, url, path, *, max_bytes):
            downloads.append((url, max_bytes))
            download_root.append(path.parent)
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            path.write_bytes(b"\xff\xd8\xff" + b"supplier image")

    class Storage:
        def put_file(self, path, *, namespace, suffix):
            stored.append((path.read_bytes(), namespace, suffix))
            result_path = tmp_path / "stored-image.jpg"
            result_path.write_bytes(path.read_bytes())
            return SimpleNamespace(
                object_key="supplier-messages/hash.jpg",
                sha256="a" * 64,
                byte_size=result_path.stat().st_size,
            )

        def resolve(self, object_key):
            assert object_key == "supplier-messages/hash.jpg"
            return tmp_path / "stored-image.jpg"

    class ImageReader:
        async def read(self, path):
            assert path.read_bytes().startswith(b"\xff\xd8\xff")
            return SupplierImageReading(
                readable_text="500 тг",
                observations="Этикетка",
                language="ru",
                confidence=0.9,
                uncertain_numbers=True,
            )

    async def validate(path):
        assert path.read_bytes().startswith(b"\xff\xd8\xff")

    replies = []

    async def enqueue(_pool, conversation_id, message_id):
        replies.append((conversation_id, message_id))

    monkeypatch.setattr(media, "_validate_image", validate)
    monkeypatch.setattr(media, "_enqueue_reply", enqueue)
    await media.process_supplier_media_payload(
        {"conversation_id": str(pool.conversation_id), "message_id": str(pool.message_id)},
        pool=pool,
        storage=Storage(),
        waha=Waha(),
        transcriber=None,
        image_reader=ImageReader(),
        audio_probe=None,
        max_bytes=4096,
    )

    assert downloads == [(pool.row["remote_url"], 4096)]
    assert stored == [(b"\xff\xd8\xffsupplier image", "supplier-messages", ".jpg")]
    assert not download_root[0].exists()
    assert replies == [(pool.conversation_id, pool.message_id)]


@pytest.mark.asyncio
async def test_invalid_image_signature_is_not_sent_to_reader_or_storage(tmp_path, monkeypatch):
    pool = Pool(kind="image")
    pool.row.update(
        object_key=None,
        remote_url="http://127.0.0.1:3000/api/files/fake.jpg",
        external_chat_id="7700@c.us",
        external_message_id="true_7700@c.us_msg",
    )

    class Waha:
        async def download_media(self, _url, path, *, max_bytes):
            assert max_bytes == 1024
            path.write_bytes(b"not an image")

    class Storage:
        def put_file(self, *_args, **_kwargs):
            pytest.fail("invalid image bytes must not enter private storage")

    class ImageReader:
        async def read(self, _path):
            pytest.fail("invalid image bytes must not reach image reader")

    replies = []

    async def enqueue(_pool, conversation_id, message_id):
        replies.append((conversation_id, message_id))

    monkeypatch.setattr(media, "_enqueue_reply", enqueue)
    await media.process_supplier_media_payload(
        {"conversation_id": str(pool.conversation_id), "message_id": str(pool.message_id)},
        pool=pool,
        storage=Storage(),
        waha=Waha(),
        transcriber=None,
        image_reader=ImageReader(),
        audio_probe=None,
        max_bytes=1024,
    )

    assert pool.writes[-1][1][1] == "failed"
    assert pool.writes[-1][1][9] == "PermanentProviderError"
    assert replies == [(pool.conversation_id, pool.message_id)]


@pytest.mark.asyncio
async def test_media_download_cancellation_cleans_private_partial_directory(tmp_path, monkeypatch):
    pool = Pool(kind="image")
    pool.row.update(
        object_key=None,
        remote_url="http://127.0.0.1:3000/api/files/waiting.jpg",
        external_chat_id="7700@c.us",
        external_message_id="true_7700@c.us_msg",
    )
    started = asyncio.Event()
    download_path = []

    class Waha:
        async def download_media(self, _url, path, *, max_bytes):
            assert max_bytes == 1024
            path.write_bytes(b"partial")
            download_path.append(path)
            started.set()
            await asyncio.Future()

    class Storage:
        def put_file(self, *_args, **_kwargs):
            pytest.fail("cancelled download must not be stored")

    task = asyncio.create_task(
        media.process_supplier_media_payload(
            {"conversation_id": str(pool.conversation_id), "message_id": str(pool.message_id)},
            pool=pool,
            storage=Storage(),
            waha=Waha(),
            transcriber=None,
            image_reader=None,
            audio_probe=None,
            max_bytes=1024,
        )
    )
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not download_path[0].parent.exists()
    assert pool.writes == []
