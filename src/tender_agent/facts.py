from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


def _decimal(value: Decimal | int | float | str | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class FactKind(StrEnum):
    PURCHASED = "purchased"
    DELIVERED = "delivered"
    FOUND_AT_SCHOOL = "found_at_school"
    NOT_FOUND_AT_SCHOOL = "not_found_at_school"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REPLACEMENT_REQUIRED = "replacement_required"
    REPLACEMENT_DELIVERED = "replacement_delivered"
    NOTE = "note"


class EvidenceSource(StrEnum):
    TENDER_DOCUMENT = "tender_document"
    PURCHASE_SHEET = "purchase_sheet"
    SCHOOL_SHEET = "school_sheet"
    OPERATOR = "operator"
    SUPPLIER = "supplier"
    SYSTEM = "system"


@dataclass(frozen=True)
class LineDefinition:
    line_id: str
    request_id: str
    number: str
    name: str
    technical_spec: str
    required_quantity: Decimal
    unit: str
    tender_unit_price: Decimal = Decimal("0")
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_quantity", _decimal(self.required_quantity))
        object.__setattr__(self, "tender_unit_price", _decimal(self.tender_unit_price))
        if not self.line_id.strip() or not self.request_id.strip():
            raise ValueError("line_id and request_id are required")
        if not self.number.strip() or not self.name.strip() or not self.unit.strip():
            raise ValueError("number, name and unit are required")
        if self.required_quantity is None or self.required_quantity <= 0:
            raise ValueError("required_quantity must be positive")
        if self.tender_unit_price is None or self.tender_unit_price < 0:
            raise ValueError("tender_unit_price cannot be negative")


@dataclass(frozen=True)
class FactAssertion:
    fact_id: str
    line_id: str
    kind: FactKind
    source_type: EvidenceSource
    source_ref: str
    observed_at: datetime
    quantity: Decimal | None = None
    unit: str = ""
    value: str = ""
    confidence: int = 3
    supersedes_id: str = ""
    note: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", FactKind(self.kind))
        object.__setattr__(self, "source_type", EvidenceSource(self.source_type))
        object.__setattr__(self, "quantity", _decimal(self.quantity))
        if not self.fact_id.strip() or not self.line_id.strip() or not self.source_ref.strip():
            raise ValueError("fact_id, line_id and source_ref are required")
        if self.quantity is not None and self.quantity < 0:
            raise ValueError("quantity cannot be negative")
        if not 1 <= self.confidence <= 5:
            raise ValueError("confidence must be between 1 and 5")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

    @classmethod
    def now(cls, **values: Any) -> FactAssertion:
        return cls(observed_at=datetime.now(UTC), **values)


@dataclass(frozen=True)
class LineState:
    line_id: str
    procurement_status: str
    movement_status: str
    location_status: str
    acceptance_status: str
    replacement_status: str
    reconciliation_status: str
    delivered_quantity: Decimal
    found_quantity: Decimal
    accepted_quantity: Decimal
    remaining_to_deliver: Decimal
    remaining_to_accept: Decimal
    conflicts: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    evidence_fact_ids: tuple[str, ...] = ()


_SOURCE_PRIORITY = {
    EvidenceSource.SYSTEM: 0,
    EvidenceSource.TENDER_DOCUMENT: 1,
    EvidenceSource.SUPPLIER: 2,
    EvidenceSource.PURCHASE_SHEET: 3,
    EvidenceSource.SCHOOL_SHEET: 4,
    EvidenceSource.OPERATOR: 5,
}


def active_facts(facts: Iterable[FactAssertion]) -> tuple[FactAssertion, ...]:
    facts_tuple = tuple(facts)
    ids = [fact.fact_id for fact in facts_tuple]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate fact_id")
    superseded = {fact.supersedes_id for fact in facts_tuple if fact.supersedes_id}
    unknown = superseded.difference(ids)
    if unknown:
        raise ValueError(f"unknown supersedes_id: {sorted(unknown)}")
    return tuple(
        sorted(
            (fact for fact in facts_tuple if fact.fact_id not in superseded),
            key=lambda fact: (
                fact.observed_at,
                _SOURCE_PRIORITY[fact.source_type],
                fact.fact_id,
            ),
        )
    )


def _latest(facts: Iterable[FactAssertion], *kinds: FactKind) -> FactAssertion | None:
    matching = [fact for fact in facts if fact.kind in kinds]
    if not matching:
        return None
    return max(
        matching,
        key=lambda fact: (
            _SOURCE_PRIORITY[fact.source_type],
            fact.observed_at,
            fact.confidence,
            fact.fact_id,
        ),
    )


def _quantity(fact: FactAssertion | None, default: Decimal = Decimal("0")) -> Decimal:
    if fact is None or fact.quantity is None:
        return default
    return fact.quantity


def reconcile_line(line: LineDefinition, facts: Iterable[FactAssertion]) -> LineState:
    selected = active_facts(fact for fact in facts if fact.line_id == line.line_id)
    required = line.required_quantity
    purchased = _quantity(_latest(selected, FactKind.PURCHASED))
    delivered_fact = _latest(selected, FactKind.DELIVERED)
    replacement_delivered = _latest(selected, FactKind.REPLACEMENT_DELIVERED)
    delivered = max(_quantity(delivered_fact), _quantity(replacement_delivered))
    found = _quantity(_latest(selected, FactKind.FOUND_AT_SCHOOL))
    not_found = _latest(selected, FactKind.NOT_FOUND_AT_SCHOOL)
    accepted = _quantity(_latest(selected, FactKind.ACCEPTED))
    rejected = _latest(selected, FactKind.REJECTED)
    replacement_required = _latest(selected, FactKind.REPLACEMENT_REQUIRED)

    if purchased >= required:
        procurement_status = "Закуплено полностью"
    elif purchased > 0:
        procurement_status = "Закуплено частично"
    else:
        procurement_status = "Не закуплено / неизвестно"

    if delivered >= required:
        movement_status = "Доставлено полностью"
    elif delivered > 0:
        movement_status = "Доставлено частично"
    elif purchased > 0:
        movement_status = "Закуплено, доставка не подтверждена"
    else:
        movement_status = "Доставка не подтверждена"

    if found >= required:
        location_status = "Найдено полностью"
    elif found > 0:
        location_status = "Найдено частично"
    elif not_found:
        location_status = "Школа не нашла"
    else:
        location_status = "Не проверено"

    if accepted >= required:
        acceptance_status = "Принято полностью"
    elif accepted > 0:
        acceptance_status = "Принято частично"
    elif rejected:
        acceptance_status = "Не принято"
    else:
        acceptance_status = "Не проверено"

    if replacement_delivered:
        replacement_status = "Замена доставлена"
    elif replacement_required:
        replacement_status = "Требуется замена"
    else:
        replacement_status = "Не требуется / неизвестно"

    remaining_to_deliver = max(Decimal("0"), required - delivered)
    remaining_to_accept = max(Decimal("0"), required - accepted)
    conflicts: list[str] = []
    actions: list[str] = []

    if not_found and delivered > 0:
        conflicts.append("Поставка подтверждена, но школа товар не нашла")
        actions.append("Проверить товар и место хранения в школе")
    if rejected and not replacement_required:
        conflicts.append("Есть отказ без отдельного факта о требовании замены")
    if accepted > delivered and delivered_fact is not None:
        conflicts.append("Принятое количество превышает подтверждённо доставленное")
    if replacement_required and not replacement_delivered:
        actions.append("Найти и поставить согласованную замену")
    if replacement_delivered and accepted < required:
        actions.append("Проверить приёмку доставленной замены")
    elif remaining_to_deliver > 0:
        actions.append(f"Допоставить {remaining_to_deliver.normalize()} {line.unit}")
    elif accepted < required:
        actions.append("Подтвердить приёмку в школе")

    if conflicts:
        reconciliation_status = "Конфликт — проверить в школе"
    elif replacement_required and not replacement_delivered:
        reconciliation_status = "Нужна замена"
    elif replacement_delivered and accepted < required:
        reconciliation_status = "Замена выполнена — проверить"
    elif delivered >= required and accepted >= required:
        reconciliation_status = "Закрыто и принято"
    elif delivered >= required:
        reconciliation_status = "Доставлено — приёмка не подтверждена"
    elif delivered > 0:
        reconciliation_status = "Частичная поставка"
    elif purchased > 0:
        reconciliation_status = "Закуплено — проверить местонахождение"
    elif not_found:
        reconciliation_status = "Не найдено в школе"
    else:
        reconciliation_status = "Нужно уточнить"

    return LineState(
        line_id=line.line_id,
        procurement_status=procurement_status,
        movement_status=movement_status,
        location_status=location_status,
        acceptance_status=acceptance_status,
        replacement_status=replacement_status,
        reconciliation_status=reconciliation_status,
        delivered_quantity=delivered,
        found_quantity=found,
        accepted_quantity=accepted,
        remaining_to_deliver=remaining_to_deliver,
        remaining_to_accept=remaining_to_accept,
        conflicts=tuple(conflicts),
        actions=tuple(dict.fromkeys(actions)),
        evidence_fact_ids=tuple(fact.fact_id for fact in selected),
    )


def reconcile_request(
    lines: Iterable[LineDefinition], facts: Iterable[FactAssertion]
) -> tuple[LineState, ...]:
    lines_tuple = tuple(lines)
    facts_tuple = tuple(facts)
    line_ids = [line.line_id for line in lines_tuple]
    if len(line_ids) != len(set(line_ids)):
        raise ValueError("duplicate line_id")
    fact_ids = [fact.fact_id for fact in facts_tuple]
    if len(fact_ids) != len(set(fact_ids)):
        raise ValueError("duplicate fact_id")
    known = set(line_ids)
    orphaned = {fact.line_id for fact in facts_tuple}.difference(known)
    if orphaned:
        raise ValueError(f"facts reference unknown lines: {sorted(orphaned)}")
    return tuple(reconcile_line(line, facts_tuple) for line in lines_tuple)
