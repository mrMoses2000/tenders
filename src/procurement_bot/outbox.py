from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from uuid import UUID

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile

from procurement_bot.approvals import validate_consumed_approval
from procurement_bot.errors import PermanentProviderError, ValidationBlocked
from procurement_bot.providers.waha import WahaClient
from procurement_bot.queue import (
    ClaimedOutbox,
    claim_outbox,
    complete_outbox,
    fail_outbox,
    renew_outbox_lease,
    set_outbox_provider_request_id,
)
from procurement_bot.report_artifacts import resolve_report_path
from procurement_bot.supplier_dialogue import assert_supplier_safe_text
from procurement_bot.telegram_outbox_payloads import (
    TELEGRAM_ANSWER_CALLBACK_QUERY,
    TELEGRAM_SEND_DOCUMENT,
    TELEGRAM_SEND_MESSAGE,
    TelegramAnswerCallbackQueryPayload,
    TelegramSendDocumentPayload,
    TelegramSendMessagePayload,
    parse_telegram_outbox_payload,
    to_aiogram_kwargs,
)
from procurement_bot.whatsapp_delivery import (
    assert_whatsapp_delivery_allowed,
    complete_whatsapp_delivery,
)

LOGGER = logging.getLogger(__name__)


class TelegramOutboxWorker:
    """Deliver approved Telegram events; no WhatsApp event is accepted here."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        bot: Bot,
        worker_id: str,
        lease_seconds: int = 60,
        results_root: Path = Path("./var/results"),
    ) -> None:
        self.pool = pool
        self.bot = bot
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.results_root = results_root.expanduser().resolve()

    async def run_forever(self, *, idle_seconds: float = 0.5) -> None:
        while True:
            worked = await self.run_once()
            if not worked:
                await asyncio.sleep(idle_seconds)

    async def run_once(self) -> bool:
        event = await claim_outbox(
            self.pool,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            event_types=(
                TELEGRAM_SEND_MESSAGE,
                TELEGRAM_ANSWER_CALLBACK_QUERY,
                TELEGRAM_SEND_DOCUMENT,
            ),
        )
        if event is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(event))
        try:
            payload = parse_telegram_outbox_payload(event.event_type, event.payload)
            if isinstance(payload, TelegramSendMessagePayload):
                sent = await self.bot.send_message(**to_aiogram_kwargs(payload))
                external_id = str(sent.message_id)
            elif isinstance(payload, TelegramAnswerCallbackQueryPayload):
                await self.bot.answer_callback_query(**to_aiogram_kwargs(payload))
                external_id = payload.callback_query_id
            elif isinstance(payload, TelegramSendDocumentPayload):
                path = resolve_report_path(self.results_root, payload.storage_path)
                sent = await self.bot.send_document(
                    chat_id=payload.chat_id,
                    document=FSInputFile(path, filename=payload.filename),
                    caption=payload.caption,
                )
                external_id = str(sent.message_id)
            else:  # pragma: no cover - guarded by the strict parser union
                raise ValueError(f"unsupported outbox event: {event.event_type}")
            if not await complete_outbox(
                self.pool,
                event.id,
                self.worker_id,
                external_id=external_id,
            ):
                raise RuntimeError("outbox lease expired after Telegram send")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retryable = not isinstance(
                exc,
                (ValueError, KeyError, TelegramBadRequest, TelegramForbiddenError),
            )
            retry_after = exc.retry_after if isinstance(exc, TelegramRetryAfter) else None
            LOGGER.exception(
                "outbox delivery failed",
                extra={"job_id": event.id, "error": type(exc).__name__},
            )
            if retry_after:
                await asyncio.sleep(min(float(retry_after), 60.0))
            with suppress(RuntimeError):
                await fail_outbox(
                    self.pool,
                    event,
                    type(exc).__name__,
                    retryable=retryable,
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _heartbeat(self, event: ClaimedOutbox) -> None:
        interval = max(5, self.lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            if not await renew_outbox_lease(
                self.pool,
                event.id,
                self.worker_id,
                lease_seconds=self.lease_seconds,
            ):
                LOGGER.error("outbox lease could not be renewed", extra={"job_id": event.id})
                return


class WhatsAppOutboxWorker:
    """Deliver only pre-approved WhatsApp commands already present in the outbox."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        client: WahaClient,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> None:
        self.pool = pool
        self.client = client
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    async def run_forever(self, *, idle_seconds: float = 0.5) -> None:
        while True:
            worked = await self.run_once()
            if not worked:
                await asyncio.sleep(idle_seconds)

    async def run_once(self) -> bool:
        event = await claim_outbox(
            self.pool,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            event_types=("whatsapp.send_text",),
        )
        if event is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(event))
        try:
            if event.event_type != "whatsapp.send_text":
                raise ValueError(f"unsupported outbox event: {event.event_type}")
            payload = event.payload
            case_id = UUID(str(payload["case_id"]))
            await validate_consumed_approval(
                self.pool,
                approval_id=event.approval_id,
                case_id=case_id,
                action_type="send_whatsapp",
                payload=payload,
            )
            assert_supplier_safe_text(str(payload["text"]))
            delivery = await assert_whatsapp_delivery_allowed(self.pool, event)
            provider_message_id = event.provider_request_id
            if provider_message_id is None:
                provider_message_id = await self.client.new_message_id()
                stored = await set_outbox_provider_request_id(
                    self.pool,
                    event.id,
                    self.worker_id,
                    provider_message_id,
                )
                if not stored:
                    raise RuntimeError("outbox lease expired before WAHA id reservation")
            result = await self.client.send_text(
                recipient=str(payload["recipient"]),
                text=str(payload["text"]),
                idempotency_key=event.idempotency_key,
                provider_message_id=provider_message_id,
            )
            completed = await complete_whatsapp_delivery(
                self.pool,
                event,
                delivery,
                result,
            )
            if not completed:
                raise RuntimeError("outbox lease expired after WAHA send")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            retryable = not isinstance(
                exc,
                (ValueError, KeyError, PermanentProviderError, ValidationBlocked),
            )
            LOGGER.exception(
                "WhatsApp outbox delivery failed",
                extra={"job_id": event.id, "error": type(exc).__name__},
            )
            with suppress(RuntimeError):
                await fail_outbox(
                    self.pool,
                    event,
                    type(exc).__name__,
                    retryable=retryable,
                )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _heartbeat(self, event: ClaimedOutbox) -> None:
        interval = max(5, self.lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            if not await renew_outbox_lease(
                self.pool,
                event.id,
                self.worker_id,
                lease_seconds=self.lease_seconds,
            ):
                LOGGER.error("WhatsApp outbox lease renewal failed", extra={"job_id": event.id})
                return
