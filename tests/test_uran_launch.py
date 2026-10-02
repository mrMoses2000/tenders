from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from procurement_bot.approvals import payload_sha256
from scripts.manage_uran_launch import (
    CASE_ID,
    EXPECTED_BASELINE_SHA256,
    RUN_ID,
    LaunchRefusal,
    _preservation_projection,
    _validate_consumed_approval,
    build_parser,
    validate_groups,
    validate_identity,
    validate_retry_recovery,
    validate_worker_limits,
)


def _groups(*, status: str = "paused") -> list[dict[str, object]]:
    return [{"id": uuid4(), "line_number": line, "status": status} for line in range(1, 56)]


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "research_active_groups": 3,
        "browser_cli_concurrency": 1,
        "staged_research_enabled": True,
        "staged_research_primary": "codex",
        "semantic_supplier_reply_enabled": True,
        "research_auto_prepare_outreach": True,
        "waha_enabled": True,
        "research_max_batches": 80,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_launch_target_is_hardcoded_to_exact_case_and_run() -> None:
    validate_identity(CASE_ID, RUN_ID)
    with pytest.raises(LaunchRefusal, match="hardcoded case and run"):
        validate_identity(uuid4(), RUN_ID)
    with pytest.raises(LaunchRefusal, match="hardcoded case and run"):
        validate_identity(CASE_ID, uuid4())


def test_full_case_requires_exact_55_line_partition_and_paused_resume_state() -> None:
    groups = _groups()
    for index, terminal in enumerate(("succeeded", "partial", "failed")):
        groups[index]["status"] = terminal
    assert len(validate_groups(groups, require_paused=True)) == 55

    groups[3]["status"] = "queued"
    with pytest.raises(LaunchRefusal, match="all nonterminal groups must be paused"):
        validate_groups(groups, require_paused=True)

    with pytest.raises(LaunchRefusal, match="exactly 55 distinct"):
        validate_groups(groups[:-1], require_paused=False)


def test_pause_accepts_only_active_or_paused_groups() -> None:
    groups = _groups(status="researching")
    groups[0]["status"] = "succeeded"
    groups[1]["status"] = "partial"
    assert len(validate_groups(groups, require_paused=False)) == 55
    groups[2]["status"] = "failed"
    assert len(validate_groups(groups, require_paused=False)) == 55
    groups[3]["status"] = "cancelled"
    with pytest.raises(LaunchRefusal, match="pause accepts only active, paused, or terminal"):
        validate_groups(groups, require_paused=False)


def test_resume_refuses_active_nonterminal_and_all_terminal_groups() -> None:
    groups = _groups()
    groups[0]["status"] = "partial"
    groups[1]["status"] = "researching"
    with pytest.raises(LaunchRefusal, match="all nonterminal groups must be paused"):
        validate_groups(groups, require_paused=True)

    for group in groups:
        group["status"] = "failed"
    with pytest.raises(LaunchRefusal, match="all 55 groups are terminal"):
        validate_groups(groups, require_paused=True)
    assert len(validate_groups(groups, require_paused=False)) == 55


def test_retry_recovery_requires_partial_run_report_ready_case_and_terminal_groups() -> None:
    groups = _groups(status="partial")
    run = {"status": "partial", "case_status": "report_ready"}
    validate_retry_recovery(run, groups)

    with pytest.raises(LaunchRefusal, match="partial run and report-ready case"):
        validate_retry_recovery({"status": "running", "case_status": "researching"}, groups)
    groups[0]["status"] = "researching"
    with pytest.raises(LaunchRefusal, match="all item groups terminal"):
        validate_retry_recovery(run, groups)


def test_consumed_start_approval_must_match_the_allowlisted_frozen_plan() -> None:
    payload = {
        "case_id": str(CASE_ID),
        "research_run_id": str(RUN_ID),
        "plan_sha256": "a" * 64,
    }
    row = {
        "id": uuid4(),
        "case_id": CASE_ID,
        "action_type": "start_research",
        "payload": payload,
        "payload_sha256": payload_sha256(payload),
        "status": "consumed",
        "approved_by": uuid4(),
        "consumed_at": datetime.now(UTC),
        "consumed_when_live": True,
    }
    assert _validate_consumed_approval(row, {"plan_sha256": "a" * 64}).research_run_id == RUN_ID

    row["payload_sha256"] = "0" * 64
    with pytest.raises(LaunchRefusal, match="does not match this case, run, and frozen plan"):
        _validate_consumed_approval(row, {"plan_sha256": "a" * 64})


def test_worker_config_is_bounded_and_full_pipeline_is_explicit() -> None:
    validate_worker_limits(_settings())
    for override, expected_error in (
        ({"research_active_groups": 4}, "at most 3"),
        ({"browser_cli_concurrency": 2}, "must equal 1"),
        ({"staged_research_enabled": False}, "staged_research_enabled"),
        ({"staged_research_primary": "agy"}, "staged_research_primary"),
        ({"semantic_supplier_reply_enabled": False}, "semantic_supplier_reply_enabled"),
        ({"research_auto_prepare_outreach": False}, "research_auto_prepare_outreach"),
        ({"waha_enabled": False}, "waha_enabled"),
        ({"research_max_batches": 20}, "at least 80"),
    ):
        with pytest.raises(LaunchRefusal, match=expected_error):
            validate_worker_limits(_settings(**override))


def test_resume_preservation_projection_keeps_cursor_results_and_receipts() -> None:
    before = [
        {
            "id": uuid4(),
            "current_batch_no": 4,
            "excluded_external_ids": ["supplier-a"],
            "candidate_receipts": [{"batch_no": 4, "ids": ["supplier-a"]}],
            "search_cursor_state": {"current": "cursor-4", "seen": ["cursor-3"]},
            "source_progress_token": "page-4",
            "semantic_intent_ordinal": 2,
            "candidates_persisted": 1,
            "own_database_candidates": 1,
            "max_batches": 20,
        }
    ]
    after = [{**before[0], "max_batches": 80}]
    assert _preservation_projection(before) == _preservation_projection(after)
    after[0]["search_cursor_state"] = {"current": None, "seen": []}
    assert _preservation_projection(before) != _preservation_projection(after)


def test_parser_defaults_to_read_only_and_exposes_only_pause_or_resume() -> None:
    parser = build_parser()
    assert parser.parse_args([]).mode == "preflight"
    assert parser.parse_args(["--pause"]).mode == "pause"
    assert parser.parse_args(["--resume"]).mode == "resume"
    assert parser.parse_args(["--retry-failed"]).mode == "retry_failed"
    assert EXPECTED_BASELINE_SHA256 == (
        "eb650a33901b338c8229a284ffa63dd07d86c9f913476b0872ac2644f9848003"
    )
