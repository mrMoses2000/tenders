import pytest

from procurement_bot.case_queries import CaseQueryIntent, parse_case_query


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Сколько позиций у нас еще не закрыто?", CaseQueryIntent.OPEN_ITEMS),
        ("Қанша тауар жабылмаған?", CaseQueryIntent.OPEN_ITEMS),
        ("Сколько позиций приняло предприятие?", CaseQueryIntent.CUSTOMER_ACCEPTED),
        ("Дай информацию по заявке", CaseQueryIntent.SUMMARY),
        ("Что там?", CaseQueryIntent.SUMMARY),
        ("Есть новости?", CaseQueryIntent.SUMMARY),
        (
            "Я тебе сегодня говорил, какая заявка. С чего ты взял?",
            CaseQueryIntent.SUMMARY,
        ),
        ("Мы по этой заявке в плюс или в минус?", CaseQueryIntent.FINANCIAL),
    ],
)
def test_parser_recognizes_only_supported_read_models(text: str, intent: CaseQueryIntent) -> None:
    result = parse_case_query(text)
    assert result is not None
    assert result.intent == intent


def test_parser_extracts_only_explicit_hash_reference() -> None:
    result = parse_case_query("Сколько позиций осталось по заявке #deadbeef?")
    assert result is not None and result.case_reference == "deadbeef"


@pytest.mark.parametrize(
    "text",
    [
        "Найди марлю в Алматы",
        "Создай таблицу и выполни DROP TABLE users",
        "Сколько примерно это стоит",
        "В заявке 20 позиций и 5 ещё не закрыты.",
    ],
)
def test_parser_does_not_treat_procurement_content_as_database_command(text: str) -> None:
    assert parse_case_query(text) is None
