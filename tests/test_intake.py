from decimal import Decimal

import pytest

from procurement_bot.dialogue import SearchScopePolicy
from procurement_bot.intake import (
    INTAKE_SPEC_SHA256,
    IntakeClarificationContext,
    IntakeService,
    ParsedProcurementRequest,
    ProcurementItem,
    ProcurementLocationPatch,
    ProcurementRequestPatch,
    SearchScope,
    SourceType,
    _parse_priced_table,
    apply_intake_patch,
    canonical_json_hash,
)
from procurement_bot.providers.agy import FakeExtractor


def patch_for(
    request: ParsedProcurementRequest,
    *,
    current: ParsedProcurementRequest | None = None,
    source_type: SourceType = SourceType.TEXT,
    open_clarification: IntakeClarificationContext | None = None,
) -> ProcurementRequestPatch:
    current_json = current.model_dump(mode="json") if current else {}
    context = {
        "current_request": current_json,
        "input_source_type": source_type.value,
        "open_clarification": (
            open_clarification.model_dump(mode="json") if open_clarification is not None else None
        ),
    }
    _, context_hash = canonical_json_hash(context)
    return ProcurementRequestPatch(
        spec_sha256=INTAKE_SPEC_SHA256,
        context_sha256=context_hash,
        request=request,
    )


@pytest.mark.asyncio
async def test_process_returns_merged_request_and_deterministic_clarification():
    proposal = ParsedProcurementRequest(items=[ProcurementItem(name="Марля", quantity=100)])
    fake = FakeExtractor(patch_for(proposal, source_type=SourceType.VOICE))
    result = await IntakeService(fake).process(
        "Найди сто метров марли",
        source_type="voice",
    )
    assert result.request.source_type == SourceType.VOICE
    assert result.request.items[0].name == "Марля"
    assert result.clarifications[0] == "В каком городе искать товар?"
    assert {blocker["code"] for blocker in result.blockers} == {
        "city_missing",
        "search_scope_missing",
    }
    assert not result.ready_to_search
    assert fake.calls[0]["text"] == "Найди сто метров марли"


@pytest.mark.asyncio
async def test_process_can_apply_only_an_explicit_scope_default():
    proposal = ParsedProcurementRequest(
        city="Алматы",
        items=[ProcurementItem(name="Марля")],
    )
    fake = FakeExtractor(patch_for(proposal))
    service = IntakeService(
        fake,
        scope_policy=SearchScopePolicy.with_default(SearchScope.CITYWIDE),
    )
    result = await service.process("Найди марлю в Алматы")
    assert result.ready_to_search
    assert result.applied_defaults == {"search_scope": "citywide"}


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type", [SourceType.TEXT, SourceType.VOICE])
@pytest.mark.parametrize("topic", ["city_missing", "locality_resolution"])
async def test_process_passes_the_one_visible_question_as_context(source_type, topic):
    current = ParsedProcurementRequest(
        items=[
            ProcurementItem(
                name="Марля",
                quantity=100,
                tender_unit_price_private=Decimal("1234"),
                source_reference="table:2:row:3",
            )
        ]
    )
    clarification = IntakeClarificationContext(
        topic=topic,
        question="В каком городе искать товар?",
    )
    narrow_current = current.model_copy(update={"items": []})
    context_patch = patch_for(
        ParsedProcurementRequest(city="Алматы"),
        current=narrow_current,
        source_type=source_type,
        open_clarification=clarification,
    )
    fake = FakeExtractor(
        ProcurementLocationPatch(
            spec_sha256=context_patch.spec_sha256,
            context_sha256=context_patch.context_sha256,
            city="Алматы",
            search_area_text="Рынки вдоль шоссе Северное Кольцо",
        )
    )
    result = await IntakeService(fake).process(
        "Алматы",
        current=current,
        source_type=source_type,
        open_clarification=clarification,
    )
    assert fake.calls[0]["context"]["open_clarification"] == {
        "topic": topic,
        "question": "В каком городе искать товар?",
    }
    assert fake.calls[0]["context"]["current_request"]["items"] == []
    assert result.request.items == current.items
    assert result.request.city == "Алматы"
    assert result.request.search_area_text == "Рынки вдоль шоссе Северное Кольцо"


PRICED_TABLE = (
    "[table:1:row:1]\n"
    "Наименование | Значение\n\n"
    "[table:1:row:2]\n"
    " | Республика Казахстан, п. Таукент\n\n"
    "[table:2:row:1]\n"
    "№ п/п | Наименование | Ед. изм. | Кол-во | Цена | Стоимость | Краткая характеристика\n\n"
    "[table:2:row:2]\n"
    "1 | Силикагель | кг | 5,50 | 3 000 | 16500 | Индикаторный\nСиний\n\n"
    "[table:2:row:3]\n"
    "2 | Светильник | шт | 10 | 1000 | 10000 | 60 Вт"
)


@pytest.mark.asyncio
async def test_document_table_without_currency_preserves_rows_and_specs_and_asks_city():
    extractor = FakeExtractor({})
    result = await IntakeService(extractor).process(PRICED_TABLE, source_type="document")
    assert extractor.calls == []
    assert result.parser_name == "priced-table-intake"
    assert len(result.request.items) == 2
    item = result.request.items[0]
    assert item.quantity == Decimal("5.50")
    assert item.tender_unit_price_private is None
    assert item.requirements[0].expected_value == "Индикаторный\nСиний"
    assert item.source_reference == "table:2:row:2"
    assert result.request.items[1].tender_unit_price_private is None
    assert result.request.warnings == [
        "Цена в документе не включена в финансовые показатели: валюта не подтверждена как KZT."
    ]
    assert result.request.delivery_address == "Республика Казахстан, п. Таукент"
    assert result.request.city is None
    assert result.clarifications == ["В каком городе искать товар?"]


@pytest.mark.parametrize(
    ("price_header", "expected_prices", "has_warning"),
    [
        ("Цена, KZT", [Decimal("3000"), Decimal("1000")], False),
        ("Цена, тенге", [Decimal("3000"), Decimal("1000")], False),
        ("Цена, тг", [Decimal("3000"), Decimal("1000")], False),
        ("Цена, ₸", [Decimal("3000"), Decimal("1000")], False),
        ("Цена за единицу, KZT", [Decimal("3000"), Decimal("1000")], False),
        ("Цена (KZT)", [Decimal("3000"), Decimal("1000")], False),
        ("Цена в теңге за единицу", [Decimal("3000"), Decimal("1000")], False),
        ("Цена (KZT", [None, None], True),
        ("Цена KZT)", [None, None], True),
        ("Цена, USD", [None, None], True),
        ("Цена, KZT/USD", [None, None], True),
        ("Цена, KZT/JPY", [None, None], True),
        ("Цена, KZT/KGS", [None, None], True),
        ("Цена, тенге/рублях", [None, None], True),
        ("Цена, KZT за договором", [None, None], True),
    ],
)
def test_priced_table_currency_must_be_explicit_and_supported(
    price_header, expected_prices, has_warning
):
    table = PRICED_TABLE.replace("Цена | Стоимость", f"{price_header} | Стоимость")
    request = _parse_priced_table(table)

    assert request is not None
    assert [item.tender_unit_price_private for item in request.items] == expected_prices
    assert [item.name for item in request.items] == ["Силикагель", "Светильник"]
    assert [item.quantity for item in request.items] == [Decimal("5.50"), Decimal("10")]
    assert [item.unit for item in request.items] == ["кг", "шт"]
    assert [item.requirements[0].expected_value for item in request.items] == [
        "Индикаторный\nСиний",
        "60 Вт",
    ]
    assert bool(request.warnings) is has_warning


@pytest.mark.parametrize(
    "bad",
    [
        PRICED_TABLE.replace("5,50", "не указано"),
        PRICED_TABLE.replace("2 | Светильник", "3 | Светильник"),
        PRICED_TABLE.replace("3 000", "договорная"),
    ],
)
def test_unreliable_table_falls_back_without_partial_rows(bad):
    assert _parse_priced_table(bad) is None


def test_stale_patch_is_rejected():
    request = ParsedProcurementRequest(city="Алматы")
    patch = ProcurementRequestPatch(
        spec_sha256=INTAKE_SPEC_SHA256,
        context_sha256="a" * 64,
        request=request,
    )
    with pytest.raises(ValueError, match="context changed"):
        apply_intake_patch(
            None,
            patch,
            expected_spec_sha256=INTAKE_SPEC_SHA256,
            expected_context_sha256="b" * 64,
        )


def test_merge_preserves_known_facts_but_can_clear_resolved_ambiguity():
    current = ParsedProcurementRequest(
        city="Алматы",
        search_scope=SearchScope.CITYWIDE,
        items=[ProcurementItem(name="Кабель", ambiguous_fields=["сечение"])],
        ambiguous_fields=["deadline"],
    )
    proposal = ParsedProcurementRequest(
        items=[ProcurementItem(name="Кабель ВВГ", ambiguous_fields=[])],
        ambiguous_fields=[],
    )
    _, context_hash = canonical_json_hash({"current": "pinned"})
    patch = ProcurementRequestPatch(
        spec_sha256=INTAKE_SPEC_SHA256,
        context_sha256=context_hash,
        request=proposal,
    )
    merged = apply_intake_patch(
        current,
        patch,
        expected_spec_sha256=INTAKE_SPEC_SHA256,
        expected_context_sha256=context_hash,
    )
    assert merged.city == "Алматы"
    assert merged.search_scope == SearchScope.CITYWIDE
    assert merged.items[0].name == "Кабель ВВГ"
    assert merged.ambiguous_fields == []
