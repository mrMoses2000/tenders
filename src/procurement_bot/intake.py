from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from procurement_bot.providers.agy import StructuredExtractor

CleanText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceType(StrEnum):
    TEXT = "text"
    DOCUMENT = "document"
    VOICE = "voice"


class SearchScope(StrEnum):
    CITYWIDE = "citywide"
    SPECIFIC_AREA = "specific_area"
    ONLINE_WITH_DELIVERY = "online_with_delivery"


class ProcurementRequirement(StrictModel):
    """One atomic, source-backed product requirement."""

    name: CleanText = Field(max_length=240)
    expected_value: CleanText | None = Field(default=None, max_length=1000)
    unit: CleanText | None = Field(default=None, max_length=80)
    hard_requirement: bool = True
    source_reference: CleanText | None = Field(
        default=None,
        max_length=240,
        description="Page, paragraph, row, or transcript time range containing the fact",
    )
    confidence: float = Field(default=1.0, ge=0, le=1)
    ambiguous: bool = False


class ProcurementItem(StrictModel):
    """A requested product line; missing values stay null rather than being inferred."""

    name: CleanText | None = Field(default=None, max_length=500)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=18, decimal_places=6)
    unit: CleanText | None = Field(default=None, max_length=80)
    tender_unit_price_private: Decimal | None = Field(
        default=None, ge=0, max_digits=16, decimal_places=4,
        description="Explicit customer tender unit price; never a supplier quote; unknown is null",
    )
    requirements: list[ProcurementRequirement] = Field(default_factory=list, max_length=100)
    analogs_allowed: bool | None = None
    source_reference: CleanText | None = Field(default=None, max_length=240)
    confidence: float = Field(default=1.0, ge=0, le=1)
    ambiguous_fields: list[CleanText] = Field(default_factory=list, max_length=20)

    @field_validator("ambiguous_fields")
    @classmethod
    def unique_ambiguous_fields(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class ParsedProcurementRequest(StrictModel):
    """Facts extracted from Telegram text, a document, or a voice transcript."""

    source_type: SourceType = SourceType.TEXT
    city: CleanText | None = Field(default=None, max_length=160)
    search_scope: SearchScope | None = None
    search_area_text: CleanText | None = Field(default=None, max_length=500)
    delivery_address: CleanText | None = Field(default=None, max_length=500)
    deadline_text: CleanText | None = Field(default=None, max_length=240)
    items: list[ProcurementItem] = Field(default_factory=list, max_length=200)
    transcript_ambiguous: bool = False
    ambiguous_fields: list[CleanText] = Field(default_factory=list, max_length=30)
    warnings: list[CleanText] = Field(default_factory=list, max_length=30)

    @field_validator("ambiguous_fields", "warnings")
    @classmethod
    def unique_strings(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class ProcurementRequestPatch(StrictModel):
    """Untrusted model proposal guarded by immutable spec/context hashes."""

    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: ParsedProcurementRequest


class ProcurementLocationPatch(StrictModel):
    """Small response for the location question; it cannot replace product rows."""

    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    city: CleanText | None = Field(default=None, max_length=160)
    search_scope: SearchScope | None = None
    search_area_text: CleanText | None = Field(default=None, max_length=500)
    transcript_ambiguous: bool = False
    ambiguous_fields: list[CleanText] = Field(default_factory=list, max_length=30)
    warnings: list[CleanText] = Field(default_factory=list, max_length=30)


class IntakeClarificationContext(StrictModel):
    """The one question actually shown to the user on the previous turn."""

    topic: CleanText = Field(max_length=160)
    question: CleanText = Field(max_length=1000)


INTAKE_SPEC: dict[str, Any] = {
    "id": "procurement-intake",
    "version": 1,
    "facts_only": True,
    "city_required": True,
    "model_questions_authoritative": False,
    "side_effects_allowed": False,
    "unknown_values": "null",
    "ambiguity": "mark_and_clarify",
}


def canonical_json_hash(value: Any) -> tuple[str, str]:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


_, INTAKE_SPEC_SHA256 = canonical_json_hash(INTAKE_SPEC)


def request_context_hash(request: ParsedProcurementRequest | None) -> tuple[str, str]:
    return canonical_json_hash(request or {})


def _merge_request(
    current: ParsedProcurementRequest | None,
    proposal: ParsedProcurementRequest,
) -> ParsedProcurementRequest:
    """Merge without allowing an uncertain extraction to erase known facts."""
    if current is None:
        return proposal
    update: dict[str, Any] = {"source_type": proposal.source_type}
    for field_name in (
        "city",
        "search_scope",
        "search_area_text",
        "delivery_address",
        "deadline_text",
    ):
        value = getattr(proposal, field_name)
        if value is not None:
            update[field_name] = value
    if proposal.items:
        prior_by_source = {
            item.source_reference: item
            for item in current.items
            if item.source_reference
        }
        update["items"] = [
            item.model_copy(update={
                "tender_unit_price_private": prior_by_source[
                    item.source_reference
                ].tender_unit_price_private
            })
            if item.tender_unit_price_private is None
            and item.source_reference in prior_by_source
            else item
            for item in proposal.items
        ]
    update["transcript_ambiguous"] = proposal.transcript_ambiguous
    # These are extraction-state flags, not facts: a later clarification must be
    # able to clear them deterministically.
    update["ambiguous_fields"] = proposal.ambiguous_fields
    update["warnings"] = list(dict.fromkeys([*current.warnings, *proposal.warnings]))
    return current.model_copy(update=update)


def apply_intake_patch(
    current: ParsedProcurementRequest | None,
    patch: ProcurementRequestPatch,
    *,
    expected_spec_sha256: str,
    expected_context_sha256: str,
) -> ParsedProcurementRequest:
    if patch.spec_sha256 != expected_spec_sha256:
        raise ValueError("intake spec changed; extraction must be retried")
    if patch.context_sha256 != expected_context_sha256:
        raise ValueError("intake context changed; extraction must be retried")
    return _merge_request(current, patch.request)


class IntakeResult(StrictModel):
    request: ParsedProcurementRequest
    patch: ProcurementRequestPatch
    blockers: list[Any] = Field(default_factory=list)
    clarifications: list[str] = Field(default_factory=list)
    applied_defaults: dict[str, str] = Field(default_factory=dict)
    ready_to_search: bool
    parser_name: str = "agy-intake"


_TABLE_ROW = re.compile(
    r"\[(table:\d+:row:\d+)\]\n(.*?)(?=\n\n\[[^\]]+\]\n|\Z)",
    re.DOTALL,
)


def _table_decimal(value: str) -> Decimal | None:
    normalized = value.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        number = Decimal(normalized) if normalized else None
        return number if number is None or number.is_finite() else None
    except InvalidOperation:
        return None


def _parse_priced_table(text: str) -> ParsedProcurementRequest | None:
    """Read explicit seven-column tender rows without inferring search locality."""
    rows = [
        (locator, content.rstrip())
        for locator, content in _TABLE_ROW.findall(text)
    ]
    item_tables: set[str] = set()
    for locator, content in rows:
        header = [part.strip().lower() for part in content.split(" | ")]
        if len(header) == 7 and (
            "наименование" in header[1]
            and "ед." in header[2]
            and "кол-во" in header[3]
            and "цена" in header[4]
            and "стоимость" in header[5]
            and "характеристика" in header[6]
        ):
            item_tables.add(locator.split(":")[1])
    if not item_tables:
        return None

    items: list[ProcurementItem] = []
    delivery_address: str | None = None
    deadline_text: str | None = None
    for locator, content in rows:
        cells = [part.strip() for part in content.split(" | ", 6)]
        if len(cells) == 2 and not cells[0] and "Республика Казахстан" in cells[1]:
            delivery_address = cells[1][:500]
        if len(cells) == 2 and cells[0].lower() == "срок поставки":
            deadline_text = cells[1][:240]
        if locator.split(":")[1] not in item_tables:
            continue
        if not re.fullmatch(r"\d+", cells[0]):
            continue
        if len(cells) != 7 or not cells[1] or int(cells[0]) != len(items) + 1:
            return None
        quantity = _table_decimal(cells[3])
        if quantity is None or quantity <= 0:
            return None
        price = _table_decimal(cells[4])
        if (cells[4] and price is None) or (price is not None and price < 0):
            return None
        spec = cells[6]
        if len(spec) > 1000:
            return None
        requirements = [
            ProcurementRequirement(
                name="Техническая характеристика",
                expected_value=spec,
                source_reference=locator,
            )
        ] if spec else []
        try:
            items.append(
                ProcurementItem(
                    name=cells[1],
                    quantity=quantity,
                    unit=cells[2] or None,
                    tender_unit_price_private=price,
                    requirements=requirements,
                    source_reference=locator,
                )
            )
        except ValueError:
            return None
    if not items or len(items) > 200:
        return None
    return ParsedProcurementRequest(
        source_type=SourceType.DOCUMENT,
        delivery_address=delivery_address,
        deadline_text=deadline_text,
        items=items,
    )


class IntakeService:
    """Safe extraction followed by application-owned deterministic gap evaluation."""

    def __init__(self, extractor: StructuredExtractor, *, scope_policy: Any | None = None) -> None:
        # Local import prevents the schema module and dialogue policy from becoming cyclic.
        from procurement_bot.dialogue import SearchScopePolicy

        self.extractor = extractor
        self.scope_policy = scope_policy or SearchScopePolicy.require_explicit()

    async def process(
        self,
        text: str,
        current: ParsedProcurementRequest | None = None,
        source_type: SourceType | Literal["text", "document", "voice"] = SourceType.TEXT,
        open_clarification: IntakeClarificationContext | None = None,
    ) -> IntakeResult:
        from procurement_bot.dialogue import apply_search_scope_policy, evaluate_missing_blockers

        source = SourceType(source_type)
        location_reply = (
            source in {SourceType.TEXT, SourceType.VOICE}
            and current is not None
            and open_clarification is not None
            and open_clarification.topic in {
                "city_missing", "search_scope_missing", "search_area_missing",
                "locality_resolution",
            }
        )
        model_current = current
        if location_reply:
            model_current = current.model_copy(update={"items": []})
        context_json, _ = request_context_hash(model_current)
        trusted_context = {
            "current_request": json.loads(context_json),
            "input_source_type": source.value,
            "open_clarification": (
                open_clarification.model_dump(mode="json")
                if open_clarification is not None
                else None
            ),
        }
        # Hash exactly what is supplied to the model, including the transport source.
        _, model_context_sha256 = canonical_json_hash(trusted_context)
        tabular = _parse_priced_table(text) if source == SourceType.DOCUMENT else None
        if tabular is not None and current is None:
            patch = ProcurementRequestPatch(
                spec_sha256=INTAKE_SPEC_SHA256,
                context_sha256=model_context_sha256,
                request=tabular,
            )
        elif location_reply:
            location = await self.extractor.extract(
                text,
                ProcurementLocationPatch,
                context=trusted_context,
                spec_sha256=INTAKE_SPEC_SHA256,
                context_sha256=model_context_sha256,
            )
            patch = ProcurementRequestPatch(
                spec_sha256=location.spec_sha256,
                context_sha256=location.context_sha256,
                request=ParsedProcurementRequest(
                    city=location.city,
                    search_scope=location.search_scope,
                    search_area_text=location.search_area_text,
                    transcript_ambiguous=location.transcript_ambiguous,
                    ambiguous_fields=location.ambiguous_fields,
                    warnings=location.warnings,
                ),
            )
        else:
            patch = await self.extractor.extract(
                text,
                ProcurementRequestPatch,
                context=trusted_context,
                spec_sha256=INTAKE_SPEC_SHA256,
                context_sha256=model_context_sha256,
            )
        merged = apply_intake_patch(
            current,
            patch,
            expected_spec_sha256=INTAKE_SPEC_SHA256,
            expected_context_sha256=model_context_sha256,
        )
        merged = merged.model_copy(update={"source_type": source})
        resolved, applied_defaults = apply_search_scope_policy(merged, self.scope_policy)
        report = evaluate_missing_blockers(resolved, self.scope_policy)
        return IntakeResult(
            request=resolved,
            patch=patch,
            blockers=[blocker.model_dump(mode="json") for blocker in report.blockers],
            clarifications=report.questions,
            applied_defaults=applied_defaults,
            ready_to_search=report.ready_to_search,
            parser_name=(
                "priced-table-intake"
                if tabular is not None and current is None
                else "agy-intake"
            ),
        )
