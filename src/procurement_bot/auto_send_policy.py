from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg

from procurement_bot.approvals import canonical_payload_json, payload_sha256
from procurement_bot.errors import ValidationBlocked
from procurement_bot.queue import enqueue_job, stable_key
from procurement_bot.supplier_dialogue import SendPolicy

_AUTO_SEND_JOB = "enqueue_whatsapp_send"
_AUTO_SEND_JOB_PAYLOAD_KEY = "approval_id"
URAN_CASE_ID = UUID("889f7d8e-5b6d-4906-ac34-5074f9cbb924")
URAN_OWNER_USER_ID = UUID("f0439e78-97db-45c4-8c63-3c22478f3b4e")
URAN_CONSENT_REFERENCE = (
    "codex-thread:01a0d040-dfcf-7680-8086-c13f094bb120:user-autonomous-dialogue-2026-10-01"
)
URAN_CONSENT_TEXT = (
    "Не надо нашему pipeline ждать, пока одобрю отправку сообщенией, "
    "он должен сам писать поставщикам и сам вести диалоги"
)


class AutoSendPolicyDenied(RuntimeError):
    """Persisted owner consent does not authorize this exact WhatsApp action."""


class AutoSendPolicyConflict(AutoSendPolicyDenied):
    """An idempotent identity is already bound to different persisted data."""


async def set_case_auto_send_policy(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    owner_user_id: UUID,
    enabled: bool,
    consent_reference: str | None = None,
    consent_text: str | None = None,
) -> UUID | None:
    """Enable or revoke autonomous sending for one case, as its current owner.

    Consent evidence is stored verbatim and immutable. Call this inside the
    caller's transaction; ``consent_reference`` should point to the exact user
    message or other durable source containing ``consent_text``.
    """

    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a boolean")
    case = await connection.fetchrow(
        """SELECT case_row.owner_user_id,case_row.status,owner.active
           FROM procurement_cases AS case_row
           JOIN users AS owner ON owner.id=case_row.owner_user_id
           WHERE case_row.id=$1 FOR UPDATE OF case_row""",
        case_id,
    )
    if (
        case is None
        or case["owner_user_id"] != owner_user_id
        or not case["active"]
        or case["status"] in {"closed", "cancelled"}
    ):
        raise AutoSendPolicyDenied(
            "case is absent, closed, or does not belong to an active acting owner"
        )

    active = await connection.fetchrow(
        """SELECT id,owner_user_id,consent_reference,consent_text
           FROM case_auto_send_policies
           WHERE case_id=$1 AND revoked_at IS NULL FOR UPDATE""",
        case_id,
    )
    if not enabled:
        if active is None:
            return None
        # The current case owner may revoke stale consent from a prior owner.
        await connection.execute(
            """UPDATE case_auto_send_policies
               SET revoked_at=now(),revoked_by=$2
               WHERE id=$1 AND revoked_at IS NULL""",
            active["id"],
            owner_user_id,
        )
        return active["id"]

    if not isinstance(consent_reference, str) or not consent_reference.strip():
        raise ValueError("consent_reference must be a non-empty immutable reference")
    if not isinstance(consent_text, str) or not consent_text.strip():
        raise ValueError("consent_text must be the verbatim non-empty owner authorization")
    if active is not None:
        if active["owner_user_id"] != owner_user_id:
            raise AutoSendPolicyDenied("active policy belongs to a different owner")
        if (
            active["consent_reference"] != consent_reference
            or active["consent_text"] != consent_text
        ):
            raise AutoSendPolicyConflict("active policy consent evidence is immutable")
        return active["id"]

    return await connection.fetchval(
        """INSERT INTO case_auto_send_policies(
               case_id,owner_user_id,consent_reference,consent_text
           ) VALUES ($1,$2,$3,$4) RETURNING id""",
        case_id,
        owner_user_id,
        consent_reference,
        consent_text,
    )


async def enable_uran_auto_send_policy(
    connection: asyncpg.Connection,
) -> UUID:
    """Persist the exact Uran owner authorization received for this case."""

    policy_id = await set_case_auto_send_policy(
        connection,
        case_id=URAN_CASE_ID,
        owner_user_id=URAN_OWNER_USER_ID,
        enabled=True,
        consent_reference=URAN_CONSENT_REFERENCE,
        consent_text=URAN_CONSENT_TEXT,
    )
    if policy_id is None:  # pragma: no cover - enabled always returns an id
        raise RuntimeError("Uran auto-send policy was not persisted")
    return policy_id


async def authorize_and_enqueue_auto_whatsapp(
    connection: asyncpg.Connection,
    *,
    approval_id: UUID,
    case_id: UUID,
    payload: Mapping[str, Any],
    requested_by: UUID | None,
) -> UUID | None:
    """Authorize one exact requested approval and queue its native worker job.

    Invoke inside the same transaction as ``request_approval``. Returns the
    durable job id when an active case policy authorized this exact approval,
    or ``None`` when the case has no active policy or the approval was already
    handled manually. The job contains only the approval id; its worker rechecks
    approval state, expiry, current contact, opt-out, pacing and exact payload
    before it can create an outbox event. No network access happens here.
    """

    canonical = canonical_payload_json(payload)
    digest = payload_sha256(payload)
    if payload.get("case_id") != str(case_id):
        raise AutoSendPolicyDenied("WhatsApp payload does not identify the requested case")
    if requested_by is None:
        raise AutoSendPolicyDenied("auto-send approval request has no owner provenance")

    policy = await connection.fetchrow(
        """SELECT policy.id,policy.owner_user_id,case_row.owner_user_id AS case_owner_id,
                  case_row.status AS case_status,owner.active AS owner_active
           FROM case_auto_send_policies AS policy
           JOIN procurement_cases AS case_row ON case_row.id=policy.case_id
           JOIN users AS owner ON owner.id=case_row.owner_user_id
           WHERE policy.case_id=$1 AND policy.revoked_at IS NULL""",
        case_id,
    )
    if policy is None:
        return None
    owner_id = policy["owner_user_id"]
    if (
        owner_id != policy["case_owner_id"]
        or requested_by != owner_id
        or not policy["owner_active"]
        or policy["case_status"] in {"closed", "cancelled"}
    ):
        raise AutoSendPolicyDenied(
            "active policy or approval request is not owned by the case owner"
        )

    approval = await connection.fetchrow(
        """SELECT id,case_id,action_type,payload,payload_sha256,status,requested_by,
                  approved_by,(expires_at IS NULL OR expires_at>now()) AS is_live
           FROM action_approvals WHERE id=$1 FOR UPDATE""",
        approval_id,
    )
    if approval is None:
        raise AutoSendPolicyDenied("send_whatsapp approval does not exist")
    if approval["case_id"] != case_id or approval["action_type"] != "send_whatsapp":
        raise AutoSendPolicyDenied("approval is for a different case or action")
    if approval["payload_sha256"] != digest:
        raise AutoSendPolicyDenied(
            "approval payload hash does not match the exact requested payload"
        )
    if canonical_payload_json(approval["payload"]) != canonical:
        raise AutoSendPolicyDenied("approval payload does not match the exact requested payload")
    if approval["requested_by"] != owner_id:
        raise AutoSendPolicyDenied("approval request provenance does not match the case owner")

    authorization = await connection.fetchrow(
        """SELECT policy_id,case_id,authorized_by,payload_sha256
           FROM auto_send_approval_authorizations WHERE approval_id=$1 FOR SHARE""",
        approval_id,
    )
    if authorization is None:
        if approval["status"] != "requested" or not approval["is_live"]:
            # Preserve manually approved, consumed, rejected, revoked and expired grants.
            return None
        changed = await connection.execute(
            """UPDATE action_approvals
               SET status='approved',approved_by=$2,approved_at=now()
               WHERE id=$1 AND status='requested' AND requested_by=$2
                 AND (expires_at IS NULL OR expires_at>now())""",
            approval_id,
            owner_id,
        )
        if changed != "UPDATE 1":
            return None
        await connection.execute(
            """INSERT INTO auto_send_approval_authorizations(
                   approval_id,policy_id,case_id,authorized_by,payload_sha256
               ) VALUES ($1,$2,$3,$4,$5)""",
            approval_id,
            policy["id"],
            case_id,
            owner_id,
            digest,
        )
    elif (
        authorization["policy_id"] != policy["id"]
        or authorization["case_id"] != case_id
        or authorization["authorized_by"] != owner_id
        or authorization["payload_sha256"] != digest
        or approval["status"] not in {"approved", "consumed"}
    ):
        # Do not rebind a prior episode or revive any previously handled grant.
        return None

    job_payload = {_AUTO_SEND_JOB_PAYLOAD_KEY: str(approval_id)}
    job_key = stable_key("auto-whatsapp-send", approval_id)
    job_id = await enqueue_job(
        connection,
        kind=_AUTO_SEND_JOB,
        payload=job_payload,
        idempotency_key=job_key,
        max_attempts=5,
    )
    if job_id is not None:
        return job_id
    existing = await connection.fetchrow(
        """SELECT id,kind,payload,idempotency_key FROM jobs WHERE idempotency_key=$1""",
        job_key,
    )
    if existing is None:
        raise RuntimeError("idempotent WhatsApp start job conflict vanished")
    if (
        existing["kind"] != _AUTO_SEND_JOB
        or existing["idempotency_key"] != job_key
        or dict(existing["payload"]) != job_payload
    ):
        raise AutoSendPolicyConflict("auto-send job key is bound to different durable data")
    return existing["id"]


async def auto_send_authorization_is_active(
    connection: asyncpg.Connection,
    *,
    approval_id: UUID,
) -> bool:
    """Return whether manual or currently authorized auto-send may proceed.

    Manual approvals have no auto-authorization audit row and remain governed
    by their ordinary exact-approval checks. Auto-authorized approvals require
    their original policy episode to remain active and owned by the case owner.
    """

    active = await connection.fetchval(
        """SELECT CASE
               WHEN NOT EXISTS (
                   SELECT 1 FROM auto_send_approval_authorizations
                   WHERE approval_id=$1
               ) THEN TRUE
               ELSE EXISTS (
                   SELECT 1
                   FROM auto_send_approval_authorizations AS auto_auth
                   JOIN case_auto_send_policies AS policy
                     ON policy.id=auto_auth.policy_id
                   JOIN procurement_cases AS case_row
                     ON case_row.id=auto_auth.case_id
                   JOIN users AS owner ON owner.id=case_row.owner_user_id
                   WHERE auto_auth.approval_id=$1
                     AND policy.case_id=auto_auth.case_id
                     AND policy.owner_user_id=auto_auth.authorized_by
                     AND case_row.owner_user_id=auto_auth.authorized_by
                     AND policy.revoked_at IS NULL
                     AND case_row.status NOT IN ('closed','cancelled')
                     AND owner.active
               )
           END""",
        approval_id,
    )
    return active is True


async def assert_auto_send_authorization_active(
    connection: asyncpg.Connection,
    *,
    approval_id: UUID,
) -> None:
    """Allow manual grants, but fail closed when auto consent was revoked."""

    if not await auto_send_authorization_is_active(connection, approval_id=approval_id):
        raise ValidationBlocked("case owner revoked autonomous WhatsApp sending")


async def get_case_send_policy(
    connection: asyncpg.Connection,
    case_id: UUID,
    default: SendPolicy,
) -> SendPolicy:
    """Return higher dialogue capacities only for a currently opted-in case."""

    active = await connection.fetchval(
        """SELECT EXISTS (
               SELECT 1 FROM case_auto_send_policies AS policy
               JOIN procurement_cases AS case_row ON case_row.id=policy.case_id
               JOIN users AS owner ON owner.id=case_row.owner_user_id
               WHERE policy.case_id=$1 AND policy.revoked_at IS NULL
                 AND policy.owner_user_id=case_row.owner_user_id
                 AND case_row.status NOT IN ('closed','cancelled')
                 AND owner.active
           )""",
        case_id,
    )
    if active is not True:
        return default
    return replace(
        default,
        daily_limit=30,
        conversation_limit=60,
        followup_limit=1,
    )


def next_send_eligible_at(
    now: datetime,
    *,
    policy: SendPolicy,
    last_sent_at: datetime | None = None,
) -> datetime:
    """Compute the next time outside quiet hours and past minimum spacing."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if last_sent_at is not None and last_sent_at.tzinfo is None:
        raise ValueError("last_sent_at must be timezone-aware")
    zone = ZoneInfo(policy.timezone)
    candidate = now.astimezone(UTC)
    if last_sent_at is not None:
        candidate = max(candidate, last_sent_at.astimezone(UTC) + policy.minimum_interval)
    local = candidate.astimezone(zone)
    local_time = local.timetz().replace(tzinfo=None)
    if policy.quiet_start <= policy.quiet_end:
        quiet = policy.quiet_start <= local_time < policy.quiet_end
    else:
        quiet = local_time >= policy.quiet_start or local_time < policy.quiet_end
    if quiet:
        send_day = local.date()
        if local_time >= policy.quiet_start and policy.quiet_start > policy.quiet_end:
            send_day += timedelta(days=1)
        local = datetime.combine(send_day, policy.quiet_end, tzinfo=zone)
        candidate = local.astimezone(UTC)
    return candidate
