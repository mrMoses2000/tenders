from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
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
from procurement_bot.errors import PermanentProviderError, RetryableProviderError, ValidationBlocked
from procurement_bot.providers.waha import (
    WahaClient,
    WahaSendResult,
    normalize_account_jid,
    normalize_chat_id,
)
from procurement_bot.queue import (
    ClaimedOutbox,
    claim_outbox,
    complete_outbox,
    defer_outbox,
    fail_outbox,
    renew_outbox_lease,
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
from procurement_bot.whatsapp_account import (
    WhatsAppAccountReservation,
    load_account_identity_pin,
    mark_outcome,
    record_account_identity_preflight_event,
    renew_post_lease,
    reserve_outbound,
)
from procurement_bot.whatsapp_delivery import (
    WhatsAppDeliveryContext,
    assert_whatsapp_delivery_allowed,
    complete_whatsapp_delivery,
    parse_whatsapp_delivery_payload,
)
from procurement_bot.whatsapp_send_journal import (
    acceptance_unknown_due,
    exact_recovered_message,
    load_acceptance,
    load_reconciliation_identity,
    mark_acceptance_unknown,
    persist_acceptance,
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

    async def run_once(self, *, event_id: UUID | None = None) -> bool:
        claim = await claim_whatsapp_outbox(
            self.pool,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            event_id=event_id,
        )
        if claim is None:
            return False
        event, actor = claim
        lease_lost = asyncio.Event()
        account_reservation: dict[str, WhatsAppAccountReservation | None] = {"value": None}
        heartbeat = asyncio.create_task(
            self._heartbeat(event, actor, lease_lost, account_reservation)
        )
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
            provider_message_id = event.provider_request_id
            completion_received_at = None
            async with self.pool.acquire() as connection:
                acceptance = await load_acceptance(connection, event)
            if acceptance is not None:
                row, result = acceptance
                completion_received_at = row["accepted_at"]
                async with self.pool.acquire() as connection, connection.transaction():
                    await mark_outcome(
                        connection,
                        event.id,
                        state="accepted",
                        reason="acceptance_journal_confirmed",
                        accepted_at=completion_received_at,
                        projection_ok=False,
                        source="reconciliation",
                    )
                delivery = _accepted_send_context(
                    event,
                    session_name=row["session_name"],
                    external_chat_id=row["external_chat_id"],
                )
            elif provider_message_id is not None:
                payload_model = parse_whatsapp_delivery_payload(payload)
                chat_id = normalize_chat_id(payload_model.recipient)
                identity = None
                try:
                    async with self.pool.acquire() as connection:
                        identity = await load_reconciliation_identity(connection, event)
                    if (
                        identity is None
                        or identity["session_name"] != self.client.session_name
                        or identity["external_chat_id"] != chat_id
                    ):
                        raise ValueError("reserved WAHA send identity changed")
                    if (
                        identity is not None
                        and identity["observed_account_jid"] is not None
                        and identity["account_identity_observed_at"] is not None
                        and identity["account_pin_version"] is not None
                        and identity["state"] in {"reserved", "unknown", "accepted"}
                        and identity["pinned_account_jid"] == identity["observed_account_jid"]
                    ):
                        live_identity = await self.client.get_account_identity()
                        if live_identity.account_jid == identity["observed_account_jid"]:
                            expected_full_id = f"true_{chat_id}_{provider_message_id}"
                            received = await self.client.fetch_message(chat_id, expected_full_id)
                            result = exact_recovered_message(
                                received,
                                expected_chat_id=chat_id,
                                provider_request_id=provider_message_id,
                                expected_text=payload_model.text,
                            )
                        else:
                            result = None
                    else:
                        # Legacy reservations have no provable sender account. Do not
                        # query a message ID under whichever account is connected now.
                        result = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result = None
                    LOGGER.info(
                        "reserved WAHA message reconciliation unavailable",
                        extra={"job_id": event.id, "error": type(exc).__name__},
                    )
                if result is None:
                    async with self.pool.acquire() as connection, connection.transaction():
                        await mark_outcome(
                            connection,
                            event.id,
                            state="unknown",
                            reason="exact_message_not_found_or_unavailable",
                            source="reconciliation",
                        )
                        await mark_acceptance_unknown(
                            connection,
                            event,
                            conversation_id=payload_model.conversation_id,
                        )
                    raise WhatsAppSendDeferred(
                        available_at=acceptance_unknown_due(), reason="provider_acceptance_unknown"
                    )
                delivery = _accepted_send_context(
                    event,
                    session_name=identity["session_name"],
                    external_chat_id=identity["external_chat_id"],
                )
                async with self.pool.acquire() as connection, connection.transaction():
                    await persist_acceptance(
                        connection,
                        event,
                        session_name=identity["session_name"],
                        external_chat_id=identity["external_chat_id"],
                        provider_request_id=provider_message_id,
                        result=result,
                        source="reconciliation",
                        account_jid=identity["observed_account_jid"],
                        account_pin_version=identity["account_pin_version"],
                        account_identity_observed_at=identity["account_identity_observed_at"],
                    )
                    await mark_outcome(
                        connection,
                        event.id,
                        state="accepted",
                        reason="exact_provider_message_reconciled",
                        accepted_at=datetime.now(UTC),
                        projection_ok=False,
                        source="reconciliation",
                    )
                completion_received_at = datetime.now(UTC)
            else:
                delivery = await assert_whatsapp_delivery_allowed(self.pool, event)
                if delivery.session_name != self.client.session_name:
                    raise WhatsAppSendDeferred(
                        available_at=acceptance_unknown_due(), reason="waha_session_mismatch"
                    )
                pin = await load_account_identity_pin(self.pool, delivery.session_name)
                try:
                    identity_observation = await self.client.get_account_identity()
                    observed_jid = normalize_account_jid(identity_observation.account_jid)
                except (
                    PermanentProviderError,
                    RetryableProviderError,
                    OSError,
                    TimeoutError,
                ) as exc:
                    await record_account_identity_preflight_event(
                        self.pool,
                        session_name=delivery.session_name,
                        outbox_event_id=event.id,
                        event_type="account_identity_check_failed",
                        reason="preflight_self_identity_unavailable",
                        pin=pin,
                    )
                    raise WhatsAppSendDeferred(
                        available_at=acceptance_unknown_due(),
                        reason="whatsapp_account_identity_check_failed",
                    ) from exc
                if observed_jid != pin.account_jid:
                    await record_account_identity_preflight_event(
                        self.pool,
                        session_name=delivery.session_name,
                        outbox_event_id=event.id,
                        event_type="account_identity_mismatch",
                        reason="preflight_self_identity_mismatch",
                        pin=pin,
                        observed_identity=identity_observation,
                    )
                    raise WhatsAppSendDeferred(
                        available_at=acceptance_unknown_due(),
                        reason="whatsapp_account_identity_mismatch",
                    )
                try:
                    provider_message_id = await self.client.new_message_id()
                except RetryableProviderError as exc:
                    if "session is not ready" not in str(exc).lower():
                        raise
                    raise WhatsAppSendDeferred(
                        available_at=acceptance_unknown_due(), reason="waha_session_unavailable"
                    ) from exc
                reservation = await reserve_outbound(
                    self.pool,
                    event,
                    provider_request_id=provider_message_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                    identity_observation=identity_observation,
                    expected_account_jid=pin.account_jid,
                    expected_pin_version=pin.version,
                    expected_session_name=delivery.session_name,
                )
                if reservation is None:
                    raise WhatsAppSendDeferred(
                        available_at=acceptance_unknown_due(),
                        reason="whatsapp_account_outcome_unresolved",
                    )
                account_reservation["value"] = reservation
                try:
                    result = await _send_while_actor_owned(
                        self.client,
                        lease_lost,
                        recipient=str(payload["recipient"]),
                        text=str(payload["text"]),
                        idempotency_key=event.idempotency_key,
                        provider_message_id=provider_message_id,
                    )
                except Exception as exc:
                    async with self.pool.acquire() as connection, connection.transaction():
                        await mark_outcome(
                            connection,
                            event.id,
                            state="unknown",
                            reason=f"send_exception:{type(exc).__name__}",
                        )
                        await mark_acceptance_unknown(
                            connection,
                            event,
                            conversation_id=UUID(str(payload["conversation_id"])),
                        )
                    raise
                async with self.pool.acquire() as connection, connection.transaction():
                    await persist_acceptance(
                        connection,
                        event,
                        session_name=delivery.session_name,
                        external_chat_id=delivery.external_chat_id,
                        provider_request_id=provider_message_id,
                        result=result,
                        source="http",
                        account_jid=identity_observation.account_jid,
                        account_pin_version=reservation.account_pin_version,
                        account_identity_observed_at=identity_observation.observed_at,
                    )
                    await mark_outcome(
                        connection,
                        event.id,
                        state="accepted",
                        reason="provider_http_accepted",
                        accepted_at=datetime.now(UTC),
                        projection_ok=False,
                        source="http",
                    )
                completion_received_at = datetime.now(UTC)
            try:
                completed = await complete_whatsapp_delivery(
                    self.pool,
                    event,
                    delivery,
                    result,
                    received_at=completion_received_at,
                )
            except Exception:
                async with self.pool.acquire() as connection, connection.transaction():
                    await mark_outcome(
                        connection,
                        event.id,
                        state="accepted",
                        reason="accepted_projection_failed",
                        accepted_at=completion_received_at,
                        projection_ok=False,
                        source="http" if event.provider_request_id is None else "reconciliation",
                    )
                raise
            if not completed:
                async with self.pool.acquire() as connection, connection.transaction():
                    await mark_outcome(
                        connection,
                        event.id,
                        state="accepted",
                        reason="accepted_projection_failed",
                        accepted_at=completion_received_at,
                        projection_ok=False,
                        source="http" if event.provider_request_id is None else "reconciliation",
                    )
                raise RuntimeError("outbox lease expired after WAHA send")
        except asyncio.CancelledError:
            raise
        except WhatsAppSendDeferred as exc:
            LOGGER.info(
                "WhatsApp outbox reconciliation deferred"
                if event.provider_request_id
                else "WhatsApp send deferred",
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
        account_reservation: dict[str, WhatsAppAccountReservation | None],
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
                reservation = account_reservation["value"]
                account_renewed = (
                    await renew_post_lease(
                        self.pool,
                        reservation,
                        worker_id=self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
                    if reservation is not None
                    else True
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
            if not outbox_renewed or not actor_renewed or not account_renewed:
                lease_lost.set()
                LOGGER.error("WhatsApp outbox lease renewal failed", extra={"job_id": event.id})
                return


def _accepted_send_context(
    event: ClaimedOutbox,
    *,
    session_name: str,
    external_chat_id: str,
) -> WhatsAppDeliveryContext:
    payload = parse_whatsapp_delivery_payload(event.payload)
    return WhatsAppDeliveryContext(
        payload=payload,
        session_name=session_name,
        external_chat_id=external_chat_id,
    )


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
