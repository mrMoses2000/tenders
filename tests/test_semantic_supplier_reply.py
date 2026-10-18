from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from procurement_bot.providers.agy import AgyExtractor
from procurement_bot.semantic_supplier_reply import (
    SemanticReplyProposalOutput,
    SemanticReplyTarget,
    SemanticReplyTurn,
    SemanticReplyValidationError,
    _make_snapshot,
    assert_snapshot_current,
    proposal_prompt,
    propose_semantic_reply,
    validate_semantic_reply_proposal,
)


def _snapshot(*, multi: bool = False):
    first = SemanticReplyTarget(
        target_id=uuid4(),
        offer_id=uuid4(),
        request_item_id=uuid4(),
        line_number=1,
        item_name="Марля",
        quantity=Decimal("20"),
        unit="рулон",
        specification_text="Марля медицинская 10 метров",
    )
    targets = (first,)
    if multi:
        targets += (
            SemanticReplyTarget(
                target_id=uuid4(),
                offer_id=uuid4(),
                request_item_id=uuid4(),
                line_number=2,
                item_name="Бинт",
                quantity=Decimal("10"),
                unit="шт",
                specification_text="Бинт медицинский стерильный",
            ),
        )
    return _make_snapshot(
        conversation_id=uuid4(),
        message_id=uuid4(),
        case_id=uuid4(),
        supplier_id=uuid4(),
        message_text="Марля — цена 12 500 тенге за рулон, есть 20 рулонов",
        targets=targets,
    )


def _proposal(snapshot, **overrides):
    target = snapshot.targets[0]
    value = {
        "context_sha256": snapshot.context_sha256,
        "classification": "price_received",
        "classification_quote": "цена 12 500 тенге",
        "target_offer_id": str(target.offer_id),
        "target_item_id": str(target.request_item_id),
        "facts": [
            {
                "offer_id": str(target.offer_id),
                "item_id": str(target.request_item_id),
                "field": "price",
                "value": "12500",
                "evidence_quote": "Марля — цена 12 500 тенге за рулон",
            },
            {
                "offer_id": str(target.offer_id),
                "item_id": str(target.request_item_id),
                "field": "currency",
                "value": "KZT",
                "evidence_quote": "Марля — цена 12 500 тенге за рулон",
            },
        ],
        "reason": "Supplier quoted a price.",
    }
    value.update(overrides)
    return value


def test_snapshot_and_prompt_contain_only_supplier_safe_context() -> None:
    snapshot = _snapshot()
    prompt = proposal_prompt(snapshot)

    assert snapshot.context_sha256 in prompt
    assert snapshot.message_sha256
    assert "Марля медицинская" in prompt
    assert "тендерн" not in prompt.casefold()
    assert "price_amount" not in prompt


def test_valid_proposal_keeps_literal_per_field_quote_and_exact_target() -> None:
    snapshot = _snapshot()

    proposal = validate_semantic_reply_proposal(snapshot, _proposal(snapshot))

    assert proposal.target_offer_id == snapshot.targets[0].offer_id
    assert {fact.field for fact in proposal.facts} == {"price", "currency"}
    assert all(fact.evidence_quote in snapshot.message_text for fact in proposal.facts)


@pytest.mark.parametrize(
    ("text", "classification"),
    [
        ("На работе буду после 15", "needs_time"),
        ("Кейін жауап беремін", "needs_time"),
        ("Кто вы?", "identity_question"),
    ],
)
def test_semantic_time_and_identity_acts_require_a_literal_quote(text, classification) -> None:
    target = _snapshot().targets[0]
    snapshot = _make_snapshot(
        conversation_id=uuid4(),
        message_id=uuid4(),
        case_id=uuid4(),
        supplier_id=uuid4(),
        message_text=text,
        targets=(target,),
        pending_turn=SemanticReplyTurn(
            id=uuid4(),
            target_id=target.target_id,
            offer_id=target.offer_id,
            request_item_id=target.request_item_id,
            status="pending_reply",
            draft_text="Есть ли товар?",
        ),
    )
    proposal = validate_semantic_reply_proposal(
        snapshot,
        {
            "context_sha256": snapshot.context_sha256,
            "classification": classification,
            "classification_quote": text,
            "target_offer_id": str(target.offer_id),
            "target_item_id": str(target.request_item_id),
            "facts": [],
            "reason": "literal supplier message",
        },
    )

    assert proposal.classification == classification
    assert proposal.facts == ()


def test_semantic_identity_action_cannot_switch_multi_item_pending_turn() -> None:
    snapshot = _snapshot(multi=True)
    wrong_target = snapshot.targets[1]
    pending = SemanticReplyTurn(
        id=uuid4(),
        target_id=snapshot.targets[0].target_id,
        offer_id=snapshot.targets[0].offer_id,
        request_item_id=snapshot.targets[0].request_item_id,
        status="pending_reply",
        draft_text="Есть ли марля?",
    )
    snapshot = _make_snapshot(
        conversation_id=snapshot.conversation_id,
        message_id=snapshot.message_id,
        case_id=snapshot.case_id,
        supplier_id=snapshot.supplier_id,
        message_text="Кто вы?",
        targets=snapshot.targets,
        pending_turn=pending,
    )
    with pytest.raises(SemanticReplyValidationError, match="pending item turn"):
        validate_semantic_reply_proposal(
            snapshot,
            {
                "context_sha256": snapshot.context_sha256,
                "classification": "identity_question",
                "classification_quote": "Кто вы?",
                "target_offer_id": str(wrong_target.offer_id),
                "target_item_id": str(wrong_target.request_item_id),
                "facts": [],
                "reason": "literal supplier message",
            },
        )


def test_semantic_needs_time_binds_to_exact_pending_turn_in_multi_item_chat() -> None:
    base = _snapshot(multi=True)
    target = base.targets[0]
    snapshot = _make_snapshot(
        conversation_id=base.conversation_id,
        message_id=base.message_id,
        case_id=base.case_id,
        supplier_id=base.supplier_id,
        message_text="На работе буду после 15",
        targets=base.targets,
        pending_turn=SemanticReplyTurn(
            id=uuid4(),
            target_id=target.target_id,
            offer_id=target.offer_id,
            request_item_id=target.request_item_id,
            status="pending_reply",
            draft_text="Есть ли марля?",
        ),
    )

    proposal = validate_semantic_reply_proposal(
        snapshot,
        {
            "context_sha256": snapshot.context_sha256,
            "classification": "needs_time",
            "classification_quote": "На работе буду после 15",
            "target_offer_id": str(target.offer_id),
            "target_item_id": str(target.request_item_id),
            "facts": [],
            "reason": "supplier will check later",
        },
    )

    assert proposal.target_offer_id == target.offer_id
    assert proposal.facts == ()


@pytest.mark.parametrize(
    ("text", "classification"),
    [("Скиньте фото товара", "photo_request"), ("Бағасы қанша?", "supplier_question")],
)
def test_semantic_photo_and_supplier_questions_are_review_only(text, classification) -> None:
    target = _snapshot().targets[0]
    snapshot = _make_snapshot(
        conversation_id=uuid4(),
        message_id=uuid4(),
        case_id=uuid4(),
        supplier_id=uuid4(),
        message_text=text,
        targets=(target,),
    )

    proposal = validate_semantic_reply_proposal(
        snapshot,
        {
            "context_sha256": snapshot.context_sha256,
            "classification": classification,
            "classification_quote": text,
            "target_offer_id": None,
            "target_item_id": None,
            "facts": [],
            "reason": "requires owner review",
        },
    )

    assert proposal.target_offer_id is None
    assert proposal.facts == ()


@pytest.mark.asyncio
async def test_structured_extractor_boundary_returns_only_validated_proposal() -> None:
    snapshot = _snapshot()

    class Extractor:
        async def extract(self, text, output_model, **kwargs):
            assert snapshot.message_text in text
            assert kwargs["context_sha256"] == snapshot.context_sha256
            return output_model.model_validate(_proposal(snapshot))

    proposal = await propose_semantic_reply(Extractor(), snapshot)  # type: ignore[arg-type]

    assert proposal.context_sha256 == snapshot.context_sha256
    assert proposal.target_offer_id == snapshot.targets[0].offer_id
    with pytest.raises(ValueError):
        SemanticReplyProposalOutput.model_validate(_proposal(snapshot) | {"unexpected": True})


@pytest.mark.asyncio
async def test_real_agy_extractor_reaches_runner_with_legacy_snapshot_hash(
    tmp_path, monkeypatch
) -> None:
    snapshot = _snapshot()
    target = snapshot.targets[0]
    # Rebuild the pre-fix frozen payload independently so the test protects the
    # existing snapshot hash contract rather than asserting the helper against itself.
    legacy_context = {
        "conversation_id": str(snapshot.conversation_id),
        "message_id": str(snapshot.message_id),
        "case_id": str(snapshot.case_id),
        "supplier_id": str(snapshot.supplier_id),
        "message_sha256": snapshot.message_sha256,
        "media_sha256": snapshot.media_sha256,
        "media_needs_clarification": snapshot.media_needs_clarification,
        "targets": [
            {
                "target_id": str(target.target_id),
                "offer_id": str(target.offer_id),
                "item_id": str(target.request_item_id),
                "line_number": target.line_number,
                "item_name": target.item_name,
                "quantity": str(target.quantity),
                "unit": target.unit,
                "specification_text": target.specification_text,
            }
        ],
        "pending_turn": None,
    }
    expected_hash = hashlib.sha256(
        json.dumps(
            legacy_context, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    assert snapshot.context_sha256 == expected_hash

    extractor = AgyExtractor(home_dir=tmp_path)
    runner_calls = 0

    async def external_run(prompt, schema):
        nonlocal runner_calls
        runner_calls += 1
        assert f"CONTEXT_SHA256: {expected_hash}" in prompt
        return _proposal(snapshot)

    monkeypatch.setattr(extractor, "_run", external_run)

    proposal = await propose_semantic_reply(extractor, snapshot)

    assert runner_calls == 1
    assert proposal.context_sha256 == expected_hash
    assert proposal.target_offer_id == target.offer_id


@pytest.mark.parametrize(
    "change",
    [
        {"context_sha256": "0" * 64},
        {"target_offer_id": str(uuid4())},
        {"target_item_id": str(uuid4())},
        {"classification": "exact"},
        {"facts": [{"offer_id": "bad"}]},
    ],
)
def test_stale_unknown_or_unsupported_proposals_are_rejected(change) -> None:
    snapshot = _snapshot()

    with pytest.raises(SemanticReplyValidationError):
        validate_semantic_reply_proposal(snapshot, _proposal(snapshot, **change))


def test_amount_must_be_literal_and_quote_must_be_exact_message_substring() -> None:
    snapshot = _snapshot()
    raw = _proposal(snapshot)
    raw["facts"][0]["value"] = "125000"
    with pytest.raises(SemanticReplyValidationError, match="literally supported"):
        validate_semantic_reply_proposal(snapshot, raw)

    raw = _proposal(snapshot)
    raw["facts"][0]["evidence_quote"] = "цена 12 500 тенге за другой товар"
    with pytest.raises(SemanticReplyValidationError, match="literal message substring"):
        validate_semantic_reply_proposal(snapshot, raw)


def test_ambiguous_multitarget_reply_cannot_guess_an_item() -> None:
    snapshot = _snapshot(multi=True)
    raw = _proposal(snapshot)
    raw["classification"] = "ambiguous"
    raw["target_offer_id"] = None
    raw["target_item_id"] = None
    raw["facts"] = []

    proposal = validate_semantic_reply_proposal(snapshot, raw)
    assert proposal.target_offer_id is None
    assert proposal.facts == ()


def test_multitarget_price_requires_quote_to_name_target() -> None:
    snapshot = _snapshot(multi=True)
    raw = _proposal(snapshot)
    raw["facts"][0]["evidence_quote"] = "цена 12 500 тенге за рулон"
    raw["facts"][1]["evidence_quote"] = "цена 12 500 тенге за рулон"
    with pytest.raises(SemanticReplyValidationError, match="identify"):
        validate_semantic_reply_proposal(snapshot, raw)


def test_explicit_pending_item_turn_disambiguates_reply_for_one_target() -> None:
    snapshot = _snapshot(multi=True)
    target = snapshot.targets[0]
    text = "Цена 12 500 тенге за рулон"
    pending_turn = SemanticReplyTurn(
        id=uuid4(),
        target_id=target.target_id,
        offer_id=target.offer_id,
        request_item_id=target.request_item_id,
        status="pending_reply",
        draft_text="Уточните цену по марле.",
    )
    snapshot = _make_snapshot(
        conversation_id=snapshot.conversation_id,
        message_id=snapshot.message_id,
        case_id=snapshot.case_id,
        supplier_id=snapshot.supplier_id,
        message_text=text,
        targets=snapshot.targets,
        pending_turn=pending_turn,
    )
    raw = _proposal(snapshot)
    raw["classification_quote"] = "Цена 12 500 тенге"
    raw["facts"][0]["evidence_quote"] = text
    raw["facts"][1]["evidence_quote"] = text

    proposal = validate_semantic_reply_proposal(snapshot, raw)

    assert proposal.target_offer_id == target.offer_id


def test_duplicate_fact_fields_are_rejected() -> None:
    snapshot = _snapshot()
    raw = _proposal(snapshot)
    raw["facts"].append(dict(raw["facts"][0]))

    with pytest.raises(SemanticReplyValidationError, match="duplicate"):
        validate_semantic_reply_proposal(snapshot, raw)


def test_stock_amount_requires_its_literal_unit_and_field_quote() -> None:
    snapshot = _snapshot()
    raw = _proposal(snapshot)
    raw["facts"].extend(
        [
            {
                "offer_id": str(snapshot.targets[0].offer_id),
                "item_id": str(snapshot.targets[0].request_item_id),
                "field": "stock",
                "value": "20",
                "evidence_quote": "Марля — цена 12 500 тенге за рулон, есть 20 рулонов",
            },
            {
                "offer_id": str(snapshot.targets[0].offer_id),
                "item_id": str(snapshot.targets[0].request_item_id),
                "field": "stock_unit",
                "value": "рулон",
                "evidence_quote": "Марля — цена 12 500 тенге за рулон, есть 20 рулонов",
            },
        ]
    )

    proposal = validate_semantic_reply_proposal(snapshot, raw)

    assert {fact.field for fact in proposal.facts} == {
        "price",
        "currency",
        "stock",
        "stock_unit",
    }


def test_contradictory_availability_polarity_is_rejected() -> None:
    snapshot = _snapshot()
    snapshot = _make_snapshot(
        conversation_id=snapshot.conversation_id,
        message_id=snapshot.message_id,
        case_id=snapshot.case_id,
        supplier_id=snapshot.supplier_id,
        message_text="Нет в наличии",
        targets=snapshot.targets,
    )
    target = snapshot.targets[0]
    raw = _proposal(snapshot, classification="available")
    raw["classification_quote"] = "Нет в наличии"
    raw["target_offer_id"] = str(target.offer_id)
    raw["target_item_id"] = str(target.request_item_id)
    raw["facts"] = [
        {
            "offer_id": str(target.offer_id),
            "item_id": str(target.request_item_id),
            "field": "availability",
            "value": "in_stock",
            "evidence_quote": "Нет в наличии",
        }
    ]

    with pytest.raises(SemanticReplyValidationError):
        validate_semantic_reply_proposal(snapshot, raw)


def test_technical_exact_claim_is_not_an_allowed_fact_or_classification() -> None:
    snapshot = _snapshot()
    raw = _proposal(snapshot, classification="verified_exact")

    with pytest.raises(SemanticReplyValidationError, match="classification"):
        validate_semantic_reply_proposal(snapshot, raw)


def test_snapshot_change_fences_proposal_before_commit() -> None:
    snapshot = _snapshot()
    altered = replace(snapshot, context_sha256="f" * 64)

    with pytest.raises(SemanticReplyValidationError, match="stale"):
        assert_snapshot_current(snapshot, altered)
