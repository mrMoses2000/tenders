from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from tender_agent.intake import OCRObservation, SourceArtifact, check_intake_completeness
from tender_agent.notion_sync import InMemoryNotionGateway, apply_sync_plan, plan_notion_sync
from tender_agent.sourcing import (
    CandidateMaturity, CoveragePolicy, SourcingCandidate, evaluate_coverage,
    decide_next_search, remaining_search_routes,
)


class NotionSyncTests(unittest.TestCase):
    def test_dry_run_does_not_write_and_apply_verifies(self) -> None:
        gateway = InMemoryNotionGateway({"p": {"№": "7", "Статус сверки": "Старый"}})
        plan = plan_notion_sync(
            {"p": gateway.get_properties("p")}, {"p": {"№": "7", "Статус сверки": "Новый"}}
        )
        self.assertEqual(apply_sync_plan(gateway, plan).status, "dry_run")
        self.assertEqual(gateway.get_properties("p")["Статус сверки"], "Старый")
        self.assertEqual(apply_sync_plan(gateway, plan, dry_run=False).status, "applied")
        self.assertEqual(gateway.get_properties("p")["Статус сверки"], "Новый")

    def test_immutable_spec_change_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "immutable"):
            plan_notion_sync({"p": {"Тех. спек": "A"}}, {"p": {"Тех. спек": "B"}})

    def test_partial_failure_returns_rollback_plan(self) -> None:
        class BrokenGateway(InMemoryNotionGateway):
            def update_properties(self, page_id, properties):
                if page_id == "p2":
                    raise RuntimeError("network")
                super().update_properties(page_id, properties)

        rows = {"p1": {"Статус": "a"}, "p2": {"Статус": "a"}}
        plan = plan_notion_sync(rows, {"p1": {"Статус": "b"}, "p2": {"Статус": "b"}})
        result = apply_sync_plan(BrokenGateway(rows), plan, dry_run=False)
        self.assertEqual(result.status, "partial_failure")
        self.assertEqual(result.applied_page_ids, ("p1",))
        self.assertEqual(result.rollback_patches[0].desired_properties, {"Статус": "a"})

    def test_verification_failure_rolls_back_the_changed_page(self) -> None:
        class CorruptGateway(InMemoryNotionGateway):
            def get_properties(self, page_id):
                values = dict(super().get_properties(page_id))
                values["Статус"] = "corrupted"
                return values

        rows = {"p": {"Статус": "a"}}
        result = apply_sync_plan(
            CorruptGateway(rows), plan_notion_sync(rows, {"p": {"Статус": "b"}}), dry_run=False
        )
        self.assertEqual(result.status, "partial_failure")
        self.assertEqual(result.rollback_patches[0].desired_properties, {"Статус": "a"})


class SourcingQualityTests(unittest.TestCase):
    def candidate(self, index, maturity=CandidateMaturity.LEAD, **overrides):
        values = {
            "candidate_id": f"c{index}", "line_id": "r:1", "supplier_id": f"s{index}",
            "maturity": maturity, "checked_at": datetime.now(timezone.utc),
        }
        values.update(overrides)
        return SourcingCandidate(**values)

    def test_five_weak_links_are_not_enough(self) -> None:
        result = evaluate_coverage([self.candidate(i) for i in range(5)])
        self.assertFalse(result.sufficient)
        self.assertEqual(result.fresh_candidates, 5)
        self.assertEqual(result.fresh_actionable, 0)

    def test_quality_coverage_requires_actionable_local_and_reserve(self) -> None:
        candidates = [self.candidate(i) for i in range(3)] + [
            self.candidate(
                4, CandidateMaturity.SUPPLIER_QUOTE, technical_match_proven=True,
                price_confirmed=True, contact_available=True, is_local=True,
            ),
            self.candidate(
                5, CandidateMaturity.PURCHASE_READY, technical_match_proven=True,
                price_confirmed=True, stock_confirmed=True, contact_available=True, is_reserve=True,
            ),
        ]
        result = evaluate_coverage(candidates)
        self.assertTrue(result.sufficient)
        self.assertEqual(result.stop_reason, "quality_coverage_met")

    def test_stale_quote_does_not_count(self) -> None:
        stale = self.candidate(
            1, CandidateMaturity.SUPPLIER_QUOTE,
            checked_at=datetime.now(timezone.utc) - timedelta(days=3),
            technical_match_proven=True, price_confirmed=True, contact_available=True,
        )
        result = evaluate_coverage([stale], CoveragePolicy(0, 1, False, False))
        self.assertEqual(result.fresh_actionable, 0)

    def test_route_history_is_not_repeated(self) -> None:
        self.assertNotIn("history", [step.route for step in remaining_search_routes(["history"])])

    def test_exhausted_routes_are_explicit(self) -> None:
        decision = decide_next_search(
            [], [step.route for step in remaining_search_routes(())],
            CoveragePolicy(1, 1, False, False),
        )
        self.assertEqual(decision.status, "routes_exhausted_with_gaps")
        self.assertIsNone(decision.next_step)


class IntakeTests(unittest.TestCase):
    def test_file_hash_and_completeness_gate(self) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            handle.write(b"photo")
            handle.flush()
            artifact = SourceArtifact.from_file("r", handle.name, "image/jpeg")
        self.assertTrue(artifact.artifact_id.startswith("artifact:"))
        observations = [
            OCRObservation("o1", artifact.artifact_id, "line_number", "1", "1", .99),
            OCRObservation("o2", artifact.artifact_id, "line_number", "2", "2", .99),
        ]
        self.assertTrue(check_intake_completeness(("1", "2"), observations).complete)
        self.assertFalse(check_intake_completeness(("1", "2", "3"), observations).complete)
        self.assertTrue(check_intake_completeness(("1", "2"), iter(observations)).complete)


if __name__ == "__main__":
    unittest.main()
