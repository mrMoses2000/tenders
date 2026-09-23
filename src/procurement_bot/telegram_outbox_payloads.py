from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, Final, Literal
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TELEGRAM_SEND_MESSAGE: Final = "telegram.send_message"
TELEGRAM_ANSWER_CALLBACK_QUERY: Final = "telegram.answer_callback_query"
TELEGRAM_SEND_DOCUMENT: Final = "telegram.send_document"

# Keep this list deliberately small. Adding a prefix grants the producer access to a
# callback-handler command namespace, so additions require a corresponding handler review.
ALLOWED_CALLBACK_PREFIXES: Final = (
    "rs:a:",
    "rs:r:",
    "wa:a:",
    "wa:r:",
)


class StrictTelegramPayload(BaseModel):
    """Base for persisted commands crossing the Telegram provider boundary."""

    model_config = ConfigDict(extra="forbid", strict=True)


class TelegramCallbackButton(StrictTelegramPayload):
    """A callback-only button; URL, payment and web-app buttons are not permitted."""

    text: str = Field(min_length=1, max_length=64)
    callback_data: str = Field(min_length=1, max_length=64)

    @field_validator("text")
    @classmethod
    def text_must_be_visible(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("button text must contain a visible character")
        return value

    @field_validator("callback_data")
    @classmethod
    def validate_callback_data(cls, value: str) -> str:
        # Telegram's limit is bytes, whereas Pydantic's max_length counts code points.
        if len(value.encode("utf-8")) > 64:
            raise ValueError("callback_data exceeds Telegram's 64-byte limit")

        prefix = next(
            (candidate for candidate in ALLOWED_CALLBACK_PREFIXES if value.startswith(candidate)),
            None,
        )
        if prefix is None:
            raise ValueError("callback_data prefix is not allowed")

        identifier = value.removeprefix(prefix)
        try:
            parsed = UUID(identifier)
        except (ValueError, AttributeError) as exc:
            raise ValueError("callback_data must end with an approval UUID") from exc
        if identifier != str(parsed):
            raise ValueError("callback_data approval UUID must use canonical lowercase form")
        return value


class TelegramInlineKeyboard(StrictTelegramPayload):
    inline_keyboard: list[list[TelegramCallbackButton]] = Field(
        min_length=1,
        max_length=20,
    )

    @field_validator("inline_keyboard")
    @classmethod
    def validate_rows(
        cls,
        value: list[list[TelegramCallbackButton]],
    ) -> list[list[TelegramCallbackButton]]:
        if any(not row for row in value):
            raise ValueError("inline keyboard rows must not be empty")
        if any(len(row) > 8 for row in value):
            raise ValueError("inline keyboard rows may contain at most 8 buttons")
        if sum(map(len, value)) > 100:
            raise ValueError("inline keyboard may contain at most 100 buttons")
        return value


class TelegramSendMessagePayload(StrictTelegramPayload):
    chat_id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=4096)
    reply_markup: TelegramInlineKeyboard | None = None

    @field_validator("text")
    @classmethod
    def text_must_be_visible(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message text must contain a visible character")
        return value


class TelegramAnswerCallbackQueryPayload(StrictTelegramPayload):
    callback_query_id: str = Field(min_length=1, max_length=256)
    text: str | None = Field(default=None, min_length=1, max_length=200)
    show_alert: bool = False
    cache_time: int = Field(default=0, ge=0, le=86_400)

    @field_validator("callback_query_id")
    @classmethod
    def callback_query_id_must_be_visible(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("callback_query_id must contain a visible character")
        return value

    @model_validator(mode="after")
    def callback_text_must_be_visible(self) -> TelegramAnswerCallbackQueryPayload:
        if self.text is not None and not self.text.strip():
            raise ValueError("callback answer text must contain a visible character")
        return self


class TelegramSendDocumentPayload(StrictTelegramPayload):
    """A private artifact reference resolved below RESULTS_ROOT at delivery time."""

    chat_id: int = Field(gt=0)
    storage_path: str = Field(min_length=1, max_length=1000)
    filename: str = Field(min_length=1, max_length=200)
    caption: str | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("storage_path")
    @classmethod
    def storage_path_must_be_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value or value.endswith("/"):
            raise ValueError("storage_path must be a safe relative POSIX path")
        return value

    @field_validator("filename")
    @classmethod
    def filename_must_be_plain(cls, value: str) -> str:
        if not value.strip() or PurePosixPath(value).name != value or "\\" in value:
            raise ValueError("filename must be a plain visible name")
        return value


type TelegramOutboxPayload = (
    TelegramSendMessagePayload
    | TelegramAnswerCallbackQueryPayload
    | TelegramSendDocumentPayload
)
type TelegramEventType = Literal[
    "telegram.send_message",
    "telegram.answer_callback_query",
    "telegram.send_document",
]


def parse_telegram_outbox_payload(
    event_type: str,
    payload: Mapping[str, object],
) -> TelegramOutboxPayload:
    """Parse only explicitly supported Telegram commands.

    The event type is never translated to a method name dynamically. This is the
    allowlist boundary that prevents a persisted event from invoking arbitrary Bot API
    methods.
    """

    if event_type == TELEGRAM_SEND_MESSAGE:
        return TelegramSendMessagePayload.model_validate(payload)
    if event_type == TELEGRAM_ANSWER_CALLBACK_QUERY:
        return TelegramAnswerCallbackQueryPayload.model_validate(payload)
    if event_type == TELEGRAM_SEND_DOCUMENT:
        return TelegramSendDocumentPayload.model_validate(payload)
    raise ValueError(f"unsupported Telegram outbox event: {event_type}")


def to_aiogram_kwargs(payload: TelegramOutboxPayload) -> dict[str, Any]:
    """Build kwargs for the one aiogram method selected by the caller's type branch."""

    if isinstance(payload, TelegramSendMessagePayload):
        result: dict[str, Any] = {
            "chat_id": payload.chat_id,
            "text": payload.text,
            "disable_web_page_preview": True,
        }
        if payload.reply_markup is not None:
            result["reply_markup"] = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=button.text,
                            callback_data=button.callback_data,
                        )
                        for button in row
                    ]
                    for row in payload.reply_markup.inline_keyboard
                ]
            )
        return result

    if isinstance(payload, TelegramAnswerCallbackQueryPayload):
        return {
            "callback_query_id": payload.callback_query_id,
            "text": payload.text,
            "show_alert": payload.show_alert,
            "cache_time": payload.cache_time,
        }

    # Kept as a defensive guard if the TypeAlias gains a new member without an explicit
    # conversion branch.
    raise TypeError(f"unsupported Telegram payload model: {type(payload).__name__}")
