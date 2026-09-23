import pytest

from procurement_bot.dialogue import SearchScopePolicy
from procurement_bot.intake import (
    INTAKE_SPEC_SHA256,
    IntakeClarificationContext,
    IntakeService,
    ParsedProcurementRequest,
    ProcurementItem,
    ProcurementRequestPatch,
    SearchScope,
    SourceType,
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
            open_clarification.model_dump(mode="json")
            if open_clarification is not None
            else None
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
async def test_process_passes_the_one_visible_question_as_context():
    current = ParsedProcurementRequest(items=[ProcurementItem(name="Марля")])
    clarification = IntakeClarificationContext(
        topic="city_missing",
        question="В каком городе искать товар?",
    )
    proposal = ParsedProcurementRequest(city="Алматы")
    fake = FakeExtractor(
        patch_for(
            proposal,
            current=current,
            open_clarification=clarification,
        )
    )
    await IntakeService(fake).process(
        "Алматы",
        current=current,
        open_clarification=clarification,
    )
    assert fake.calls[0]["context"]["open_clarification"] == {
        "topic": "city_missing",
        "question": "В каком городе искать товар?",
    }


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
