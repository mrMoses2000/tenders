from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class PerformanceEvent:
    event_id: str
    subject_type: str
    subject_id: str
    metric: str
    actual_value: Decimal
    occurred_at: datetime
    request_id: str = ""
    note: str = ""


@dataclass(frozen=True)
class PerformanceSummary:
    subject_id: str
    observations: int
    average_actual: Decimal | None
    minimum_actual: Decimal | None
    maximum_actual: Decimal | None


def summarize_actuals(
    subject_id: str, metric: str, events: Iterable[PerformanceEvent]
) -> PerformanceSummary:
    values = [event.actual_value for event in events if event.subject_id == subject_id and event.metric == metric]
    if not values:
        return PerformanceSummary(subject_id, 0, None, None, None)
    return PerformanceSummary(
        subject_id, len(values), sum(values) / len(values), min(values), max(values)
    )
