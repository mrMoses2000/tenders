"""Durable supplier media processing outside database transactions."""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.supplier_dialogue import is_opt_out
from procurement_bot.supplier_media import SupplierMediaDeferred
from procurement_bot.transcription import transcript_review_reason


async def process_supplier_media_payload(
    payload: dict[str, Any],
    *,
    pool,
    storage,
    waha,
    transcriber,
    image_reader,
    audio_probe,
    max_bytes: int = 20 * 1024 * 1024,
    max_duration: int = 3600,
    minimum_language_confidence: float = 0.55,
    kazakh_transcriber=None,
) -> None:
    if set(payload) != {"message_id", "conversation_id"}:
        raise PermanentProviderError("supplier media payload invalid")
    try:
        message_id, conversation_id = UUID(payload["message_id"]), UUID(payload["conversation_id"])
    except (ValueError, TypeError) as exc:
        raise PermanentProviderError("supplier media identifiers invalid") from exc
    if (
        str(message_id) != payload["message_id"]
        or str(conversation_id) != payload["conversation_id"]
    ):
        raise PermanentProviderError("supplier media identifiers not canonical")
    row = await pool.fetchrow(
        """SELECT media.*,m.external_message_id,c.external_chat_id,c.opted_out,
                  c.case_id,p.status AS case_status
           FROM supplier_message_media media
           JOIN supplier_conversation_messages m ON m.id=media.message_id
           JOIN supplier_conversations c ON c.id=m.conversation_id
           JOIN procurement_cases p ON p.id=c.case_id
           WHERE m.id=$1 AND m.conversation_id=$2 AND m.direction='inbound'""",
        message_id,
        conversation_id,
    )
    if row is None:
        raise PermanentProviderError("supplier media evidence missing")
    if row["opted_out"] or row["case_status"] in {"closed", "cancelled"}:
        await pool.execute(
            """UPDATE supplier_message_media SET state='failed',
               last_error_code='conversation_closed',updated_at=now() WHERE message_id=$1""",
            message_id,
        )
        return
    if row["state"] in {"ready", "failed"}:
        await _enqueue_reply(pool, conversation_id, message_id)
        return
    if datetime.now(UTC) - row["created_at"] > timedelta(hours=4):
        await _finish(pool, message_id, text="", reason="media_processing_deadline")
        await _enqueue_reply(pool, conversation_id, message_id)
        return
    try:
        if not row["object_key"]:
            if waha is None:
                raise PermanentProviderError("supplier_media_waha_disabled")
            media_url = row["remote_url"]
            if not media_url:
                messages = await waha.fetch_chat_messages(
                    row["external_chat_id"],
                    limit=100,
                    download_media=True,
                )
                exact = [
                    message
                    for message in messages
                    if message.get("id") == row["external_message_id"]
                ]
                media_url = (exact[0].get("media") or {}).get("url") if len(exact) == 1 else None
            if not media_url:
                raise RetryableProviderError("supplier_media_url_unavailable")
            with tempfile.TemporaryDirectory(prefix="supplier-media-") as directory:
                path = Path(directory) / "media.bin"
                await waha.download_media(media_url, path, max_bytes=max_bytes)
                if row["kind"] == "audio":
                    await audio_probe(path, max_duration_seconds=max_duration)
                else:
                    await _validate_image(path)
                stored = await asyncio.to_thread(
                    storage.put_file,
                    path,
                    namespace="supplier-messages",
                    suffix=".audio" if row["kind"] == "audio" else _image_suffix(path),
                )
            await pool.execute(
                """UPDATE supplier_message_media SET object_key=$2,sha256=$3,byte_size=$4,
                   state='downloaded',updated_at=now() WHERE message_id=$1""",
                message_id,
                stored.object_key,
                stored.sha256,
                stored.byte_size,
            )
            row = dict(row) | {"object_key": stored.object_key, "sha256": stored.sha256}
        path = storage.resolve(row["object_key"])
        if row["kind"] == "image":
            if image_reader is None:
                raise PermanentProviderError("supplier_image_reader_disabled")
            reading = await image_reader.read(path)
            await _finish(
                pool,
                message_id,
                text=reading.readable_text,
                provider="codex-image",
                language=reading.language,
                confidence=reading.confidence,
                reason="photo_requires_supplier_confirmation",
                analysis=reading.model_dump(),
            )
        else:
            request_id = row["provider_request_id"]
            if not request_id:
                request_id = await transcriber.submit(path, language="auto")
                await pool.execute(
                    """UPDATE supplier_message_media SET provider_request_id=$2,
                       state='submitted',updated_at=now() WHERE message_id=$1""",
                    message_id,
                    request_id,
                )
            transcript = await transcriber.poll(request_id)
            if transcript is None:
                raise SupplierMediaDeferred(
                    datetime.now(UTC) + timedelta(seconds=20), "transcription_processing"
                )
            reason = transcript_review_reason(
                transcript,
                minimum_language_confidence=minimum_language_confidence,
            )
            fallback_error = None
            if (
                reason
                and kazakh_transcriber is not None
                and transcript.language in {"kk", "unknown"}
            ):
                try:
                    fallback = await kazakh_transcriber.transcribe(
                        path,
                        language="kk" if transcript.language == "kk" else "auto",
                    )
                except (PermanentProviderError, RetryableProviderError, TimeoutError) as exc:
                    # Keep usable primary evidence and ask the supplier to clarify.
                    # An optional provider outage must not erase the completed transcript.
                    fallback_error = type(exc).__name__
                else:
                    transcript = fallback
                    reason = transcript_review_reason(
                        transcript,
                        minimum_language_confidence=minimum_language_confidence,
                    )
            speech_confidence = getattr(transcript, "speech_confidence", None)
            uncertain = getattr(transcript, "low_confidence_words", ())
            if speech_confidence is None or speech_confidence < 0.75:
                reason = reason or "speech_confidence_low_or_missing"
            if uncertain:
                reason = "speech_words_uncertain"
            if is_opt_out(transcript.text):
                await _project_voice_opt_out(pool, conversation_id, message_id, row["case_id"])
            await _finish(
                pool,
                message_id,
                text=transcript.text,
                provider=transcript.provider,
                language=transcript.language,
                confidence=transcript.confidence,
                reason=reason,
                analysis={
                    "speech_confidence": speech_confidence,
                    "low_confidence_words": list(uncertain),
                    "fallback_error": fallback_error,
                },
            )
    except PermanentProviderError as exc:
        if row["kind"] == "audio" and row["object_key"] and kazakh_transcriber is not None:
            transcript = await kazakh_transcriber.transcribe(
                storage.resolve(row["object_key"]),
                language="auto",
            )
            if is_opt_out(transcript.text):
                await _project_voice_opt_out(pool, conversation_id, message_id, row["case_id"])
            await _finish(
                pool,
                message_id,
                text=transcript.text,
                provider=transcript.provider,
                language=transcript.language,
                reason="fallback_speech_requires_confirmation",
                analysis={"fallback": True, "primary_error": type(exc).__name__},
            )
        else:
            await _finish(
                pool, message_id, text="", reason="media_provider_failed", error=type(exc).__name__
            )
    await _enqueue_reply(pool, conversation_id, message_id)


async def _project_voice_opt_out(pool, conversation_id: UUID, message_id: UUID, case_id: UUID):
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """UPDATE supplier_conversations SET state='opted_out',opted_out=true,
               opted_out_at=COALESCE(opted_out_at,now()),
               opt_out_reason='supplier_voice_requested',updated_at=now()
               WHERE id=$1""",
            conversation_id,
        )
        await connection.execute(
            """INSERT INTO workflow_events(case_id,event_type,actor_type,actor_id,
               idempotency_key,data) VALUES ($1,'supplier_voice_opt_out','worker',
               'supplier-media',$2,$3::jsonb)
               ON CONFLICT(idempotency_key) DO NOTHING""",
            case_id,
            stable_key("supplier-voice-opt-out", message_id),
            {"message_id": str(message_id), "conversation_id": str(conversation_id)},
        )


async def _finish(
    pool,
    message_id: UUID,
    *,
    text: str,
    provider: str | None = None,
    language: str | None = None,
    confidence: float | None = None,
    reason: str | None = None,
    analysis: dict | None = None,
    error: str | None = None,
) -> None:
    await pool.execute(
        """UPDATE supplier_message_media SET state=$2,extracted_text=$3,provider=$4,
           language=$5,confidence=$6,needs_clarification=$7,clarification_reason=$8,
           analysis_json=$9::jsonb,last_error_code=$10,updated_at=now() WHERE message_id=$1""",
        message_id,
        "failed" if error else "ready",
        text,
        provider,
        language,
        confidence,
        bool(reason),
        reason,
        analysis or {},
        error or "",
    )


async def _enqueue_reply(pool, conversation_id: UUID, message_id: UUID) -> None:
    async with pool.acquire() as connection, connection.transaction():
        await enqueue_job(
            connection,
            kind="process_supplier_reply",
            payload={"conversation_id": str(conversation_id), "message_id": str(message_id)},
            idempotency_key=stable_key("supplier-media-reply", message_id),
            max_attempts=20,
        )


async def _validate_image(path: Path) -> None:
    with path.open("rb") as source:
        header = source.read(16)
    if not (
        header.startswith(b"\xff\xd8\xff")
        or header.startswith(b"\x89PNG\r\n\x1a\n")
        or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    ):
        raise PermanentProviderError("supplier_image_format_unsupported")
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
        streams = json.loads(output).get("streams", [])
        width, height = streams[0]["width"], streams[0]["height"]
        if process.returncode or not 0 < width * height <= 24000000:
            raise PermanentProviderError("supplier_image_dimensions_invalid")
    except (ValueError, KeyError, IndexError) as exc:
        raise PermanentProviderError("supplier_image_invalid") from exc
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def _image_suffix(path: Path) -> str:
    with path.open("rb") as source:
        header = source.read(16)
    if header.startswith(b"\x89PNG"):
        return ".png"
    if header[:4] == b"RIFF":
        return ".webp"
    return ".jpg"
