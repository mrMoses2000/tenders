from __future__ import annotations

from uuid import UUID

import pytest
from aiogram.types import InlineKeyboardMarkup
from pydantic import ValidationError

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

APPROVAL_ID = UUID("126d47dd-daa0-43dc-bbed-f33d6cc4c434")


def test_send_message_payload_converts_keyboard_to_aiogram_types() -> None:
    parsed = parse_telegram_outbox_payload(
        TELEGRAM_SEND_MESSAGE,
        {
            "chat_id": 123456,
            "text": "  План готов.  ",
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "Запустить поиск",
                            "callback_data": f"rs:a:{APPROVAL_ID}",
                        },
                        {
                            "text": "Отмена",
                            "callback_data": f"rs:r:{APPROVAL_ID}",
                        },
                    ]
                ]
            },
        },
    )

    assert isinstance(parsed, TelegramSendMessagePayload)
    kwargs = to_aiogram_kwargs(parsed)
    assert kwargs["chat_id"] == 123456
    assert kwargs["text"] == "  План готов.  "
    assert kwargs["disable_web_page_preview"] is True
    assert isinstance(kwargs["reply_markup"], InlineKeyboardMarkup)
    buttons = kwargs["reply_markup"].inline_keyboard[0]
    assert [button.callback_data for button in buttons] == [
        f"rs:a:{APPROVAL_ID}",
        f"rs:r:{APPROVAL_ID}",
    ]


def test_plain_send_message_omits_reply_markup() -> None:
    parsed = parse_telegram_outbox_payload(
        TELEGRAM_SEND_MESSAGE,
        {"chat_id": 77, "text": "Готово"},
    )

    assert to_aiogram_kwargs(parsed) == {
        "chat_id": 77,
        "text": "Готово",
        "disable_web_page_preview": True,
    }


@pytest.mark.parametrize(
    "callback_data",
    [
        f"admin:delete:{APPROVAL_ID}",
        f"rs:a:{str(APPROVAL_ID).upper()}",
        "rs:a:not-a-uuid",
        "rs:a:" + "я" * 32,
    ],
)
def test_callback_data_rejects_unapproved_namespace_or_identifier(
    callback_data: str,
) -> None:
    with pytest.raises(ValidationError):
        TelegramSendMessagePayload.model_validate(
            {
                "chat_id": 77,
                "text": "Готово",
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "Выполнить", "callback_data": callback_data}]
                    ]
                },
            }
        )


def test_keyboard_rejects_arbitrary_button_api_fields() -> None:
    with pytest.raises(ValidationError, match="url"):
        TelegramSendMessagePayload.model_validate(
            {
                "chat_id": 77,
                "text": "Готово",
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {
                                "text": "Открыть",
                                "callback_data": f"rs:a:{APPROVAL_ID}",
                                "url": "https://example.com",
                            }
                        ]
                    ]
                },
            }
        )


def test_send_message_payload_is_strict_and_forbids_extra_method_parameters() -> None:
    with pytest.raises(ValidationError):
        TelegramSendMessagePayload.model_validate(
            {"chat_id": "77", "text": "Готово"}
        )

    with pytest.raises(ValidationError, match="parse_mode"):
        TelegramSendMessagePayload.model_validate(
            {"chat_id": 77, "text": "Готово", "parse_mode": "HTML"}
        )


def test_answer_callback_query_has_a_bounded_explicit_contract() -> None:
    parsed = parse_telegram_outbox_payload(
        TELEGRAM_ANSWER_CALLBACK_QUERY,
        {
            "callback_query_id": "callback-id",
            "text": "Запуск подтверждён",
            "show_alert": True,
            "cache_time": 5,
        },
    )

    assert isinstance(parsed, TelegramAnswerCallbackQueryPayload)
    assert to_aiogram_kwargs(parsed) == {
        "callback_query_id": "callback-id",
        "text": "Запуск подтверждён",
        "show_alert": True,
        "cache_time": 5,
    }


def test_callback_answer_rejects_telegram_limit_and_unexpected_url() -> None:
    with pytest.raises(ValidationError):
        TelegramAnswerCallbackQueryPayload.model_validate(
            {"callback_query_id": "callback-id", "text": "x" * 201}
        )

    with pytest.raises(ValidationError, match="url"):
        TelegramAnswerCallbackQueryPayload.model_validate(
            {"callback_query_id": "callback-id", "url": "https://example.com"}
        )


def test_unknown_event_type_is_not_dynamically_dispatched() -> None:
    with pytest.raises(ValueError, match="unsupported Telegram outbox event"):
        parse_telegram_outbox_payload(
            "telegram.delete_webhook",
            {"drop_pending_updates": True},
        )


def test_send_document_accepts_only_private_relative_artifact_reference() -> None:
    parsed = parse_telegram_outbox_payload(
        TELEGRAM_SEND_DOCUMENT,
        {
            "chat_id": 77,
            "storage_path": "reports/ab/report.html",
            "filename": "report.html",
            "caption": "Отчёт готов",
        },
    )
    assert isinstance(parsed, TelegramSendDocumentPayload)

    for unsafe in ("/etc/passwd", "../secret.html", "reports/../../secret", "a\\b"):
        with pytest.raises(ValidationError):
            TelegramSendDocumentPayload.model_validate(
                {
                    "chat_id": 77,
                    "storage_path": unsafe,
                    "filename": "report.html",
                }
            )
