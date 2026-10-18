from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import asyncpg

from procurement_bot.approvals import request_approval
from procurement_bot.auto_send_policy import (
    authorize_and_enqueue_auto_whatsapp,
    get_case_send_policy,
)
from procurement_bot.chat_actors import ChatActorBusy, assign_supplier_dialogue
from procurement_bot.conversation_targets import (
    ConversationTargetInput,
    attach_conversation_target,
)
from procurement_bot.conversations import (
    ConversationInput,
    ConversationState,
    create_conversation,
    transition_conversation_state,
)
from procurement_bot.geographic_scope import GeographicDecision, decide_geographic_scope
from procurement_bot.intake import SearchScope
from procurement_bot.offer_comparison import normalize_goods_unit
from procurement_bot.phone import normalize_phone
from procurement_bot.queue import enqueue_outbox, stable_key
from procurement_bot.research import LocalityConstraint
from procurement_bot.research_scope import load_effective_scope
from procurement_bot.supplier_contact_scope import case_owned_supplier_contact_evidence
from procurement_bot.supplier_dialogue import (
    DEFAULT_SEND_POLICY,
    SupplierInquiry,
    WhatsAppSendCommand,
    build_availability_inquiry,
)


@dataclass(frozen=True, slots=True)
class OutreachPreparationResult:
    case_id: UUID
    proposals_created: int
    suppliers_without_whatsapp: int
    skipped_existing: int
    queued_turns: int = 0


class _SkipOutreachCandidate(Exception):
    """Roll back candidate-local setup when the parallel lane has no capacity."""


async def request_next_supplier_turn_approval(
    connection: asyncpg.Connection,
    *,
    conversation_id: UUID,
    requested_by: UUID,
) -> UUID | None:
    """Promote a queued turn, applying the owner's case-specific send policy."""

    async with connection.transaction():
        conversation = await connection.fetchrow(
            """
            SELECT c.case_id,c.state,c.opted_out,c.outbound_count,
                   procurement_case.status AS case_status,
                   owner.id AS owner_user_id,owner.telegram_id,
                   contact.normalized_value,contact.active
            FROM supplier_conversations AS c
            JOIN procurement_cases AS procurement_case ON procurement_case.id=c.case_id
            JOIN users AS owner ON owner.id=procurement_case.owner_user_id
            JOIN supplier_contacts AS contact ON contact.id=c.supplier_contact_id
            JOIN source_evidence AS contact_evidence
              ON contact_evidence.id=contact.source_evidence_id
            WHERE c.id=$1 AND owner.id=$2 AND owner.active
              AND __CONTACT_SCOPE__
            FOR UPDATE OF c,owner,contact
            """.replace(
                "__CONTACT_SCOPE__",
                case_owned_supplier_contact_evidence(
                    contact_alias="contact",
                    evidence_alias="contact_evidence",
                    scope="owner",
                ),
            ),
            conversation_id,
            requested_by,
        )
        if (
            conversation is None
            or not conversation["active"]
            or conversation["opted_out"]
            or conversation["telegram_id"] is None
            or conversation["case_status"] in {"closed", "cancelled"}
        ):
            raise ValueError("supplier conversation is unavailable for an item turn")
        if conversation["state"] in {"complete", "closed_no_reply", "opted_out"}:
            return None
        active_turn = await connection.fetchval(
            """
            SELECT id FROM supplier_conversation_item_turns
            WHERE conversation_id=$1
              AND status IN ('awaiting_approval','pending_reply','needs_time','ambiguous')
            LIMIT 1
            """,
            conversation_id,
        )
        if active_turn is not None:
            return None
        turn = await connection.fetchrow(
            """
            SELECT id,draft_text FROM supplier_conversation_item_turns
            WHERE conversation_id=$1 AND status='queued'
            ORDER BY turn_no,id LIMIT 1
            FOR UPDATE
            """,
            conversation_id,
        )
        if turn is None:
            return None
        recipient = normalize_phone(str(conversation["normalized_value"]))
        send_policy = await get_case_send_policy(
            connection, conversation["case_id"], DEFAULT_SEND_POLICY
        )
        command = WhatsAppSendCommand(
            case_id=conversation["case_id"],
            recipient=recipient,
            text=str(turn["draft_text"]),
            conversation_id=conversation_id,
            logical_action_id=f"supplier-item-turn:{turn['id']}",
            is_followup=(
                send_policy == DEFAULT_SEND_POLICY and int(conversation["outbound_count"]) > 0
            ),
        )
        approval_id = await request_approval(
            connection,
            case_id=conversation["case_id"],
            action_type="send_whatsapp",
            payload=command.approval_payload(),
            requested_by=requested_by,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        updated = await connection.execute(
            """
            UPDATE supplier_conversation_item_turns
            SET status='awaiting_approval',approval_id=$2,updated_at=now()
            WHERE id=$1 AND status='queued'
            """,
            turn["id"],
            approval_id,
        )
        if updated != "UPDATE 1":
            raise RuntimeError("queued supplier item turn changed during approval")
        auto_job = await authorize_and_enqueue_auto_whatsapp(
            connection,
            approval_id=approval_id,
            case_id=conversation["case_id"],
            payload=command.approval_payload(),
            requested_by=requested_by,
        )
        if auto_job is not None:
            return approval_id
        await enqueue_outbox(
            connection,
            event_type="telegram.send_message",
            payload={
                "chat_id": int(conversation["telegram_id"]),
                "text": (
                    "Черновик следующего вопроса поставщику ожидает вашего решения:\n\n"
                    f"{command.text}"
                ),
                "reply_markup": {
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
                },
            },
            idempotency_key=stable_key("telegram.supplier-item-turn", approval_id),
        )
        return approval_id


async def prepare_supplier_outreach(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    session_name: str,
    max_proposals: int = 100,
) -> OutreachPreparationResult:
    """Create approval-gated initial messages; never call the provider."""

    if not session_name.strip():
        raise ValueError("session_name must be non-empty")
    if not 1 <= max_proposals <= 100:
        raise ValueError("max_proposals must be between 1 and 100")
    async with connection.transaction():
        case = await connection.fetchrow(
            """
            SELECT procurement_case.owner_user_id,owner.telegram_id,procurement_case.status,
                   procurement_case.city,procurement_case.search_area_text,
                   (SELECT run.id FROM research_runs AS run
                    WHERE run.case_id=procurement_case.id
                    ORDER BY run.created_at DESC,run.id DESC LIMIT 1) AS latest_run_id
            FROM procurement_cases AS procurement_case
            JOIN users AS owner ON owner.id=procurement_case.owner_user_id
            WHERE procurement_case.id=$1 AND owner.active
            FOR SHARE OF procurement_case,owner
            """,
            case_id,
        )
        if case is None or case["telegram_id"] is None:
            raise ValueError("outreach case or active owner does not exist")
        if case["status"] not in {"evaluating", "contacting", "report_ready"}:
            if case["status"] != "researching":
                raise ValueError("case is not ready for supplier outreach")
            has_parallel_groups = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM research_runs AS run
                    JOIN research_item_groups AS item_group
                      ON item_group.research_run_id=run.id
                    WHERE run.case_id=$1
                )
                """,
                case_id,
            )
            if not has_parallel_groups:
                raise ValueError("case is not ready for supplier outreach")

        await connection.execute(
            """
            SELECT pg_advisory_xact_lock(hashtextextended($1,0))
            """,
            stable_key("supplier-outreach-case-lock", case_id),
        )
        # Dialogue slots are shared with reply projection, send validation and
        # the autonomous tick. Acquire the global cap lock before reading or
        # creating conversations so all those paths use one lock order.
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('supplier-dialogue-active-groups',0))"
        )

        candidates = await connection.fetch(
            """
            SELECT
                   offer.id AS offer_id,offer.supplier_id,offer.status AS offer_status,
                   item.id AS request_item_id,item.line_number,item.name,
                   item.quantity,item.unit,item.specification_text,
                   contact.id AS contact_id,contact.normalized_value,
                   evidence.id AS contact_evidence_id,
                   procurement_case.city AS case_city,
                   procurement_case.search_area_text,
                   location.city AS location_city,location.address_text
            FROM offers AS offer
            JOIN request_items AS item ON item.id=offer.request_item_id
            JOIN procurement_cases AS procurement_case ON procurement_case.id=item.case_id
            LEFT JOIN supplier_locations AS location
              ON location.id=offer.supplier_location_id AND location.active
            JOIN supplier_contacts AS contact
              ON contact.supplier_id=offer.supplier_id
             AND contact.active AND contact.contact_type='whatsapp'
            JOIN source_evidence AS evidence ON evidence.id=contact.source_evidence_id
            WHERE item.case_id=$1
              AND __CONTACT_SCOPE__
              AND offer.status=ANY($2::text[])
              AND NOT EXISTS (
                  SELECT 1 FROM supplier_conversation_targets target
                  WHERE target.offer_id=offer.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM supplier_conversations existing
                  WHERE existing.supplier_id=offer.supplier_id
                    AND (
                        existing.opted_out
                        OR (
                            existing.state NOT IN ('complete','closed_no_reply','opted_out')
                            AND (
                                existing.case_id<>item.case_id
                                OR existing.supplier_contact_id<>contact.id
                                OR existing.session_name<>$3
                                OR existing.external_chat_id<>
                                   regexp_replace(contact.normalized_value,'^\\+','') || '@c.us'
                            )
                        )
                    )
              )
              AND (
                NOT EXISTS(SELECT 1 FROM case_pipeline_runtime p WHERE p.case_id=$1 AND p.enabled)
                OR (
                  NOT EXISTS(SELECT 1 FROM case_pipeline_runtime p WHERE p.case_id=$1 AND p.paused)
                  AND (
                    (SELECT count(DISTINCT group_id) FROM supplier_dialogue_assignments
                     WHERE status IN ('assigned','waiting_reply')) < 3
                    OR EXISTS(SELECT 1 FROM supplier_dialogue_assignments a
                      WHERE a.request_item_id=item.id AND a.status IN ('assigned','waiting_reply'))
                  )
                  AND EXISTS(SELECT 1 FROM research_lanes lane
                    JOIN research_item_groups g ON g.id=lane.group_id
                    WHERE g.request_item_id=item.id AND g.research_run_id=(
                      SELECT research_run_id FROM case_pipeline_runtime WHERE case_id=$1)
                      AND (SELECT count(*) FROM supplier_dialogue_assignments a
                        WHERE a.lane_id=lane.id AND a.status IN ('assigned','waiting_reply')) < 5)
                )
              )
            ORDER BY CASE WHEN EXISTS(SELECT 1 FROM case_pipeline_runtime p
                      WHERE p.case_id=$1 AND p.enabled) THEN item.line_number ELSE NULL END,
                     offer.supplier_id,contact.id,
                     CASE offer.status WHEN 'exact' THEN 0 WHEN 'confirm' THEN 1 ELSE 2 END,
                     item.line_number,offer.created_at,contact.first_seen_at
            LIMIT $4
            """.replace(
                "__CONTACT_SCOPE__",
                case_owned_supplier_contact_evidence(
                    contact_alias="contact", evidence_alias="evidence"
                ),
            ),
            case_id,
            ["exact", "confirm", "lead"],
            session_name,
            max_proposals,
        )
        if case["search_area_text"]:
            locality = LocalityConstraint(
                city=case["city"],
                scope=SearchScope.SPECIFIC_AREA,
                raw_area_text=case["search_area_text"],
            )
            if case["latest_run_id"] is not None:
                effective_scope = await load_effective_scope(
                    connection, case["latest_run_id"], locality
                )
                locality = effective_scope.locality
            expected_decision = (
                GeographicDecision.CITY_VERIFIED
                if locality.scope == SearchScope.CITYWIDE
                else GeographicDecision.ACCEPTED_BY_ADDRESS
            )
            candidates = [
                candidate
                for candidate in candidates
                if decide_geographic_scope(
                    locality,
                    address_city=candidate["location_city"],
                    address_text=candidate["address_text"],
                )
                == expected_decision
            ]
        suppliers_without_whatsapp = int(
            await connection.fetchval(
                """
                SELECT count(DISTINCT offer.supplier_id)
                FROM offers AS offer
                JOIN request_items AS item ON item.id=offer.request_item_id
                WHERE item.case_id=$1 AND offer.status=ANY($2::text[])
                  AND NOT EXISTS (
                      SELECT 1 FROM supplier_contacts contact
                      JOIN source_evidence evidence
                        ON evidence.id=contact.source_evidence_id
                      WHERE contact.supplier_id=offer.supplier_id
                        AND contact.active AND contact.contact_type='whatsapp'
                        AND __CONTACT_SCOPE__
                  )
                """.replace(
                    "__CONTACT_SCOPE__",
                    case_owned_supplier_contact_evidence(
                        contact_alias="contact", evidence_alias="evidence"
                    ),
                ),
                case_id,
                ["exact", "confirm", "lead"],
            )
            or 0
        )
        created = 0
        skipped = 0
        queued_turns = 0
        for candidate in candidates:
            recipient = normalize_phone(candidate["normalized_value"])
            external_chat_id = recipient.removeprefix("+") + "@c.us"
            proposed_conversation_key = stable_key(
                "supplier-conversation",
                case_id,
                candidate["supplier_id"],
                candidate["contact_id"],
                candidate["offer_id"],
            )
            existing_transport = await connection.fetchrow(
                """
                SELECT c.idempotency_key,c.state,c.outbound_count,
                       (SELECT target.offer_id
                        FROM supplier_conversation_targets AS target
                        WHERE target.conversation_id=c.id
                        ORDER BY target.created_at,target.id LIMIT 1) AS first_offer_id
                FROM supplier_conversations AS c
                WHERE c.case_id=$1 AND c.supplier_id=$2 AND c.supplier_contact_id=$3
                  AND c.session_name=$4 AND c.external_chat_id=$5
                  AND c.state NOT IN ('complete','closed_no_reply','opted_out')
                FOR UPDATE
                """,
                case_id,
                candidate["supplier_id"],
                candidate["contact_id"],
                session_name,
                external_chat_id,
            )
            conversation_key = proposed_conversation_key
            append_to_existing = existing_transport is not None
            if append_to_existing:
                try:
                    first_offer_id = existing_transport["first_offer_id"]
                except (IndexError, KeyError):
                    first_offer_id = None
                expected_existing_key = (
                    stable_key(
                        "supplier-conversation",
                        case_id,
                        candidate["supplier_id"],
                        candidate["contact_id"],
                        first_offer_id,
                    )
                    if first_offer_id is not None
                    else None
                )
                if (
                    not expected_existing_key
                    or existing_transport["idempotency_key"] != expected_existing_key
                ):
                    # A manual or legacy conversation may already have its own
                    # approved payload. Preserve it and leave this item for a
                    # later explicit route.
                    skipped += 1
                    continue
                conversation_key = str(existing_transport["idempotency_key"])
            turn_id: UUID | None = None
            should_request_approval = not append_to_existing
            try:
                async with connection.transaction():
                    conversation = await create_conversation(
                        connection,
                        ConversationInput(
                            case_id=case_id,
                            supplier_id=candidate["supplier_id"],
                            supplier_contact_id=candidate["contact_id"],
                            session_name=session_name,
                            external_chat_id=external_chat_id,
                            idempotency_key=conversation_key,
                            preferred_language="ru",
                        ),
                    )
                    state = await connection.fetchrow(
                        """
                        SELECT state,outbound_count
                        FROM supplier_conversations WHERE id=$1 FOR UPDATE
                        """,
                        conversation.id,
                    )
                    if state is None:
                        raise RuntimeError("created supplier conversation vanished")
                    if state["state"] in {"complete", "closed_no_reply", "opted_out"}:
                        raise _SkipOutreachCandidate
                    if not append_to_existing and state["state"] not in {"draft", "approved"}:
                        raise _SkipOutreachCandidate
                    if not append_to_existing and state["state"] == "draft":
                        transitioned = await transition_conversation_state(
                            connection,
                            conversation_id=conversation.id,
                            expected_state=ConversationState.DRAFT,
                            new_state=ConversationState.APPROVED,
                        )
                        if not transitioned:
                            raise RuntimeError(
                                "supplier conversation could not enter approval stage"
                            )
                    target = await attach_conversation_target(
                        connection,
                        ConversationTargetInput(
                            conversation_id=conversation.id,
                            offer_id=candidate["offer_id"],
                            request_item_id=candidate["request_item_id"],
                            provenance_kind="research",
                            provenance_ref=f"offer:{candidate['offer_id']}",
                            source_evidence_id=candidate["contact_evidence_id"],
                            idempotency_key=stable_key(
                                "conversation-target",
                                conversation.id,
                                candidate["offer_id"],
                            ),
                        ),
                    )
                    turn_position = await connection.fetchrow(
                        """
                        SELECT COALESCE(max(turn_no),0)+1 AS turn_no,
                               NOT EXISTS (
                                   SELECT 1 FROM supplier_conversation_item_turns
                                   WHERE conversation_id=$1
                                     AND status IN (
                                         'awaiting_approval','pending_reply','needs_time',
                                         'ambiguous'
                                     )
                               ) AS no_open_turn
                        FROM supplier_conversation_item_turns
                        WHERE conversation_id=$1
                        """,
                        conversation.id,
                    )
                    if turn_position is None:
                        raise RuntimeError("supplier item turn position could not be allocated")
                    should_request_approval = not append_to_existing or (
                        bool(turn_position["no_open_turn"])
                        and state["state"] in {"draft", "approved"}
                        and int(state["outbound_count"]) == 0
                    )
                    draft_text = initial_inquiry_text(
                        str(candidate["name"]),
                        str(candidate["specification_text"] or ""),
                        candidate["quantity"],
                        candidate["unit"],
                    )
                    # Targets are immutable, but turns are history and may have
                    # several rows for one target after follow-up cycles. The
                    # case advisory lock above serializes this initial-turn
                    # idempotency lookup without inventing target uniqueness.
                    existing_turn = await connection.fetchrow(
                        """
                        SELECT id,draft_text
                        FROM supplier_conversation_item_turns
                        WHERE conversation_id=$1 AND target_id=$2
                        ORDER BY turn_no
                        LIMIT 1
                        FOR UPDATE
                        """,
                        conversation.id,
                        target.id,
                    )
                    if existing_turn is not None:
                        if existing_turn["draft_text"] != draft_text:
                            raise RuntimeError(
                                "existing supplier item turn differs from initial draft"
                            )
                        turn_id = existing_turn["id"]
                    else:
                        turn_id = await connection.fetchval(
                            """
                            INSERT INTO supplier_conversation_item_turns(
                                conversation_id,target_id,turn_no,status,draft_text
                            ) VALUES ($1,$2,$3,'queued',$4)
                            RETURNING id
                            """,
                            conversation.id,
                            target.id,
                            int(turn_position["turn_no"]),
                            draft_text,
                        )
                    if turn_id is None:
                        raise RuntimeError("supplier item turn could not be created")
                    assignment = await _assign_to_existing_research_group(
                        connection,
                        case_id=case_id,
                        item_id=candidate["request_item_id"],
                        supplier_id=candidate["supplier_id"],
                        conversation_id=conversation.id,
                        require_group=case["status"] == "researching",
                    )
                    if assignment is False:
                        raise _SkipOutreachCandidate
            except _SkipOutreachCandidate:
                skipped += 1
                continue

            if not should_request_approval:
                queued_turns += 1
                continue
            command = WhatsAppSendCommand(
                case_id=case_id,
                recipient=recipient,
                text=initial_inquiry_text(
                    str(candidate["name"]),
                    str(candidate["specification_text"] or ""),
                    candidate["quantity"],
                    candidate["unit"],
                ),
                conversation_id=conversation.id,
                logical_action_id=f"initial-availability:{candidate['offer_id']}",
            )
            approval_id = await request_approval(
                connection,
                case_id=case_id,
                action_type="send_whatsapp",
                payload=command.approval_payload(),
                requested_by=case["owner_user_id"],
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
            await connection.execute(
                """
                UPDATE supplier_conversation_item_turns
                SET status='awaiting_approval',approval_id=$2,updated_at=now()
                WHERE id=$1 AND status='queued'
                """,
                turn_id,
                approval_id,
            )
            auto_job = await authorize_and_enqueue_auto_whatsapp(
                connection,
                approval_id=approval_id,
                case_id=case_id,
                payload=command.approval_payload(),
                requested_by=case["owner_user_id"],
            )
            if auto_job is not None:
                created += 1
                continue
            await enqueue_outbox(
                connection,
                event_type="telegram.send_message",
                payload={
                    "chat_id": int(case["telegram_id"]),
                    "text": (
                        f"Черновик поставщику по позиции №{candidate['line_number']}:\n\n"
                        f"{command.text}"
                    ),
                    "reply_markup": {
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
                    },
                },
                idempotency_key=stable_key("telegram.outreach-approval", approval_id),
            )
            created += 1

        await connection.execute(
            """
            INSERT INTO workflow_events(
                case_id,event_type,actor_type,actor_id,idempotency_key,data
            ) VALUES ($1,'supplier_outreach_prepared','worker','outreach',$2,$3::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            case_id,
            stable_key("supplier-outreach-prepared", case_id),
            {
                "proposals_created": created,
                "suppliers_without_whatsapp": suppliers_without_whatsapp,
                "skipped_existing": skipped,
                "queued_turns": queued_turns,
            },
        )
        return OutreachPreparationResult(
            case_id=case_id,
            proposals_created=created,
            suppliers_without_whatsapp=suppliers_without_whatsapp,
            skipped_existing=skipped,
            queued_turns=queued_turns,
        )


async def _assign_to_existing_research_group(
    connection: asyncpg.Connection,
    *,
    case_id: UUID,
    item_id: UUID,
    supplier_id: UUID,
    conversation_id: UUID,
    require_group: bool = False,
) -> bool | None:
    """Use an available durable lane, or preserve legacy behavior without a group."""

    group = await connection.fetchrow(
        """
        SELECT item_group.id,item_group.target_suppliers
        FROM research_item_groups AS item_group
        JOIN research_runs AS run ON run.id=item_group.research_run_id
        WHERE run.case_id=$1 AND item_group.request_item_id=$2
        ORDER BY run.created_at DESC,item_group.created_at DESC,item_group.id
        LIMIT 1
        """,
        case_id,
        item_id,
    )
    if group is None:
        return False if require_group else None
    # Serialize lookup, lane selection and the idempotent assignment write with
    # the same lock used by assign_supplier_dialogue. A paused/stale assignment
    # remains the stable owner of this supplier-item pair.
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('supplier-dialogue-active-groups',0))"
    )
    existing_assignment = await connection.fetchrow(
        """
        SELECT assignment.id,assignment.lane_id,assignment.conversation_id
        FROM supplier_dialogue_assignments AS assignment
        JOIN research_lanes AS lane ON lane.id=assignment.lane_id
                                  AND lane.group_id=assignment.group_id
        WHERE assignment.group_id=$1
          AND assignment.request_item_id=$2
          AND assignment.supplier_id=$3
        FOR UPDATE OF assignment
        """,
        group["id"],
        item_id,
        supplier_id,
    )
    if existing_assignment is not None:
        if existing_assignment["conversation_id"] != conversation_id:
            # Never move a stable supplier-item lane to a new chat implicitly.
            # Candidate-local rollback preserves the existing assignment and
            # leaves this offer for an explicit resolution path.
            return False
        try:
            await assign_supplier_dialogue(
                connection,
                group_id=group["id"],
                lane_id=existing_assignment["lane_id"],
                item_id=item_id,
                supplier_id=supplier_id,
                conversation_id=conversation_id,
                target=int(group["target_suppliers"]),
                lane_count=4,
                max_per_lane=5,
            )
        except ChatActorBusy:
            return False
        return True
    lanes = await connection.fetch(
        """
        SELECT lane.id,lane.lane_no,count(assignment.id) AS active_count
        FROM research_lanes AS lane
        LEFT JOIN supplier_dialogue_assignments AS assignment
          ON assignment.lane_id=lane.id
         AND assignment.status IN ('assigned','waiting_reply')
        WHERE lane.group_id=$1
        GROUP BY lane.id,lane.lane_no
        ORDER BY count(assignment.id),lane.lane_no
        """,
        group["id"],
    )
    for lane in lanes:
        try:
            await assign_supplier_dialogue(
                connection,
                group_id=group["id"],
                lane_id=lane["id"],
                item_id=item_id,
                supplier_id=supplier_id,
                conversation_id=conversation_id,
                target=int(group["target_suppliers"]),
                lane_count=4,
                max_per_lane=5,
            )
            return True
        except ChatActorBusy:
            continue
    return False


def _quantity(value: Decimal | None, unit: str | None) -> str:
    quantity = "количество нужно уточнить" if value is None else format(value, "f")
    if "." in quantity:
        quantity = quantity.rstrip("0").rstrip(".")
    display_unit = {
        "piece": "шт.",
        "kg": "кг",
        "g": "г",
        "m": "м",
        "m2": "м²",
        "roll": "рул.",
        "liter": "л",
    }.get(normalize_goods_unit(unit), str(unit or "").strip())
    suffix = f" {display_unit}" if display_unit else ""
    return quantity + suffix


_GENERIC_ITEM_NAMES = frozenset(
    {
        "товар",
        "позиция",
        "изделие",
        "продукция",
        "материал",
        "плитка",
        "кабель",
        "труба",
        "краска",
        "лист",
        "плита",
        "панель",
        "крепеж",
        "крепёж",
        "оборудование",
        "инструмент",
        "смесь",
    }
)
_DETAIL_START = re.compile(
    r"\b(?:размер(?:ы)?|толщин\w*|ширин\w*|длин\w*|цвет\w*|материал\w*|"
    r"характеристик\w*|техническ\w*|предназначен\w*|соответств\w*)\b",
    re.IGNORECASE,
)


def product_label(item_name: str, specification_text: str = "") -> str:
    """Return a short supplier-facing name, never the full tender specification.

    Prefer the first concise product phrase in the specification when present.
    The full specification remains reserved for the follow-up after the
    supplier confirms availability.
    """

    name = " ".join(item_name.split()).strip(" .,:;—-")
    spec = " ".join(specification_text.split()).strip()
    if spec:
        if "армстронг" in spec.casefold() and any(
            token in spec.casefold() for token in ("потол", "плит", "панел")
        ):
            return "потолочная панель Armstrong"
        candidate = re.split(r"[.,;!?\n]", spec, maxsplit=1)[0]
        detail = _DETAIL_START.search(candidate)
        if detail:
            candidate = candidate[: detail.start()]
        candidate = candidate.strip(" .,:;—-")
        if (
            len(candidate.split()) >= 2
            and candidate.casefold() not in _GENERIC_ITEM_NAMES
            and not candidate.casefold().startswith(
                (
                    "по согласованию",
                    "техническая спецификация",
                    "технические характеристики",
                )
            )
        ):
            return _short_label(candidate)

    return _short_label(name) if name else "товар из заявки"


def initial_inquiry_text(
    item_name: str,
    specification_text: str,
    quantity: Decimal | None,
    unit: str | None,
) -> str:
    """Build the current version of an availability-first supplier message."""

    inquiry = SupplierInquiry(
        product_name=product_label(item_name, specification_text),
        quantity=_quantity(quantity, unit),
        required_spec=specification_text.strip() or "требования из заявки",
    )
    return build_availability_inquiry(inquiry, "ru")


def _short_label(value: str) -> str:
    """Cap labels so a long technical sentence cannot become a copied spec."""

    words = value.split()[:9]
    label = " ".join(words).strip(" .,:;—-")
    if len(label) > 72:
        label = label[:72].rsplit(" ", maxsplit=1)[0].strip(" .,:;—-")
    return label or "товар из заявки"
