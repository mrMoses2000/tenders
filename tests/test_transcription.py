import pytest

from procurement_bot.transcription import Transcript, transcript_review_reason


@pytest.mark.parametrize("language", ["ru", "kk", "ru-RU", "kk-KZ"])
def test_supported_confident_language_can_drive_intake(language: str) -> None:
    transcript = Transcript("текст", language, 0.9, "test")
    assert transcript_review_reason(transcript, minimum_language_confidence=0.55) is None


def test_unknown_or_uncertain_language_requires_human_confirmation() -> None:
    assert transcript_review_reason(
        Transcript("hello", "en", 0.99, "test"),
        minimum_language_confidence=0.55,
    ) == "unsupported_language:en"
    assert transcript_review_reason(
        Transcript("текст", "ru", 0.2, "test"),
        minimum_language_confidence=0.55,
    ) == "language_confidence_low"
    assert transcript_review_reason(
        Transcript("текст", "kk", None, "test"),
        minimum_language_confidence=0.55,
    ) == "language_confidence_missing"
