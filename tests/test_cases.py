from __future__ import annotations

from uuid import UUID

from procurement_bot.cases import _telegram_reply
from procurement_bot.intake import (
    IntakeResult,
    ParsedProcurementRequest,
    ProcurementRequestPatch,
)


def _result(*, ready: bool, questions: list[str]) -> IntakeResult:
    request = ParsedProcurementRequest(city="Алматы")
    patch = ProcurementRequestPatch(
        spec_sha256="a" * 64,
        context_sha256="b" * 64,
        request=request,
    )
    return IntakeResult(
        request=request,
        patch=patch,
        blockers=[],
        clarifications=questions,
        ready_to_search=ready,
    )


def test_ready_reply_announces_deterministic_plan_preparation() -> None:
    reply = _telegram_reply(_result(ready=True, questions=[]), UUID(int=1))
    assert "Заявку разобрал" in reply
    assert "подготовлю поиск" in reply
    assert "00000000" not in reply


def test_clarification_reply_asks_questions_without_internal_identifier() -> None:
    reply = _telegram_reply(
        _result(ready=False, questions=["В каком городе?", "Где искать?"]),
        UUID(int=2),
    )
    assert "• В каком городе?" in reply
    assert "• Где искать?" in reply
    assert "00000000" not in reply
