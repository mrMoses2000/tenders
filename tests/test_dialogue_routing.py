from uuid import uuid4

import pytest

from procurement_bot.dialogue_routing import (
    DialogueIntent,
    ordinary_reply,
    route_dialogue_turn,
)


@pytest.mark.parametrize(
    ("text", "intent", "reason"),
    [
        ("Что там?", DialogueIntent.CASE_QUERY, "supported_read_query"),
        ("Привет!", DialogueIntent.ORDINARY, "social_message"),
        (
            "Найди 20 рулонов марли в Алматы",
            DialogueIntent.CREATE_REQUEST,
            "explicit_procurement_request",
        ),
        (
            "Марля 20 рулонов",
            DialogueIntent.CREATE_REQUEST,
            "structured_item_list",
        ),
        ("Алматы", DialogueIntent.ORDINARY, "no_procurement_intent"),
    ],
)
def test_without_active_case_only_explicit_procurement_creates_request(
    text: str,
    intent: DialogueIntent,
    reason: str,
) -> None:
    route = route_dialogue_turn(
        text,
        input_kind="text",
        active_case_id=None,
        has_open_clarification=False,
    )
    assert route.intent == intent
    assert route.reason_code == reason


def test_answer_to_open_question_updates_active_request() -> None:
    route = route_dialogue_turn(
        "Алматы",
        input_kind="text",
        active_case_id=uuid4(),
        has_open_clarification=True,
    )
    assert route.intent == DialogueIntent.UPDATE_REQUEST
    assert route.reason_code == "answer_to_open_clarification"
    assert route.permits_intake


def test_yes_or_no_can_answer_an_open_procurement_question() -> None:
    route = route_dialogue_turn(
        "Да",
        input_kind="text",
        active_case_id=uuid4(),
        has_open_clarification=True,
    )
    assert route.intent == DialogueIntent.UPDATE_REQUEST


def test_explicit_new_request_does_not_silently_overwrite_active_request() -> None:
    route = route_dialogue_turn(
        "Создай новую заявку",
        input_kind="text",
        active_case_id=uuid4(),
        has_open_clarification=True,
    )
    assert route.intent == DialogueIntent.ACTIVE_CASE_CONFLICT
    assert not route.permits_intake


@pytest.mark.parametrize(
    ("active", "expected"),
    [
        (False, DialogueIntent.CREATE_REQUEST),
        (True, DialogueIntent.UPDATE_REQUEST),
    ],
)
def test_document_is_an_intake_source(active: bool, expected: DialogueIntent) -> None:
    route = route_dialogue_turn(
        "Текст технической спецификации",
        input_kind="document",
        active_case_id=uuid4() if active else None,
        has_open_clarification=active,
    )
    assert route.intent == expected


def test_unknown_message_with_active_case_does_not_mutate_it() -> None:
    route = route_dialogue_turn(
        "Расскажи анекдот",
        input_kind="text",
        active_case_id=uuid4(),
        has_open_clarification=False,
    )
    assert route.intent == DialogueIntent.ORDINARY
    assert not route.permits_intake


def test_off_topic_message_does_not_answer_open_clarification() -> None:
    route = route_dialogue_turn(
        "Расскажи анекдот",
        input_kind="text",
        active_case_id=uuid4(),
        has_open_clarification=True,
    )
    assert route.intent == DialogueIntent.ORDINARY


def test_help_reply_contains_no_internal_identifier() -> None:
    text = ordinary_reply(active_case_title="Марля", reason_code="social_message")
    assert "Марля" in text
    assert "UUID" not in text
    assert "hash" not in text
