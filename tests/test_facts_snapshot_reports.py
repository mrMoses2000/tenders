import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import unittest

from tender_agent.facts import (
    EvidenceSource, FactAssertion, FactKind, LineDefinition, active_facts, reconcile_request,
)
from tender_agent.reports import render_operator_markdown
from tender_agent.snapshot import ProcurementSnapshot, snapshot_from_dict


FIXTURE = Path(__file__).parent / "fixtures" / "karaganda_reconciliation.json"


def load_snapshot() -> ProcurementSnapshot:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return snapshot_from_dict(data)


class ReconciliationGoldenTests(unittest.TestCase):
    def test_karaganda_direct_facts_win_without_deleting_conflicts(self) -> None:
        snapshot = load_snapshot()
        states = {state.line_id: state for state in snapshot.states}
        lime = states["karaganda_1:7"]
        self.assertEqual(lime.delivered_quantity, Decimal("200"))
        self.assertEqual(lime.reconciliation_status, "Конфликт — проверить в школе")
        self.assertIn("Поставка подтверждена, но школа товар не нашла", lime.conflicts)

        colors = states["karaganda_1:14"]
        self.assertEqual(colors.remaining_to_deliver, Decimal("20"))
        self.assertEqual(colors.reconciliation_status, "Частичная поставка")

        light = states["karaganda_1:18"]
        self.assertEqual(light.replacement_status, "Замена доставлена")
        self.assertEqual(light.reconciliation_status, "Замена выполнена — проверить")

    def test_household_and_laboratory_gloves_never_merge(self) -> None:
        snapshot = load_snapshot()
        states = {state.line_id: state for state in snapshot.states}
        self.assertEqual(states["karaganda_1:6"].delivered_quantity, Decimal("50"))
        self.assertEqual(states["karaganda_1:40"].delivered_quantity, Decimal("0"))
        self.assertEqual(states["karaganda_1:40"].reconciliation_status, "Не найдено в школе")

    def test_same_facts_produce_same_checksum(self) -> None:
        first = load_snapshot()
        second = ProcurementSnapshot.build(
            first.request_id, reversed(first.lines), reversed(first.facts),
            generated_at=datetime.now(timezone.utc),
        )
        self.assertEqual(first.checksum, second.checksum)

    def test_json_roundtrip_and_report_use_snapshot(self) -> None:
        snapshot = load_snapshot()
        restored = snapshot_from_dict(json.loads(snapshot.to_json()))
        self.assertEqual(snapshot.checksum, restored.checksum)
        report = render_operator_markdown(snapshot)
        self.assertIn("Докупить / допоставить", report)
        self.assertIn("№14 — Колер", report)
        self.assertIn("№7 — Известь", report)

    def test_supersession_is_append_only(self) -> None:
        line = LineDefinition("r:1", "r", "1", "Товар", "ТЗ", 1, "шт")
        old = FactAssertion(
            "f1", line.line_id, FactKind.DELIVERED, EvidenceSource.OPERATOR, "chat-1",
            datetime.now(timezone.utc), quantity=0,
        )
        new = FactAssertion(
            "f2", line.line_id, FactKind.DELIVERED, EvidenceSource.OPERATOR, "chat-2",
            datetime.now(timezone.utc), quantity=1, supersedes_id="f1",
        )
        self.assertEqual([fact.fact_id for fact in active_facts((old, new))], ["f2"])
        state = reconcile_request((line,), (old, new))[0]
        self.assertEqual(state.delivered_quantity, Decimal("1"))


if __name__ == "__main__":
    unittest.main()
