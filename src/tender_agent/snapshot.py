from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Iterable

from .facts import FactAssertion, LineDefinition, LineState, reconcile_request
from .intake import SourceArtifact


SCHEMA_VERSION = 1


def _line_sort_key(line: LineDefinition) -> tuple[int, int | str, str]:
    try:
        return (0, int(line.number), line.line_id)
    except ValueError:
        return (1, line.number, line.line_id)


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


@dataclass(frozen=True)
class ProcurementSnapshot:
    schema_version: int
    request_id: str
    generated_at: datetime
    lines: tuple[LineDefinition, ...]
    facts: tuple[FactAssertion, ...]
    states: tuple[LineState, ...]
    artifacts: tuple[SourceArtifact, ...] = ()

    @classmethod
    def build(
        cls, request_id: str, lines: Iterable[LineDefinition], facts: Iterable[FactAssertion],
        artifacts: Iterable[SourceArtifact] = (), generated_at: datetime | None = None,
    ) -> "ProcurementSnapshot":
        lines_tuple = tuple(sorted(lines, key=_line_sort_key))
        facts_tuple = tuple(sorted(facts, key=lambda fact: fact.fact_id))
        if any(line.request_id != request_id for line in lines_tuple):
            raise ValueError("all lines must belong to request_id")
        states = reconcile_request(lines_tuple, facts_tuple)
        snapshot = cls(
            schema_version=SCHEMA_VERSION, request_id=request_id,
            generated_at=generated_at or datetime.now(timezone.utc), lines=lines_tuple,
            facts=facts_tuple, states=states,
            artifacts=tuple(sorted(artifacts, key=lambda artifact: artifact.artifact_id)),
        )
        snapshot.validate()
        return snapshot

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        numbers = [line.number for line in self.lines]
        if len(numbers) != len(set(numbers)):
            raise ValueError("duplicate line number within request")
        line_by_id = {line.line_id: line for line in self.lines}
        if set(line_by_id) != {state.line_id for state in self.states}:
            raise ValueError("state coverage does not match lines")
        for state in self.states:
            line = line_by_id[state.line_id]
            for value in (
                state.delivered_quantity, state.found_quantity, state.accepted_quantity,
                state.remaining_to_deliver, state.remaining_to_accept,
            ):
                if value < 0:
                    raise ValueError("snapshot quantities cannot be negative")
            if state.remaining_to_deliver != max(Decimal("0"), line.required_quantity - state.delivered_quantity):
                raise ValueError("remaining_to_deliver invariant failed")

    def as_dict(self, include_checksum: bool = True) -> dict[str, Any]:
        payload = _json_ready(asdict(self))
        if include_checksum:
            payload["checksum"] = self.checksum
        return payload

    @property
    def checksum(self) -> str:
        payload = self.as_dict(include_checksum=False)
        # generation time is metadata, not procurement state.
        payload.pop("generated_at", None)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def snapshot_from_dict(data: dict[str, Any]) -> ProcurementSnapshot:
    lines = tuple(LineDefinition(**line) for line in data["lines"])
    facts = tuple(
        FactAssertion(
            **{
                **fact,
                "observed_at": datetime.fromisoformat(fact["observed_at"]),
            }
        )
        for fact in data["facts"]
    )
    artifacts = tuple(SourceArtifact(**item) for item in data.get("artifacts", ()))
    snapshot = ProcurementSnapshot.build(
        request_id=data["request_id"], lines=lines, facts=facts, artifacts=artifacts,
        generated_at=datetime.fromisoformat(data["generated_at"]),
    )
    expected = data.get("checksum")
    if expected and snapshot.checksum != expected:
        raise ValueError("snapshot checksum mismatch")
    return snapshot
