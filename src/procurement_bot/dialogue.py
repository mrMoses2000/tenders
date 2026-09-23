from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from procurement_bot.intake import ParsedProcurementRequest


class ScopePolicyMode(StrEnum):
    REQUIRE_EXPLICIT = "require_explicit"
    APPLY_DEFAULT = "apply_default"


class SearchScopePolicy(BaseModel):
    """The caller must choose whether search scope is asked or defaulted."""

    model_config = ConfigDict(extra="forbid")

    mode: ScopePolicyMode
    default_scope: str | None = None

    @model_validator(mode="after")
    def valid_default(self) -> SearchScopePolicy:
        from procurement_bot.intake import SearchScope

        if self.mode == ScopePolicyMode.REQUIRE_EXPLICIT and self.default_scope is not None:
            raise ValueError("require_explicit cannot define a default scope")
        if self.mode == ScopePolicyMode.APPLY_DEFAULT:
            if self.default_scope is None:
                raise ValueError("apply_default requires default_scope")
            SearchScope(self.default_scope)
        return self

    @classmethod
    def require_explicit(cls) -> SearchScopePolicy:
        return cls(mode=ScopePolicyMode.REQUIRE_EXPLICIT)

    @classmethod
    def with_default(cls, scope: str) -> SearchScopePolicy:
        return cls(mode=ScopePolicyMode.APPLY_DEFAULT, default_scope=scope)


class BlockerCode(StrEnum):
    CITY_MISSING = "city_missing"
    SEARCH_SCOPE_MISSING = "search_scope_missing"
    SEARCH_AREA_MISSING = "search_area_missing"
    PRODUCT_MISSING = "product_missing"
    PRODUCT_NAME_AMBIGUOUS = "product_name_ambiguous"
    PRODUCT_FIELD_AMBIGUOUS = "product_field_ambiguous"
    REQUIREMENT_AMBIGUOUS = "requirement_ambiguous"
    TRANSCRIPT_AMBIGUOUS = "transcript_ambiguous"
    REQUEST_FIELD_AMBIGUOUS = "request_field_ambiguous"


class IntakeBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: BlockerCode
    field: str
    question: str
    item_index: int | None = None


class GapReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blockers: list[IntakeBlocker] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    ready_to_search: bool


def apply_search_scope_policy(
    request: ParsedProcurementRequest,
    policy: SearchScopePolicy,
) -> tuple[ParsedProcurementRequest, dict[str, str]]:
    from procurement_bot.intake import SearchScope

    if request.search_scope is not None or policy.mode == ScopePolicyMode.REQUIRE_EXPLICIT:
        return request, {}
    assert policy.default_scope is not None
    scope = SearchScope(policy.default_scope)
    return request.model_copy(update={"search_scope": scope}), {"search_scope": scope.value}


def _clarification_questions(blockers: list[IntakeBlocker], maximum: int = 1) -> list[str]:
    """Deduplicate while preserving deterministic priority/order."""
    questions: list[str] = []
    for blocker in blockers:
        if blocker.question not in questions:
            questions.append(blocker.question)
        if len(questions) == maximum:
            break
    return questions


def evaluate_missing_blockers(
    request: ParsedProcurementRequest,
    policy: SearchScopePolicy,
) -> GapReport:
    from procurement_bot.intake import SearchScope

    blockers: list[IntakeBlocker] = []
    # First establish that this is actually a procurement request.  Asking for
    # a city before the product is known makes ordinary chat look like a draft.
    if not request.items:
        blockers.append(
            IntakeBlocker(
                code=BlockerCode.PRODUCT_MISSING,
                field="items",
                question="Какой товар нужно найти?",
            )
        )
    if not request.city:
        blockers.append(
            IntakeBlocker(
                code=BlockerCode.CITY_MISSING,
                field="city",
                question="В каком городе искать товар?",
            )
        )
    if request.search_scope is None:
        blockers.append(
            IntakeBlocker(
                code=BlockerCode.SEARCH_SCOPE_MISSING,
                field="search_scope",
                question=(
                    "Где искать: по всему городу, в конкретном районе/на рынке "
                    "или онлайн с доставкой?"
                ),
            )
        )
    elif request.search_scope == SearchScope.SPECIFIC_AREA and not request.search_area_text:
        blockers.append(
            IntakeBlocker(
                code=BlockerCode.SEARCH_AREA_MISSING,
                field="search_area_text",
                question="Какой район, рынок или ориентир использовать для поиска?",
            )
        )
    if request.transcript_ambiguous:
        blockers.append(
            IntakeBlocker(
                code=BlockerCode.TRANSCRIPT_AMBIGUOUS,
                field="transcript",
                question=(
                    "Голос распознан неоднозначно. "
                    "Повторите название и характеристики товара."
                ),
            )
        )
    for field in request.ambiguous_fields:
        blockers.append(
            IntakeBlocker(
                code=BlockerCode.REQUEST_FIELD_AMBIGUOUS,
                field=field,
                question=f"Уточните значение поля «{field}».",
            )
        )
    for index, item in enumerate(request.items):
        label = item.name or f"позиция {index + 1}"
        if not item.name:
            blockers.append(
                IntakeBlocker(
                    code=BlockerCode.PRODUCT_NAME_AMBIGUOUS,
                    field=f"items.{index}.name",
                    item_index=index,
                    question=f"Уточните точное название товара в позиции {index + 1}.",
                )
            )
        elif item.confidence < 0.6 and not item.ambiguous_fields:
            blockers.append(
                IntakeBlocker(
                    code=BlockerCode.PRODUCT_FIELD_AMBIGUOUS,
                    field=f"items.{index}",
                    item_index=index,
                    question=f"Уточните название и характеристики товара «{label}».",
                )
            )
        for field in item.ambiguous_fields:
            blockers.append(
                IntakeBlocker(
                    code=BlockerCode.PRODUCT_FIELD_AMBIGUOUS,
                    field=f"items.{index}.{field}",
                    item_index=index,
                    question=f"Уточните «{field}» для товара «{label}».",
                )
            )
        for req_index, requirement in enumerate(item.requirements):
            if requirement.ambiguous or requirement.confidence < 0.6:
                blockers.append(
                    IntakeBlocker(
                        code=BlockerCode.REQUIREMENT_AMBIGUOUS,
                        field=f"items.{index}.requirements.{req_index}",
                        item_index=index,
                        question=(
                            f"Уточните требование «{requirement.name}» "
                            f"для товара «{label}»."
                        ),
                    )
                )
    questions = _clarification_questions(blockers)
    return GapReport(blockers=blockers, questions=questions, ready_to_search=not blockers)
