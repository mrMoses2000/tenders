"""Fail-closed Telegram callbacks for approval decisions.

The callback contains only an approval UUID.  Case ownership, action type and
all research input are resolved from PostgreSQL while the approval row is
locked.  This keeps Telegram callback data outside the trust boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import asyncpg
from aiogram.types import Update

from procurement_bot.approvals import approve_request
from procurement_bot.queue import accept_update, enqueue_job, enqueue_outbox, stable_key
from procurement_bot.telegram_access import TelegramAccessPolicy

CallbackDecision = Literal["approve", "reject"]
CallbackNamespace = Literal["research", "whatsapp"]

_CALLBACK_PATTERN = re.compile(
    r"(?P<namespace>rs|wa):(?P<decision>[ar]):(?P<approval_id>"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)

_APPROVED_TEXT = "Начал поиск."
_REJECTED_TEXT = "Хорошо, поиск не запускаю."
_INVALID_TEXT = "Эта кнопка уже неактивна."
_DENIED_TEXT = "Эта кнопка уже неактивна."
_WHATSAPP_APPROVED_TEXT = "Сообщение отправляю поставщику."
_WHATSAPP_REJECTED_TEXT = "Хорошо, сообщение не отправляю."


@dataclass(frozen=True, slots=True)
class ParsedResearchCallback:
    decision: CallbackDecision
    approval_id: UUID


@dataclass(frozen=True, slots=True)
class ParsedApprovalCallback:
    namespace: CallbackNamespace
    decision: CallbackDecision
    approval_id: UUID


def parse_approval_callback(data: object) -> ParsedApprovalCallback | None:
    """Parse only canonical ``rs|wa:a|r:<lowercase UUID>`` commands."""

    if not isinstance(data, str):
        return None
    match = _CALLBACK_PATTERN.fullmatch(data)
    if match is None:
        return None
    approval_text = match.group("approval_id")
    try:
        approval_id = UUID(approval_text)
    except ValueError:
        return None
    if str(approval_id) != approval_text:
        return None
    decision: CallbackDecision = (
        "approve" if match.group("decision") == "a" else "reject"
    )
    namespace: CallbackNamespace = (
        "research" if match.group("namespace") == "rs" else "whatsapp"
    )
    return ParsedApprovalCallback(
        namespace=namespace,
        decision=decision,
        approval_id=approval_id,
    )


def parse_research_callback(data: object) -> ParsedResearchCallback | None:
    """Parse only the canonical ``rs:a|r:<UUID>`` callback contract."""

    parsed = parse_approval_callback(data)
    if parsed is None or parsed.namespace != "research":
        return None
    return ParsedResearchCallback(
        decision=parsed.decision,
        approval_id=parsed.approval_id,
    )


def parse_whatsapp_callback(data: object) -> ParsedApprovalCallback | None:
    """Parse only the canonical ``wa:a|r:<UUID>`` callback contract."""

    parsed = parse_approval_callback(data)
    if parsed is None or parsed.namespace != "whatsapp":
        return None
    return parsed


def _model_dump(value: Any) -> dict[str, Any]:
    dumper = getattr(value, "model_dump", None)
    if dumper is not None:
        return dumper(mode="json", exclude_none=True)
    return dict(getattr(value, "__dict__", {}))


class ApprovalCallbackIngress:
    """Atomically persist and execute one supported approval callback.

    ``allowed_user_ids`` is mandatory as a security boundary.  An omitted or
    empty set deliberately denies every state-changing callback.
    """

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        allowed_user_ids: set[int] | frozenset[int] | None,
        bootstrap_usernames: set[str] | frozenset[str] | None = None,
    ) -> None:
        values = frozenset(allowed_user_ids or ())
        if any(isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("allowed_user_ids must contain positive Telegram IDs")
        self.pool = pool
        self.allowed_user_ids = values
        self._access = TelegramAccessPolicy(
            allowed_user_ids=values,
            bootstrap_usernames=bootstrap_usernames,
        )

    async def accept(self, update: Update) -> bool:
        """Handle one callback, returning ``False`` for a duplicate update.

        Non-callback updates are not claimed so ``TelegramIngress`` can route
        them through its regular message path.
        """

        callback = update.callback_query
        if callback is None:
            return False
        callback_id = str(callback.id)
        raw_payload = _model_dump(update)
        async with self.pool.acquire() as connection, connection.transaction():
            if not await accept_update(
                connection,
                update.update_id,
                raw_payload=raw_payload,
            ):
                return False

            parsed = parse_approval_callback(callback.data)
            sender = callback.from_user
            sender_id = getattr(sender, "id", None)
            sender_username = getattr(sender, "username", None)
            if parsed is None:
                await self._answer(
                    connection,
                    update_id=update.update_id,
                    callback_id=callback_id,
                    text=_INVALID_TEXT,
                    show_alert=True,
                )
            elif not await self._access.authorize(
                connection,
                telegram_id=sender_id,
                username=sender_username,
            ):
                await self._answer(
                    connection,
                    update_id=update.update_id,
                    callback_id=callback_id,
                    text=_DENIED_TEXT,
                    show_alert=True,
                )
            else:
                await self._apply_decision(
                    connection,
                    update_id=update.update_id,
                    callback_id=callback_id,
                    telegram_user_id=sender_id,
                    parsed=parsed,
                )

            await self._complete_update(connection, update.update_id)
        return True

    @staticmethod
    async def _apply_decision(
        connection: asyncpg.Connection,
        *,
        update_id: int,
        callback_id: str,
        telegram_user_id: int,
        parsed: ParsedApprovalCallback,
    ) -> None:
        # The join is the authorization check.  Callback data supplies no case,
        # user, research-run or job input beyond the opaque approval UUID.
        if parsed.namespace == "research":
            row = await connection.fetchrow(
                """
                SELECT approval.id,approval.case_id,approval.status,
                       (approval.expires_at IS NULL OR approval.expires_at>now()) AS unexpired,
                       owner.id AS actor_user_id
                FROM action_approvals AS approval
                JOIN procurement_cases AS procurement_case
                  ON procurement_case.id=approval.case_id
                JOIN users AS owner
                  ON owner.id=procurement_case.owner_user_id
                WHERE approval.id=$1
                  AND approval.action_type='start_research'
                  AND approval.status='requested'
                  AND (approval.expires_at IS NULL OR approval.expires_at>now())
                  AND owner.telegram_id=$2
                  AND owner.active=TRUE
                FOR UPDATE OF approval
                """,
                parsed.approval_id,
                telegram_user_id,
            )
        else:
            row = await connection.fetchrow(
                """
                SELECT approval.id,approval.case_id,approval.status,
                       (approval.expires_at IS NULL OR approval.expires_at>now()) AS unexpired,
                       owner.id AS actor_user_id
                FROM action_approvals AS approval
                JOIN procurement_cases AS procurement_case
                  ON procurement_case.id=approval.case_id
                JOIN users AS owner
                  ON owner.id=procurement_case.owner_user_id
                WHERE approval.id=$1
                  AND approval.action_type='send_whatsapp'
                  AND approval.status='requested'
                  AND (approval.expires_at IS NULL OR approval.expires_at>now())
                  AND owner.telegram_id=$2
                  AND owner.active=TRUE
                FOR UPDATE OF approval
                """,
                parsed.approval_id,
                telegram_user_id,
            )
        if row is None or row["status"] != "requested" or row["unexpired"] is not True:
            await ApprovalCallbackIngress._answer(
                connection,
                update_id=update_id,
                callback_id=callback_id,
                text=_DENIED_TEXT,
                show_alert=True,
            )
            return

        actor_user_id: UUID = row["actor_user_id"]
        case_id: UUID = row["case_id"]
        if parsed.decision == "approve":
            changed = await approve_request(
                connection,
                approval_id=parsed.approval_id,
                approved_by=actor_user_id,
            )
            if not changed:
                raise RuntimeError("locked research approval could not be approved")
            if parsed.namespace == "research":
                await enqueue_job(
                    connection,
                    kind="start_research",
                    payload={"approval_id": str(parsed.approval_id)},
                    idempotency_key=stable_key(
                        "start-research-approval",
                        parsed.approval_id,
                    ),
                )
                audit_action = "research_approval.approved"
                answer_text = _APPROVED_TEXT
            else:
                await enqueue_job(
                    connection,
                    kind="enqueue_whatsapp_send",
                    payload={"approval_id": str(parsed.approval_id)},
                    idempotency_key=stable_key(
                        "enqueue-whatsapp-send-approval",
                        parsed.approval_id,
                    ),
                )
                audit_action = "whatsapp_approval.approved"
                answer_text = _WHATSAPP_APPROVED_TEXT
        else:
            result = await connection.execute(
                """
                UPDATE action_approvals
                SET status='rejected'
                WHERE id=$1 AND status='requested'
                  AND (expires_at IS NULL OR expires_at>now())
                """,
                parsed.approval_id,
            )
            if result != "UPDATE 1":
                raise RuntimeError("locked research approval could not be rejected")
            if parsed.namespace == "research":
                audit_action = "research_approval.rejected"
                answer_text = _REJECTED_TEXT
            else:
                audit_action = "whatsapp_approval.rejected"
                answer_text = _WHATSAPP_REJECTED_TEXT

        await connection.execute(
            """
            INSERT INTO audit_log(
                actor_user_id,actor_kind,action,entity_type,entity_id,
                correlation_id,metadata
            )
            VALUES ($1,'user',$2,'action_approval',$3,$4,$5::jsonb)
            """,
            actor_user_id,
            audit_action,
            str(parsed.approval_id),
            f"telegram-update:{update_id}",
            {"case_id": str(case_id), "telegram_update_id": update_id},
        )
        await ApprovalCallbackIngress._answer(
            connection,
            update_id=update_id,
            callback_id=callback_id,
            text=answer_text,
            show_alert=False,
        )

    @staticmethod
    async def _answer(
        connection: asyncpg.Connection,
        *,
        update_id: int,
        callback_id: str,
        text: str,
        show_alert: bool,
    ) -> None:
        await enqueue_outbox(
            connection,
            event_type="telegram.answer_callback_query",
            payload={
                "callback_query_id": callback_id,
                "text": text,
                "show_alert": show_alert,
            },
            idempotency_key=stable_key("telegram-callback-answer", update_id),
            max_attempts=3,
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


class ResearchApprovalCallbackIngress(ApprovalCallbackIngress):
    """Backward-compatible name for the unified approval callback router."""
