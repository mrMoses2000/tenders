from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import zipfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from uuid import UUID

import asyncpg
from aiogram import Bot

from procurement_bot.approvals import request_approval
from procurement_bot.case_queries import answer_case_query, parse_case_query
from procurement_bot.cases import (
    StaleIntakeContext,
    load_intake_context,
    persist_intake_result,
)
from procurement_bot.dialogue_routing import (
    DialogueIntent,
    DialogueRoute,
    finish_dialogue_turn,
    load_dialogue_turn,
    ordinary_reply,
    persist_dialogue_route,
    route_dialogue_turn,
)
from procurement_bot.documents import extract_document, joined_text
from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.intake import (
    IntakeClarificationContext,
    IntakeService,
    ParsedProcurementRequest,
    SourceType,
)
from procurement_bot.locality_repository import PostgreSQLLocalityResolutionRepository
from procurement_bot.locality_workflow import resolve_case_locality
from procurement_bot.providers.browser import BrowserResearchPort
from procurement_bot.purchase_workflow import (
    mark_purchase_artifact_ready,
    parse_close_request,
    request_purchase_evidence,
)
from procurement_bot.queue import (
    ClaimedJob,
    claim_job,
    complete_job,
    enqueue_job,
    enqueue_outbox,
    fail_job,
    renew_job_lease,
    stable_key,
)
from procurement_bot.report_pipeline import build_case_report
from procurement_bot.research import LocalityResolution, build_research_plan
from procurement_bot.research_repository import persist_research_plan
from procurement_bot.research_workflow import start_approved_research
from procurement_bot.storage import LocalContentAddressedStorage
from procurement_bot.supplier_dialogue import WhatsAppSendCommand
from procurement_bot.supplier_outreach import prepare_supplier_outreach
from procurement_bot.supplier_reply_processing import process_supplier_reply
from procurement_bot.transcription import Transcriber, transcript_review_reason
from procurement_bot.whatsapp_approval_workflow import start_approved_whatsapp_send

LOGGER = logging.getLogger(__name__)


class ResearchRunExecutor(Protocol):
    """Live read-only adapter; implementations own browser/network side effects."""

    async def execute(self, research_run_id: UUID) -> Any: ...


class JobWorker:
    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        bot: Bot,
        storage: LocalContentAddressedStorage,
        transcriber: Transcriber,
        intake: IntakeService,
        worker_id: str,
        lease_seconds: int = 300,
        max_file_bytes: int = 2 * 1024 * 1024 * 1024,
        max_audio_duration_seconds: int = 3600,
        research_executor: ResearchRunExecutor | None = None,
        locality_browser: BrowserResearchPort | None = None,
        results_root: Path = Path("./var/results"),
        whatsapp_enabled: bool = False,
        transcription_min_language_confidence: float = 0.55,
        waha_session: str = "default",
    ) -> None:
        self.pool = pool
        self.bot = bot
        self.storage = storage
        self.transcriber = transcriber
        self.intake = intake
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_file_bytes = max_file_bytes
        self.max_audio_duration_seconds = max_audio_duration_seconds
        self.research_executor = research_executor
        self.locality_browser = locality_browser
        self.results_root = results_root.expanduser().resolve()
        self.whatsapp_enabled = whatsapp_enabled
        self.transcription_min_language_confidence = transcription_min_language_confidence
        self.waha_session = waha_session

    async def run_forever(self, *, idle_seconds: float = 0.5) -> None:
        while True:
            worked = await self.run_once()
            if not worked:
                await asyncio.sleep(idle_seconds)

    async def run_once(self) -> bool:
        job = await claim_job(
            self.pool,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            await self._dispatch(job)
            if not await complete_job(self.pool, job.id, self.worker_id):
                raise StaleIntakeContext("job lease expired before completion")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the durable boundary must classify every failure
            retryable = self._is_retryable(exc)
            LOGGER.exception(
                "job failed",
                extra={"job_id": job.id, "error": type(exc).__name__},
            )
            try:
                terminal = await fail_job(
                    self.pool,
                    job,
                    type(exc).__name__,
                    retryable=retryable,
                )
                if terminal:
                    await self._record_terminal_failure(job, type(exc).__name__)
            except Exception:
                LOGGER.exception(
                    "could not persist job failure",
                    extra={"job_id": job.id, "error": type(exc).__name__},
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _heartbeat(self, job: ClaimedJob) -> None:
        interval = max(5, self.lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await renew_job_lease(
                self.pool,
                job.id,
                self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if not renewed:
                LOGGER.error("job lease could not be renewed", extra={"job_id": job.id})
                return

    async def _dispatch(self, job: ClaimedJob) -> None:
        if job.kind == "download_attachment":
            await self._download_attachment(job.payload)
        elif job.kind == "extract_document":
            await self._extract_document(job.payload)
        elif job.kind == "transcribe_voice":
            await self._transcribe_voice(job.payload)
        elif job.kind == "process_intake":
            await self._process_intake(job.payload)
        elif job.kind == "prepare_research":
            await self._prepare_research(job.payload)
        elif job.kind == "start_research":
            await self._start_research(job.payload)
        elif job.kind == "execute_research":
            await self._execute_research(job.payload)
        elif job.kind == "build_report":
            await self._build_report(job.payload)
        elif job.kind == "prepare_supplier_outreach":
            await self._prepare_supplier_outreach(job.payload)
        elif job.kind == "process_supplier_reply":
            await self._process_supplier_reply(job.payload)
        elif job.kind == "enqueue_whatsapp_send":
            await self._enqueue_whatsapp_send(job.payload)
        elif job.kind == "resolve_locality":
            await self._resolve_locality(job.payload)
        else:
            raise PermanentProviderError(f"unknown job kind: {job.kind}")

    async def _download_attachment(self, payload: dict[str, Any]) -> None:
        attachment_id = UUID(str(payload["attachment_id"]))
        row = await self.pool.fetchrow(
            """
            SELECT a.*,m.external_chat_id
            FROM incoming_attachments AS a
            JOIN messages AS m ON m.id=a.message_id
            WHERE a.id=$1
            """,
            attachment_id,
        )
        if row is None:
            raise PermanentProviderError("attachment no longer exists")
        if row["status"] == "downloaded" and row["storage_path"]:
            await self._enqueue_attachment_processing(row)
            return
        declared_size = row["declared_size"]
        if declared_size is not None and declared_size > self.max_file_bytes:
            raise PermanentProviderError("declared attachment exceeds configured size limit")
        await self.pool.execute(
            "UPDATE incoming_attachments SET status='downloading',updated_at=now() WHERE id=$1",
            attachment_id,
        )
        suffix = Path(row["original_filename"]).suffix.lower()[:20]
        with tempfile.TemporaryDirectory(prefix="procurement-download-") as directory:
            temporary = Path(directory) / f"source{suffix}"
            telegram_file = await self.bot.get_file(row["external_file_id"])
            if not telegram_file.file_path:
                raise RetryableProviderError("Telegram returned no file path")
            with temporary.open("w+b") as destination:
                await self.bot.download_file(
                    telegram_file.file_path,
                    destination=_LimitedWriter(destination, self.max_file_bytes),
                )
            size = temporary.stat().st_size
            if size > self.max_file_bytes:
                raise PermanentProviderError("downloaded attachment exceeds configured size limit")
            _validate_downloaded(temporary, row["kind"], row["mime_type"])
            stored = self.storage.put_file(
                temporary,
                namespace="telegram",
                suffix=suffix,
            )
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE incoming_attachments
                SET status='downloaded',storage_path=$2,sha256=$3,error_code='',updated_at=now()
                WHERE id=$1
                """,
                attachment_id,
                stored.object_key,
                stored.sha256,
            )
            source_artifact_id = await connection.fetchval(
                """
                INSERT INTO source_artifacts(
                    attachment_id,artifact_kind,storage_path,sha256,byte_size,mime_type
                ) VALUES ($1,'original',$2,$3,$4,$5)
                ON CONFLICT (attachment_id,artifact_kind,sha256) DO NOTHING
                RETURNING id
                """,
                attachment_id,
                stored.object_key,
                stored.sha256,
                stored.byte_size,
                row["mime_type"],
            )
            if source_artifact_id is None:
                source_artifact_id = await connection.fetchval(
                    """
                    SELECT id FROM source_artifacts
                    WHERE attachment_id=$1 AND artifact_kind='original' AND sha256=$2
                    """,
                    attachment_id,
                    stored.sha256,
                )
            if source_artifact_id is None:
                raise RuntimeError("source artifact insert conflict vanished")
            purchase_request = await mark_purchase_artifact_ready(
                connection,
                attachment_id=attachment_id,
                source_artifact_id=source_artifact_id,
            )
            if purchase_request is not None:
                await enqueue_outbox(
                    connection,
                    event_type="telegram.send_message",
                    payload={
                        "chat_id": int(purchase_request["telegram_id"]),
                        "text": (
                            "Чек сохранил. Теперь напишите количество и итоговую "
                            "сумму покупки."
                        ),
                    },
                    idempotency_key=stable_key(
                        "telegram.purchase-evidence-ready",
                        purchase_request["id"],
                    ),
                )
                return
            await self._enqueue_attachment_processing(row, connection=connection)

    async def _enqueue_attachment_processing(
        self,
        row: asyncpg.Record,
        *,
        connection: asyncpg.Connection | None = None,
    ) -> None:
        kind = row["kind"]
        if kind == "document":
            job_kind = "extract_document"
        elif kind in {"voice", "audio"}:
            job_kind = "transcribe_voice"
        elif kind == "photo":
            if connection is None:
                async with self.pool.acquire() as acquired, acquired.transaction():
                    await self._enqueue_photo_notice(acquired, row)
            else:
                await self._enqueue_photo_notice(connection, row)
            return
        else:
            raise PermanentProviderError(f"unsupported attachment kind: {kind}")
        payload = {
            "attachment_id": str(row["id"]),
            "incoming_message_id": str(row["message_id"]),
        }
        if connection is None:
            async with self.pool.acquire() as acquired, acquired.transaction():
                await enqueue_job(
                    acquired,
                    kind=job_kind,
                    payload=payload,
                    idempotency_key=stable_key(job_kind, row["id"]),
                )
        else:
            await enqueue_job(
                connection,
                kind=job_kind,
                payload=payload,
                idempotency_key=stable_key(job_kind, row["id"]),
            )

    @staticmethod
    async def _enqueue_photo_notice(
        connection: asyncpg.Connection,
        row: asyncpg.Record,
    ) -> None:
        await enqueue_outbox(
            connection,
            event_type="telegram.send_message",
            payload={
                "chat_id": int(row["external_chat_id"]),
                "text": (
                    "Я пока не умею надёжно читать заявку с фотографии. "
                    "Пришлите PDF/DOCX или продублируйте текст сообщением."
                ),
            },
            idempotency_key=stable_key("telegram.photo-notice", row["id"]),
        )

    async def _extract_document(self, payload: dict[str, Any]) -> None:
        attachment_id = UUID(str(payload["attachment_id"]))
        row = await self._load_downloaded_attachment(attachment_id)
        suffix = Path(row["original_filename"]).suffix.casefold()
        document_kind = "pdf" if suffix == ".pdf" else "docx" if suffix == ".docx" else ""
        if not document_kind:
            raise PermanentProviderError("document extension is not supported")
        segments = await asyncio.to_thread(
            extract_document,
            self.storage.resolve(row["storage_path"]),
            document_kind,
        )
        text = joined_text(segments, max_chars=49_000)
        if not text.strip():
            raise PermanentProviderError("document has no extractable text")
        extraction_id = await self._save_extraction(
            attachment_id=attachment_id,
            kind="document_text",
            engine=f"native-{document_kind}",
            text=text,
            content_json={"segments": [segment.__dict__ for segment in segments]},
        )
        await self._enqueue_intake(row, extraction_id, "document")

    async def _transcribe_voice(self, payload: dict[str, Any]) -> None:
        attachment_id = UUID(str(payload["attachment_id"]))
        row = await self._load_downloaded_attachment(attachment_id)
        path = self.storage.resolve(row["storage_path"])
        await _probe_audio(path, max_duration_seconds=self.max_audio_duration_seconds)
        transcript = await self.transcriber.transcribe(path)
        extraction_id = await self._save_extraction(
            attachment_id=attachment_id,
            kind="transcript",
            engine=transcript.provider,
            text=transcript.text,
            content_json={
                "confidence": transcript.confidence,
                "detected_language": transcript.language,
            },
            language=transcript.language,
        )
        review_reason = transcript_review_reason(
            transcript,
            minimum_language_confidence=self.transcription_min_language_confidence,
        )
        if review_reason is not None:
            async with self.pool.acquire() as connection, connection.transaction():
                chat_id = await connection.fetchval(
                    """
                    SELECT message.external_chat_id
                    FROM incoming_attachments AS attachment
                    JOIN messages AS message ON message.id=attachment.message_id
                    WHERE attachment.id=$1
                    """,
                    attachment_id,
                )
                if chat_id is None:
                    raise PermanentProviderError("voice message chat is unavailable")
                await enqueue_outbox(
                    connection,
                    event_type="telegram.send_message",
                    payload={
                        "chat_id": int(chat_id),
                        "text": (
                            "Я сохранил расшифровку, но недостаточно уверенно определил "
                            "русский/казахский язык. Пришлите запрос текстом или повторите "
                            "голосовое в более тихом месте."
                        ),
                    },
                    idempotency_key=stable_key(
                        "telegram.transcription-review",
                        attachment_id,
                        review_reason,
                    ),
                )
            return
        await self._enqueue_intake(row, extraction_id, "voice")

    async def _load_downloaded_attachment(self, attachment_id: UUID) -> asyncpg.Record:
        row = await self.pool.fetchrow(
            "SELECT * FROM incoming_attachments WHERE id=$1",
            attachment_id,
        )
        if row is None or row["status"] != "downloaded" or not row["storage_path"]:
            raise RetryableProviderError("attachment is not downloaded")
        return row

    async def _save_extraction(
        self,
        *,
        attachment_id: UUID,
        kind: str,
        engine: str,
        text: str,
        content_json: dict[str, Any],
        language: str = "",
    ) -> UUID:
        digest = hashlib.sha256(text.encode()).hexdigest()
        return await self.pool.fetchval(
            """
            INSERT INTO content_extractions(
                attachment_id,extraction_kind,version,engine,status,content_text,
                content_json,content_sha256,language_code,completed_at
            ) VALUES ($1,$2,1,$3,'succeeded',$4,$5::jsonb,$6,$7,now())
            ON CONFLICT (attachment_id,extraction_kind,version) DO UPDATE SET
                status='succeeded',content_text=EXCLUDED.content_text,
                content_json=EXCLUDED.content_json,content_sha256=EXCLUDED.content_sha256,
                language_code=EXCLUDED.language_code,error_code='',completed_at=now()
            RETURNING id
            """,
            attachment_id,
            kind,
            engine,
            text,
            content_json,
            digest,
            language,
        )

    async def _enqueue_intake(
        self,
        attachment: asyncpg.Record,
        extraction_id: UUID,
        input_kind: str,
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            await enqueue_job(
                connection,
                kind="process_intake",
                payload={
                    "incoming_message_id": str(attachment["message_id"]),
                    "extraction_id": str(extraction_id),
                    "input_kind": input_kind,
                },
                idempotency_key=stable_key("process-intake", extraction_id),
            )

    async def _process_intake(self, payload: dict[str, Any]) -> None:
        message_id = UUID(str(payload["incoming_message_id"]))
        extraction_id = (
            UUID(str(payload["extraction_id"])) if payload.get("extraction_id") else None
        )
        row = await self.pool.fetchrow(
            """
            SELECT m.text_content,m.raw_payload,m.user_id,m.external_chat_id,e.content_text
            FROM messages AS m
            LEFT JOIN content_extractions AS e ON e.id=$2
            WHERE m.id=$1
            """,
            message_id,
            extraction_id,
        )
        if row is None:
            raise PermanentProviderError("intake source message no longer exists")
        input_kind = str(payload.get("input_kind", "text"))
        source_type = {
            "document": SourceType.DOCUMENT,
            "voice": SourceType.VOICE,
            "audio": SourceType.VOICE,
        }.get(input_kind, SourceType.TEXT)
        body = row["content_text"] or _location_text(row["raw_payload"]) or row["text_content"]
        caption = row["text_content"] or ""
        if row["content_text"] and caption:
            body = f"Комментарий пользователя:\n{caption}\n\nСодержимое:\n{body}"
        if not body.strip():
            raise PermanentProviderError("intake source contains no text")
        if row["user_id"] is None:
            raise PermanentProviderError("dialogue message has no authenticated owner")

        context = await load_intake_context(self.pool, message_id)
        async with self.pool.acquire() as connection, connection.transaction():
            turn = await load_dialogue_turn(connection, message_id)
            if turn is None:
                route = route_dialogue_turn(
                    body,
                    input_kind=input_kind,
                    active_case_id=context.case_id,
                    has_open_clarification=context.open_clarification_id is not None,
                )
                turn = await persist_dialogue_route(
                    connection,
                    message_id=message_id,
                    user_id=row["user_id"],
                    case_id=context.case_id,
                    route=route,
                    input_kind=input_kind,
                    context_version=context.version,
                    open_clarification_id=context.open_clarification_id,
                )
            else:
                route = DialogueRoute(turn.intent, turn.reason_code)
            if turn.status in {"applied", "ignored"}:
                return

        if route.intent == DialogueIntent.CLOSE_POSITION:
            close_request = parse_close_request(body)
            if close_request is None:
                raise PermanentProviderError("persisted close-position route is invalid")
            async with self.pool.acquire() as connection, connection.transaction():
                result = await request_purchase_evidence(
                    connection,
                    owner_user_id=row["user_id"],
                    source_message_id=message_id,
                    statement=body,
                    request=close_request,
                )
                await enqueue_outbox(
                    connection,
                    event_type="telegram.send_message",
                    payload={
                        "chat_id": int(row["external_chat_id"]),
                        "text": result.text,
                    },
                    idempotency_key=stable_key("telegram.purchase-evidence", message_id),
                )
                await finish_dialogue_turn(
                    connection,
                    message_id=message_id,
                    case_id=result.case_id,
                    status="applied",
                    response_kind="purchase_evidence_request",
                )
            return
        if route.intent == DialogueIntent.CASE_QUERY:
            case_query = parse_case_query(body)
            if case_query is None:
                raise PermanentProviderError("persisted case-query route is invalid")
            async with self.pool.acquire() as connection, connection.transaction():
                answer = await answer_case_query(
                    connection,
                    owner_user_id=row["user_id"],
                    query=case_query,
                )
                await enqueue_outbox(
                    connection,
                    event_type="telegram.send_message",
                    payload={
                        "chat_id": int(row["external_chat_id"]),
                        "text": answer.text,
                    },
                    idempotency_key=stable_key("telegram.case-query", message_id),
                )
                await finish_dialogue_turn(
                    connection,
                    message_id=message_id,
                    case_id=answer.case_id,
                    status="applied",
                    response_kind="case_query_answer",
                )
            return

        if route.intent in {
            DialogueIntent.ORDINARY,
            DialogueIntent.ACTIVE_CASE_CONFLICT,
        }:
            text = ordinary_reply(
                active_case_title=context.case_title,
                reason_code=route.reason_code,
            )
            async with self.pool.acquire() as connection, connection.transaction():
                await enqueue_outbox(
                    connection,
                    event_type="telegram.send_message",
                    payload={"chat_id": int(row["external_chat_id"]), "text": text},
                    idempotency_key=stable_key("telegram.dialogue-help", message_id),
                )
                await finish_dialogue_turn(
                    connection,
                    message_id=message_id,
                    case_id=context.case_id,
                    status="ignored",
                    response_kind="dialogue_help",
                )
            return

        if not route.permits_intake:
            raise PermanentProviderError("dialogue route cannot enter intake")
        if turn.context_version != context.version or turn.case_id != context.case_id:
            already_applied = await self.pool.fetchval(
                "SELECT case_id FROM case_parse_versions WHERE source_message_id=$1",
                message_id,
            )
            if already_applied is not None:
                async with self.pool.acquire() as connection, connection.transaction():
                    await finish_dialogue_turn(
                        connection,
                        message_id=message_id,
                        case_id=already_applied,
                        status="applied",
                        response_kind="intake",
                    )
                return
            raise StaleIntakeContext("dialogue context changed before intake")
        open_clarification = None
        if context.open_clarification_topic and context.open_clarification_question:
            open_clarification = IntakeClarificationContext(
                topic=context.open_clarification_topic,
                question=context.open_clarification_question,
            )
        result = await self.intake.process(
            body,
            current=context.request,
            source_type=source_type,
            open_clarification=open_clarification,
        )
        await persist_intake_result(
            self.pool,
            message_id=message_id,
            source_extraction_id=extraction_id,
            context=context,
            result=result,
        )

    async def _prepare_research(self, payload: dict[str, Any]) -> None:
        """Persist a deterministic plan only; no browser or external action runs here."""

        case_id = UUID(str(payload["case_id"]))
        intake_version = int(payload["intake_version"])
        expected_keys = {"case_id", "intake_version"}
        locality_keys = {
            "locality_resolution_id",
            "locality_resolution_version",
        }
        if frozenset(payload) not in {
            frozenset(expected_keys),
            frozenset(expected_keys | locality_keys),
        }:
            raise PermanentProviderError("prepare_research payload is invalid")
        row = await self.pool.fetchrow(
            """
            SELECT c.status,c.owner_user_id,u.telegram_id,
                p.version,p.normalized_payload
            FROM procurement_cases AS c
            JOIN users AS u ON u.id=c.owner_user_id
            JOIN case_parse_versions AS p ON p.case_id=c.id AND p.status='applied'
            WHERE c.id=$1
            """,
            case_id,
        )
        if row is None:
            raise PermanentProviderError("research case no longer exists")
        if row["status"] not in {"ready", "needs_clarification"} or (
            row["version"] != intake_version
        ):
            raise PermanentProviderError("research request is stale or not ready")
        request = ParsedProcurementRequest.model_validate(row["normalized_payload"])
        locality_resolution: LocalityResolution | None = None
        if locality_keys.issubset(payload):
            resolution_id = UUID(str(payload["locality_resolution_id"]))
            resolution_version = int(payload["locality_resolution_version"])
            resolution_row = await self.pool.fetchrow(
                """
                SELECT result_payload,status
                FROM locality_resolutions
                WHERE id=$1 AND case_id=$2 AND intake_version=$3 AND version=$4
                """,
                resolution_id,
                case_id,
                intake_version,
                resolution_version,
            )
            if resolution_row is None or resolution_row["status"] != "resolved":
                raise PermanentProviderError("locality resolution is absent or stale")
            locality_resolution = LocalityResolution.model_validate(
                resolution_row["result_payload"]
            )
        plan = build_research_plan(
            request,
            locality_resolution=locality_resolution,
        )
        async with self.pool.acquire() as connection, connection.transaction():
            persisted = await persist_research_plan(
                connection,
                case_id=case_id,
                plan=plan,
                requested_by=row["owner_user_id"],
            )
            blocked = sum(
                branch.blocked_reason is not None for branch in plan.source_plans
            )
            approval_id: UUID | None = None
            if row["status"] == "needs_clarification" and not blocked:
                raise PermanentProviderError("case is no longer ready for research")
            if blocked:
                if row["status"] == "ready":
                    updated = await connection.execute(
                        """
                        UPDATE procurement_cases
                        SET status='needs_clarification',updated_at=now()
                        WHERE id=$1 AND status='ready'
                        """,
                        case_id,
                    )
                    if updated != "UPDATE 1":
                        raise PermanentProviderError(
                            "case changed before locality clarification"
                        )
                phrase = plan.locality_task.phrase if plan.locality_task else "указанную зону"
                if self.locality_browser is not None:
                    await enqueue_job(
                        connection,
                        kind="resolve_locality",
                        payload={
                            "case_id": str(case_id),
                            "intake_version": intake_version,
                            "owner_user_id": str(row["owner_user_id"]),
                        },
                        idempotency_key=stable_key(
                            "resolve-locality",
                            case_id,
                            intake_version,
                            phrase,
                        ),
                    )
                    text = f"Уточняю по открытым источникам, что означает «{phrase}» в этом городе."
                else:
                    question = (
                        f"Что именно имеется в виду под «{phrase}»? "
                        "Пришлите ближайший ориентир или геолокацию."
                    )
                    await connection.execute(
                        """
                        INSERT INTO clarifications(case_id,topic,question)
                        SELECT $1,'locality_resolution',$2
                        WHERE NOT EXISTS (
                            SELECT 1 FROM clarifications
                            WHERE case_id=$1 AND topic='locality_resolution' AND status='open'
                        )
                        """,
                        case_id,
                        question,
                    )
                    text = question
            elif self.research_executor is not None:
                approval_payload = {
                    "case_id": str(case_id),
                    "research_run_id": str(persisted.id),
                    "plan_sha256": plan.plan_sha256,
                }
                approval_id = await request_approval(
                    connection,
                    case_id=case_id,
                    action_type="start_research",
                    payload=approval_payload,
                    requested_by=row["owner_user_id"],
                    expires_at=datetime.now(UTC) + timedelta(hours=24),
                )
                text = (
                    "Всё готово к поиску. Проверю свою базу, 2GIS и интернет, "
                    "затем соберу лучшие варианты. Начать?"
                )
            else:
                text = "Заявка готова, но автоматический поиск сейчас временно недоступен."
            await enqueue_outbox(
                connection,
                event_type="telegram.send_message",
                payload={
                    "chat_id": int(row["telegram_id"]),
                    "text": text,
                    **(
                        {
                            "reply_markup": {
                                "inline_keyboard": [
                                    [
                                        {
                                            "text": "Запустить поиск",
                                            "callback_data": f"rs:a:{approval_id}",
                                        },
                                        {
                                            "text": "Отмена",
                                            "callback_data": f"rs:r:{approval_id}",
                                        },
                                    ]
                                ]
                            }
                        }
                        if approval_id is not None
                        else {}
                    ),
                },
                idempotency_key=stable_key("telegram.research-plan", persisted.id),
            )

    async def _resolve_locality(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"case_id", "intake_version", "owner_user_id"}:
            raise PermanentProviderError("resolve_locality payload is invalid")
        if self.locality_browser is None:
            raise PermanentProviderError("live locality resolver is not configured")
        try:
            case_id = UUID(str(payload["case_id"]))
            owner_user_id = UUID(str(payload["owner_user_id"]))
            intake_version = int(payload["intake_version"])
        except (ValueError, TypeError) as exc:
            raise PermanentProviderError("resolve_locality identifiers are invalid") from exc
        if str(case_id) != payload["case_id"] or str(owner_user_id) != payload["owner_user_id"]:
            raise PermanentProviderError("resolve_locality UUID is not canonical")
        repository = PostgreSQLLocalityResolutionRepository(self.pool)
        await resolve_case_locality(
            repository,
            self.locality_browser,
            case_id=case_id,
            intake_version=intake_version,
            owner_user_id=owner_user_id,
        )

    async def _start_research(self, payload: dict[str, Any]) -> None:
        """Consume the exact approval only when a live executor is configured."""

        if set(payload) != {"approval_id"}:
            raise PermanentProviderError("start_research job payload is invalid")
        if self.research_executor is None:
            raise PermanentProviderError("live research executor is not configured")
        approval_text = payload["approval_id"]
        try:
            approval_id = UUID(str(approval_text))
        except ValueError as exc:
            raise PermanentProviderError("start_research approval id is invalid") from exc
        if str(approval_id) != approval_text:
            raise PermanentProviderError("start_research approval id is not canonical")
        await start_approved_research(self.pool, approval_id=approval_id)

    async def _execute_research(self, payload: dict[str, Any]) -> None:
        """Delegate one opaque run id to the separately configured live adapter."""

        if set(payload) != {"research_run_id"}:
            raise PermanentProviderError("execute_research job payload is invalid")
        if self.research_executor is None:
            raise PermanentProviderError("live research executor is not configured")
        run_text = payload["research_run_id"]
        try:
            run_id = UUID(str(run_text))
        except ValueError as exc:
            raise PermanentProviderError("research run id is invalid") from exc
        if str(run_id) != run_text:
            raise PermanentProviderError("research run id is not canonical")
        result = await self.research_executor.execute(run_id)
        # Keep the port backwards-compatible with external executors that only
        # perform the run. The built-in executor returns the durable terminal
        # projection needed to schedule reporting.
        if result is None:
            return
        if result.status in {"succeeded", "partial"}:
            async with self.pool.acquire() as connection, connection.transaction():
                await enqueue_job(
                    connection,
                    kind="build_report",
                    payload={
                        "case_id": str(result.case_id),
                        "research_run_id": str(run_id),
                    },
                    idempotency_key=stable_key("build-report", result.case_id, run_id),
                )
                if getattr(self, "whatsapp_enabled", False):
                    await enqueue_job(
                        connection,
                        kind="prepare_supplier_outreach",
                        payload={"case_id": str(result.case_id)},
                        idempotency_key=stable_key(
                            "prepare-supplier-outreach",
                            result.case_id,
                            run_id,
                        ),
                    )

    async def _prepare_supplier_outreach(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"case_id"}:
            raise PermanentProviderError("prepare_supplier_outreach payload is invalid")
        if not self.whatsapp_enabled:
            raise PermanentProviderError("WhatsApp outreach is disabled")
        case_text = payload["case_id"]
        try:
            case_id = UUID(str(case_text))
        except (TypeError, ValueError) as exc:
            raise PermanentProviderError("outreach case id is invalid") from exc
        if str(case_id) != case_text:
            raise PermanentProviderError("outreach case id is not canonical")
        async with self.pool.acquire() as connection:
            await prepare_supplier_outreach(
                connection,
                case_id=case_id,
                session_name=self.waha_session,
            )

    async def _build_report(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"case_id", "research_run_id"}:
            raise PermanentProviderError("build_report job payload is invalid")
        try:
            case_id = UUID(str(payload["case_id"]))
            research_run_id = UUID(str(payload["research_run_id"]))
        except (TypeError, ValueError) as exc:
            raise PermanentProviderError("build_report identifiers are invalid") from exc
        if (
            str(case_id) != payload["case_id"]
            or str(research_run_id) != payload["research_run_id"]
        ):
            raise PermanentProviderError("build_report identifiers are not canonical")
        async with self.pool.acquire() as connection:
            await build_case_report(
                connection,
                results_root=self.results_root,
                case_id=case_id,
                research_run_id=research_run_id,
            )

    async def _enqueue_whatsapp_send(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"approval_id"}:
            raise PermanentProviderError("enqueue_whatsapp_send payload is invalid")
        approval_text = payload["approval_id"]
        try:
            approval_id = UUID(str(approval_text))
        except (TypeError, ValueError) as exc:
            raise PermanentProviderError("WhatsApp approval id is invalid") from exc
        if str(approval_id) != approval_text:
            raise PermanentProviderError("WhatsApp approval id is not canonical")
        await start_approved_whatsapp_send(self.pool, approval_id)

    async def _process_supplier_reply(self, payload: dict[str, Any]) -> None:
        """Project explicit supplier facts and notify the owner; never auto-send."""

        if set(payload) != {"conversation_id", "message_id"}:
            raise PermanentProviderError("process_supplier_reply payload is invalid")
        conversation_text = payload["conversation_id"]
        message_text = payload["message_id"]
        try:
            conversation_id = UUID(str(conversation_text))
            message_id = UUID(str(message_text))
        except ValueError as exc:
            raise PermanentProviderError("supplier reply identifiers are invalid") from exc
        if str(conversation_id) != conversation_text or str(message_id) != message_text:
            raise PermanentProviderError("supplier reply identifiers are not canonical")
        async with self.pool.acquire() as connection, connection.transaction():
            result = await process_supplier_reply(
                connection,
                conversation_id=conversation_id,
                message_id=message_id,
            )
            if not result.newly_processed:
                return
            owner = await connection.fetchrow(
                """
                SELECT owner.id AS owner_user_id,owner.telegram_id
                FROM procurement_cases AS procurement_case
                JOIN users AS owner ON owner.id=procurement_case.owner_user_id
                WHERE procurement_case.id=$1 AND owner.active
                """,
                result.case_id,
            )
            if owner is None or owner["telegram_id"] is None:
                raise PermanentProviderError("case owner is unavailable")
            reply_markup: dict[str, Any] | None = None
            if result.needs_human:
                text = "Ответ поставщика неоднозначный. Показываю его вам для проверки."
            elif result.draft_outbound_text:
                if getattr(self, "whatsapp_enabled", False):
                    recipient = await connection.fetchval(
                        """
                        SELECT contact.normalized_value
                        FROM supplier_conversations AS conversation
                        JOIN supplier_contacts AS contact
                          ON contact.id=conversation.supplier_contact_id
                        WHERE conversation.id=$1
                          AND conversation.case_id=$2
                          AND contact.active
                          AND contact.contact_type=ANY($3::text[])
                        """,
                        conversation_id,
                        result.case_id,
                        ["phone", "whatsapp"],
                    )
                    if recipient is None:
                        raise PermanentProviderError(
                            "supplier conversation has no active WhatsApp contact"
                        )
                    command = WhatsAppSendCommand(
                        case_id=result.case_id,
                        recipient=str(recipient),
                        text=result.draft_outbound_text,
                        conversation_id=conversation_id,
                        logical_action_id=f"supplier-reply:{message_id}",
                    )
                    approval_id = await request_approval(
                        connection,
                        case_id=result.case_id,
                        action_type="send_whatsapp",
                        payload=command.approval_payload(),
                        requested_by=owner["owner_user_id"],
                        expires_at=datetime.now(UTC) + timedelta(hours=24),
                    )
                    text = (
                        "Получен ответ поставщика. Проверьте следующий текст перед "
                        "отправкой в WhatsApp:\n\n"
                        f"{result.draft_outbound_text}"
                    )
                    reply_markup = {
                        "inline_keyboard": [
                            [
                                {
                                    "text": "Отправить в WhatsApp",
                                    "callback_data": f"wa:a:{approval_id}",
                                },
                                {
                                    "text": "Отмена",
                                    "callback_data": f"wa:r:{approval_id}",
                                },
                            ]
                        ]
                    }
                else:
                    text = (
                        "Получен ответ поставщика. Следующий черновик не отправлен:\n\n"
                        f"{result.draft_outbound_text}\n\n"
                        "WAHA выключен, поэтому отправка недоступна."
                    )
            else:
                text = "Ответ поставщика получил и учёл при сравнении вариантов."
            await enqueue_outbox(
                connection,
                event_type="telegram.send_message",
                payload={
                    "chat_id": int(owner["telegram_id"]),
                    "text": text,
                    **({"reply_markup": reply_markup} if reply_markup else {}),
                },
                idempotency_key=stable_key("telegram.supplier-reply", message_id),
            )

    async def _record_terminal_failure(self, job: ClaimedJob, error_code: str) -> None:
        attachment_id = job.payload.get("attachment_id")
        message_id = job.payload.get("incoming_message_id") or job.payload.get("message_id")
        async with self.pool.acquire() as connection, connection.transaction():
            if attachment_id:
                await connection.execute(
                    """
                    UPDATE incoming_attachments
                    SET status='failed',error_code=$2,updated_at=now()
                    WHERE id=$1
                    """,
                    UUID(str(attachment_id)),
                    error_code[:500],
                )
            chat_id = None
            if message_id:
                chat_id = await connection.fetchval(
                    "SELECT external_chat_id FROM messages WHERE id=$1",
                    UUID(str(message_id)),
                )
            if chat_id is not None:
                await enqueue_outbox(
                    connection,
                    event_type="telegram.send_message",
                    payload={
                        "chat_id": int(chat_id),
                        "text": (
                            "Не удалось обработать материал автоматически. "
                            "Попробуйте другой файл или пришлите данные текстом."
                        ),
                    },
                    idempotency_key=stable_key("telegram.job-dead", job.id),
                )

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, (PermanentProviderError, ValueError, KeyError, zipfile.BadZipFile)):
            return False
        return isinstance(
            exc,
            (RetryableProviderError, StaleIntakeContext, OSError, asyncio.TimeoutError),
        ) or not isinstance(exc, RuntimeError)


def _location_text(raw_payload: dict[str, Any]) -> str:
    message = raw_payload.get("message") or raw_payload.get("edited_message") or {}
    location = message.get("location") or {}
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if latitude is None or longitude is None:
        return ""
    return f"Пользователь указал геопозицию: latitude={latitude}, longitude={longitude}."


def _validate_downloaded(path: Path, kind: str, mime_type: str) -> None:
    with path.open("rb") as source:
        header = source.read(16)
    suffix = path.suffix.casefold()
    if kind == "document" and suffix == ".pdf" and not header.startswith(b"%PDF-"):
        raise PermanentProviderError("downloaded file is not a PDF")
    if kind == "document" and suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                names = {member.filename for member in members}
        except zipfile.BadZipFile as exc:
            raise PermanentProviderError("downloaded file is not a DOCX") from exc
        expanded_size = sum(member.file_size for member in members)
        if len(members) > 10_000:
            raise PermanentProviderError("DOCX contains too many archive members")
        if expanded_size > 256 * 1024 * 1024:
            raise PermanentProviderError("DOCX expands beyond the safe limit")
        if expanded_size > max(path.stat().st_size, 1) * 100:
            raise PermanentProviderError("DOCX compression ratio exceeds the safe limit")
        if any(member.flag_bits & 0x1 for member in members):
            raise PermanentProviderError("encrypted DOCX is not supported")
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise PermanentProviderError("downloaded archive is not a DOCX")
    if kind == "photo" and not (
        header.startswith(b"\xff\xd8\xff") or header.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        raise PermanentProviderError("downloaded file is not a supported image")
    if kind in {"voice", "audio"} and not mime_type.startswith("audio/"):
        raise PermanentProviderError("downloaded file metadata is not audio")


class _LimitedWriter:
    def __init__(self, target: BinaryIO, limit: int) -> None:
        self.target = target
        self.limit = limit
        self.written = 0

    def write(self, value: bytes) -> int:
        if self.written + len(value) > self.limit:
            raise PermanentProviderError("attachment exceeded the download size limit")
        written = self.target.write(value)
        self.written += written
        return written

    def flush(self) -> None:
        self.target.flush()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.target.seek(offset, whence)


async def _probe_audio(path: Path, *, max_duration_seconds: int) -> None:
    """Validate the real audio container before native decoders receive it."""

    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,channels:format=duration",
        "-of",
        "json",
        str(path),
        cwd=path.parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise PermanentProviderError("audio probe timed out") from exc
    if process.returncode != 0 or len(stdout) > 256_000:
        detail = stderr.decode(errors="replace")[-300:]
        raise PermanentProviderError(f"unsupported audio container: {detail}")
    try:
        payload = json.loads(stdout)
        streams = payload.get("streams") or []
        duration = float((payload.get("format") or {}).get("duration") or 0)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PermanentProviderError("audio probe returned invalid metadata") from exc
    if not streams:
        raise PermanentProviderError("file contains no audio stream")
    channels = int(streams[0].get("channels") or 0)
    if channels < 1 or channels > 2:
        raise PermanentProviderError("audio must contain one or two channels")
    if duration <= 0 or duration > max_duration_seconds:
        raise PermanentProviderError("audio duration exceeds the configured limit")
