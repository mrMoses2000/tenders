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
from procurement_bot.chat_actors import (
    ChatActorLease,
    claim_whatsapp_outbox,
    release_chat_actor,
    renew_chat_actor,
)
from procurement_bot.errors import PermanentProviderError, ValidationBlocked
from procurement_bot.providers.waha import WahaClient, WahaSendResult
from procurement_bot.queue import (
    ClaimedOutbox,
    claim_outbox,
    complete_outbox,
    defer_outbox,
    fail_outbox,
    renew_outbox_lease,
    set_outbox_provider_request_id,
)
from procurement_bot.report_artifacts import resolve_report_path
from procurement_bot.supplier_dialogue import (
    WhatsAppSendDeferred,
    assert_supplier_safe_text,
)
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


class WhatsAppSendOutcomeUnknown(PermanentProviderError):
    """A send lost its actor fence after dispatch and requires manual reconciliation."""


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
        claim = await claim_whatsapp_outbox(
            self.pool,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            return False
        event, actor = claim
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(event, actor, lease_lost))
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
            result = await _send_while_actor_owned(
                self.client,
                lease_lost,
                recipient=str(payload["recipient"]),
                text=str(payload["text"]),
                idempotency_key=event.idempotency_key,
                provider_message_id=provider_message_id,
            )
            if lease_lost.is_set():
                raise RuntimeError("WhatsApp chat actor lease was lost after send")
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
        except WhatsAppSendDeferred as exc:
            LOGGER.info(
                "WhatsApp send deferred by case policy",
                extra={
                    "job_id": event.id,
                    "available_at": exc.available_at.isoformat(),
                    "reason": exc.reason,
                },
            )
            with suppress(RuntimeError):
                await defer_outbox(
                    self.pool,
                    event,
                    available_at=exc.available_at,
                    reason=exc.reason,
                )
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
            try:
                if not await release_chat_actor(self.pool, actor):
                    LOGGER.warning(
                        "WhatsApp chat actor lease was not released by its owner",
                        extra={"actor_id": str(actor.actor_id), "generation": actor.generation},
                    )
            except Exception:
                LOGGER.exception(
                    "WhatsApp chat actor release failed",
                    extra={"actor_id": str(actor.actor_id), "generation": actor.generation},
                )
        return True

    async def _heartbeat(
        self,
        event: ClaimedOutbox,
        actor: ChatActorLease,
        lease_lost: asyncio.Event,
    ) -> None:
        interval = max(5, self.lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            try:
                outbox_renewed = await renew_outbox_lease(
                    self.pool,
                    event.id,
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
                actor_renewed = await renew_chat_actor(
                    self.pool,
                    actor,
                    lease_seconds=self.lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                lease_lost.set()
                LOGGER.exception(
                    "WhatsApp outbox or actor lease renewal raised",
                    extra={"job_id": event.id, "actor_id": str(actor.actor_id)},
                )
                return
            if not outbox_renewed or not actor_renewed:
                lease_lost.set()
                LOGGER.error("WhatsApp outbox lease renewal failed", extra={"job_id": event.id})
                return


async def _send_while_actor_owned(
    client: WahaClient,
    lease_lost: asyncio.Event,
    **kwargs: str,
) -> WahaSendResult:
    """Cancel and reap a provider request when the durable lease is lost."""

    if lease_lost.is_set():
        raise WhatsAppSendOutcomeUnknown(
            "WhatsApp chat actor lease was lost before send; reconcile the reserved id"
        )
    provider_task = asyncio.create_task(client.send_text(**kwargs))
    lease_waiter = asyncio.create_task(lease_lost.wait())
    try:
        done, _pending = await asyncio.wait(
            {provider_task, lease_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lease_lost.is_set():
            provider_task.cancel()
            await asyncio.gather(provider_task, return_exceptions=True)
            raise WhatsAppSendOutcomeUnknown(
                "WhatsApp chat actor lease was lost during send; provider outcome is unknown"
            )
        return provider_task.result()
    finally:
        lease_waiter.cancel()
        with suppress(asyncio.CancelledError):
            await lease_waiter
        if not provider_task.done():
            provider_task.cancel()
            await asyncio.gather(provider_task, return_exceptions=True)
