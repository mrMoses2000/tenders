from procurement_bot.dialogue import (
    BlockerCode,
    SearchScopePolicy,
    apply_search_scope_policy,
    evaluate_missing_blockers,
)
from procurement_bot.intake import (
    ParsedProcurementRequest,
    ProcurementItem,
    ProcurementRequirement,
    SearchScope,
)


def test_city_and_scope_are_deterministic_initial_blockers():
    report = evaluate_missing_blockers(
        ParsedProcurementRequest(items=[ProcurementItem(name="Марля")]),
        SearchScopePolicy.require_explicit(),
    )
    assert [gap.code for gap in report.blockers] == [
        BlockerCode.CITY_MISSING,
        BlockerCode.SEARCH_SCOPE_MISSING,
    ]
    assert report.questions == [
        "В каком городе искать товар?",
    ]
    assert not report.ready_to_search


def test_specific_area_requires_area_text():
    request = ParsedProcurementRequest(
        city="Алматы",
        search_scope=SearchScope.SPECIFIC_AREA,
        items=[ProcurementItem(name="Фреза")],
    )
    report = evaluate_missing_blockers(request, SearchScopePolicy.require_explicit())
    assert [gap.code for gap in report.blockers] == [BlockerCode.SEARCH_AREA_MISSING]


def test_explicit_default_is_applied_and_recorded():
    request = ParsedProcurementRequest(city="Караганда", items=[ProcurementItem(name="Марля")])
    policy = SearchScopePolicy.with_default(SearchScope.CITYWIDE)
    resolved, defaults = apply_search_scope_policy(request, policy)
    report = evaluate_missing_blockers(resolved, policy)
    assert resolved.search_scope == SearchScope.CITYWIDE
    assert defaults == {"search_scope": "citywide"}
    assert report.ready_to_search


def test_ambiguous_transcript_item_and_requirement_block_search():
    request = ParsedProcurementRequest(
        city="Алматы",
        search_scope=SearchScope.CITYWIDE,
        transcript_ambiguous=True,
        items=[
            ProcurementItem(
                name="Кабель",
                ambiguous_fields=["сечение"],
                requirements=[
                    ProcurementRequirement(
                        name="материал жилы",
                        expected_value="медь или алюминий",
                        ambiguous=True,
                    )
                ],
            )
        ],
    )
    report = evaluate_missing_blockers(request, SearchScopePolicy.require_explicit())
    assert [gap.code for gap in report.blockers] == [
        BlockerCode.TRANSCRIPT_AMBIGUOUS,
        BlockerCode.PRODUCT_FIELD_AMBIGUOUS,
        BlockerCode.REQUIREMENT_AMBIGUOUS,
    ]
    # Telegram asks one thing at a time; all blockers remain available for persistence.
    assert len(report.questions) == 1
    assert len(report.blockers) == 3


def test_low_confidence_product_blocks_even_without_named_ambiguous_field():
    request = ParsedProcurementRequest(
        city="Алматы",
        search_scope=SearchScope.CITYWIDE,
        items=[ProcurementItem(name="шарошка или фреза", confidence=0.4)],
    )
    report = evaluate_missing_blockers(request, SearchScopePolicy.require_explicit())
    assert [gap.code for gap in report.blockers] == [BlockerCode.PRODUCT_FIELD_AMBIGUOUS]
