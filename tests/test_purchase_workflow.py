import pytest

from procurement_bot.purchase_workflow import parse_close_request


@pytest.mark.parametrize(
    ("text", "line", "reference"),
    [
        ("Я закрыл позицию 3", 3, None),
        ("Позиция №12 закрыта по заявке #deadbeef", 12, "deadbeef"),
        ("5-ші позицияны жаптым", 5, None),
    ],
)
def test_close_command_requires_an_exact_line_number(
    text: str,
    line: int,
    reference: str | None,
) -> None:
    parsed = parse_close_request(text)
    assert parsed is not None
    assert parsed.line_number == line
    assert parsed.case_reference == reference


@pytest.mark.parametrize(
    "text",
    [
        "Закрой все позиции",
        "Кажется, товар закрыт",
        "Позиция 0 закрыта",
        "Позиция 2 пока не закрыта",
    ],
)
def test_ambiguous_or_negative_close_statement_is_not_executed(text: str) -> None:
    assert parse_close_request(text) is None
