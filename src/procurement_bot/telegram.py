"""Thin, crash-safe Telegram ingress built on aiogram 3 models.

No files are downloaded here.  The database message, attachment metadata,
download job, and acknowledgement outbox event are committed atomically.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from aiogram import Bot
from aiogram.types import Message, Update

from procurement_bot.attachments import (
    AttachmentMetadata,
    AttachmentValidationError,
    attachment_from_message,
)
from procurement_bot.purchase_workflow import bind_next_attachment_as_purchase_evidence
from procurement_bot.queue import (
    accept_update,
    enqueue_job,
    enqueue_outbox,
    stable_key,
)
from procurement_bot.telegram_access import TelegramAccessPolicy
from procurement_bot.telegram_callbacks import ResearchApprovalCallbackIngress

PRIVATE_ONLY_TEXT = "Для безопасности бот принимает заявки только в личном чате."
UNSUPPORTED_TEXT = "Отправьте текст, геопозицию, PDF, DOCX, голосовое, аудио или фото."
WELCOME_TEXT = (
    "Пришлите заявку файлом, голосом или текстом. Я разберу её, уточню "
    "недостающее и помогу найти поставщиков."
)


def _model_dump(value: Any) -> dict[str, Any]:
    dumper = getattr(value, "model_dump", None)
    if dumper is not None:
        return dumper(mode="json", exclude_none=True)
    return dict(getattr(value, "__dict__", {}))


def _attachment_received_text(kind: str) -> str:
    if kind == "document":
        return "Файл получил. Сейчас разберу заявку."
    if kind in {"voice", "audio"}:
        return "Голосовое получил."
    if kind == "photo":
        return "Фото получил."
    return "Получил."


class TelegramIngress:
    """Persist aiogram updates while bounding global and per-chat concurrency."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        bot: Bot | None = None,
        max_concurrency: int = 16,
        allowed_user_ids: set[int] | None = None,
        bootstrap_usernames: set[str] | frozenset[str] | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.pool = pool
        self.bot = bot
        self.allowed_user_ids = allowed_user_ids
        self._access = TelegramAccessPolicy(
            allowed_user_ids=allowed_user_ids,
            bootstrap_usernames=bootstrap_usernames,
            allow_unconfigured=(allowed_user_ids is None and bootstrap_usernames is None),
        )
        self._callback_ingress = ResearchApprovalCallbackIngress(
            pool=pool,
            allowed_user_ids=allowed_user_ids,
            bootstrap_usernames=bootstrap_usernames,
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def accept(self, update: Update) -> bool:
        """Accept one update; return ``False`` when it was already processed."""

        if getattr(update, "callback_query", None) is not None:
            async with self._semaphore:
                return await self._callback_ingress.accept(update)

        # Edits need immutable revision semantics and are deliberately ignored in
        # milestone 1. The user can send a follow-up clarification instead.
        message = update.message
        if message is None:
            return await self._accept_without_message(update)
        # Acquire the chat lock first so queued updates from one noisy chat do
        # not consume every global slot while waiting for their predecessor.
        async with self._chat_locks[message.chat.id]:
            async with self._semaphore:
                return await self._accept_message(update, message)

    async def _accept_without_message(self, update: Update) -> bool:
        async with self._semaphore:
            async with self.pool.acquire() as connection, connection.transaction():
                if not await accept_update(
                    connection, update.update_id, raw_payload=_model_dump(update)
                ):
                    return False
                await self._complete_update(connection, update.update_id)
        return True

    async def _accept_message(self, update: Update, message: Message) -> bool:
        raw_payload = _model_dump(update)
        async with self.pool.acquire() as connection, connection.transaction():
            if not await accept_update(
                connection, update.update_id, raw_payload=raw_payload
            ):
                return False

            sender = message.from_user
            if sender is None or not await self._access.authorize(
                connection,
                telegram_id=sender.id,
                username=sender.username,
                permit_bootstrap=(message.chat.type == "private"),
            ):
                await self._complete_update(connection, update.update_id)
                return True

            if message.chat.type != "private":
                await self._reply(
                    connection,
                    update_id=update.update_id,
                    chat_id=message.chat.id,
                    text=PRIVATE_ONLY_TEXT,
                    purpose="private-only",
                )
                await self._complete_update(connection, update.update_id)
                return True

            try:
                attachment = attachment_from_message(message)
            except AttachmentValidationError as exc:
                await self._reply(
                    connection,
                    update_id=update.update_id,
                    chat_id=message.chat.id,
                    text=str(exc),
                    purpose="invalid-attachment",
                )
                await self._complete_update(connection, update.update_id)
                return True

            input_kind = self._input_kind(message, attachment)
            if input_kind is None:
                await self._reply(
                    connection,
                    update_id=update.update_id,
                    chat_id=message.chat.id,
                    text=UNSUPPORTED_TEXT,
                    purpose="unsupported",
                )
                await self._complete_update(connection, update.update_id)
                return True

            message_id = await self._store_message(
                connection,
                message=message,
                input_kind=input_kind,
                raw_payload=raw_payload,
            )
            command_parts = (message.text or "").strip().split(maxsplit=1)
            command = command_parts[0].casefold().split("@", 1)[0] if command_parts else ""
            if command in {"/start", "/help"}:
                await self._reply(
                    connection,
                    update_id=update.update_id,
                    chat_id=message.chat.id,
                    text=WELCOME_TEXT,
                    purpose="welcome",
                )
                await self._complete_update(connection, update.update_id)
                return True
            if attachment is not None:
                await self._store_attachment_and_job(
                    connection,
                    update_id=update.update_id,
                    chat_id=message.chat.id,
                    message_id=message_id,
                    attachment=attachment,
                )
                await self._reply(
                    connection,
                    update_id=update.update_id,
                    chat_id=message.chat.id,
                    text=_attachment_received_text(attachment.kind),
                    purpose="attachment-accepted",
                )
            else:
                await enqueue_job(
                    connection,
                    kind="process_intake",
                    payload={
                        "incoming_message_id": str(message_id),
                        "telegram_chat_id": message.chat.id,
                        "input_kind": input_kind,
                    },
                    idempotency_key=stable_key("process-intake", message_id),
                )
            await self._complete_update(connection, update.update_id)
        return True

    @staticmethod
    def _input_kind(message: Message, attachment: AttachmentMetadata | None) -> str | None:
        if attachment is not None:
            return attachment.kind
        if message.location is not None:
            return "location"
        if message.text and message.text.strip():
            return "text"
        return None

    @staticmethod
    async def _store_message(
        connection: asyncpg.Connection,
        *,
        message: Message,
        input_kind: str,
        raw_payload: dict[str, Any],
    ) -> UUID:
        message_id = uuid4()
        text = message.text or message.caption
        if input_kind == "location":
            text = "[location]"
        received_at: datetime | None = message.date
        user_id: UUID | None = None
        sender = message.from_user
        if sender is not None:
            display_name = " ".join(
                value
                for value in (sender.first_name, sender.last_name)
                if value
            ).strip()
            user_id = await connection.fetchval(
                """
                INSERT INTO users(telegram_id,username,display_name,language_code)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username=CASE
                        WHEN EXCLUDED.username<>'' THEN EXCLUDED.username ELSE users.username
                    END,
                    display_name=CASE
                        WHEN EXCLUDED.display_name<>'' THEN EXCLUDED.display_name
                        ELSE users.display_name
                    END,
                    language_code=EXCLUDED.language_code,
                    updated_at=now()
                RETURNING id
                """,
                sender.id,
                sender.username or "",
                display_name,
                sender.language_code or "ru",
            )
        row_id = await connection.fetchval(
            """
            INSERT INTO messages(
                id,user_id,channel,direction,external_chat_id,external_message_id,
                message_kind,text_content,raw_payload,received_at
            ) VALUES ($1,$2,'telegram','inbound',$3,$4,$5,$6,$7::jsonb,$8)
            ON CONFLICT (channel,external_chat_id,external_message_id) DO UPDATE SET
                message_kind=EXCLUDED.message_kind,
                text_content=EXCLUDED.text_content,
                raw_payload=EXCLUDED.raw_payload,
                received_at=EXCLUDED.received_at
            RETURNING id
            """,
            message_id,
            user_id,
            str(message.chat.id),
            str(message.message_id),
            input_kind,
            text or "",
            raw_payload,
            received_at,
        )
        return row_id

    @staticmethod
    async def _store_attachment_and_job(
        connection: asyncpg.Connection,
        *,
        update_id: int,
        chat_id: int,
        message_id: UUID,
        attachment: AttachmentMetadata,
    ) -> UUID:
        attachment_id = uuid4()
        stored_id = await connection.fetchval(
            """
            INSERT INTO incoming_attachments(
                id,message_id,external_file_id,external_file_unique_id,kind,
                original_filename,mime_type,declared_size,
                declared_duration_seconds,status
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'pending')
            ON CONFLICT (message_id,external_file_id) DO UPDATE SET
                external_file_unique_id=EXCLUDED.external_file_unique_id,
                original_filename=EXCLUDED.original_filename,
                mime_type=EXCLUDED.mime_type,
                declared_size=EXCLUDED.declared_size,
                declared_duration_seconds=EXCLUDED.declared_duration_seconds,
                updated_at=now()
            RETURNING id
            """,
            attachment_id,
            message_id,
            attachment.file_id,
            attachment.file_unique_id,
            attachment.kind,
            attachment.filename,
            attachment.mime_type,
            attachment.declared_size,
            attachment.declared_duration_seconds,
        )
        owner_user_id = await connection.fetchval(
            "SELECT user_id FROM messages WHERE id=$1",
            message_id,
        )
        if owner_user_id is not None and attachment.kind in {"document", "photo"}:
            await bind_next_attachment_as_purchase_evidence(
                connection,
                owner_user_id=owner_user_id,
                attachment_id=stored_id,
            )
        await enqueue_job(
            connection,
            kind="download_attachment",
            payload={
                "attachment_id": str(stored_id),
                "incoming_message_id": str(message_id),
                "telegram_update_id": update_id,
                "telegram_chat_id": chat_id,
                "file_id": attachment.file_id,
                "file_unique_id": attachment.file_unique_id,
                "kind": attachment.kind,
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "suffix": attachment.suffix,
                "declared_size": attachment.declared_size,
                "declared_duration_seconds": attachment.declared_duration_seconds,
            },
            idempotency_key=stable_key("download-attachment", stored_id),
        )
        return stored_id

    @staticmethod
    async def _reply(
        connection: asyncpg.Connection,
        *,
        update_id: int,
        chat_id: int,
        text: str,
        purpose: str,
    ) -> None:
        await enqueue_outbox(
            connection,
            event_type="telegram.send_message",
            payload={"chat_id": chat_id, "text": text},
            idempotency_key=stable_key("telegram-reply", update_id, purpose),
        )

    @staticmethod
    async def _complete_update(connection: asyncpg.Connection, update_id: int) -> None:
        await connection.execute(
            """
            UPDATE processed_updates
            SET status='completed',completed_at=now(),error_code=''
            WHERE update_id=$1
            """,
            update_id,
        )
