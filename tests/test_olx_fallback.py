import asyncio
import json
import shlex
from datetime import UTC, datetime
from pathlib import Path

import pytest

from procurement_bot.errors import PermanentProviderError, RetryableProviderError
from procurement_bot.intake import SearchScope
from procurement_bot.providers.browser import (
    StaticBrowserExecutor,
    StructuredBrowserProvider,
    UnsafeBrowserResult,
)
from procurement_bot.providers.codex_chrome import CodexChromeExecutor
from procurement_bot.providers.olx_fallback import OlxFallbackProvider
from procurement_bot.research import (
    LocalityConstraint,
    LocalityResearchTask,
    ResearchPurpose,
    ResearchSource,
    SourcePlan,
)


def _plan(source: ResearchSource = ResearchSource.OLX) -> SourcePlan:
    return SourcePlan(
        item_index=0,
        source=source,
        purpose=ResearchPurpose.PRODUCT_EVIDENCE,
        query_terms=["аппарат для сварки пластиковых труб 2000 Вт"],
        rendered_query="аппарат для сварки пластиковых труб 2000 Вт Алматы",
        locality=LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=1,
        result_ttl_hours=1,
        empty_page_limit=1,
    )


def _olx_lead(listing_id: str) -> dict:
    return {
        "source": "olx",
        "external_id": listing_id,
        "name": f"Продавец {listing_id}",
        "evidence": [{
            "evidence_type": "product_page",
            "source": "olx",
            "source_url": (
                f"https://www.olx.kz/d/obyavlenie/apparat-ID{listing_id}.html"
            ),
            "observed_at": datetime.now(UTC).isoformat(),
            "raw_snapshot_sha256": "a" * 64,
            "claims": ["product_match"],
        }],
    }


def _olx_result(call, ids: list[str], *, exhausted: bool = False) -> dict:
    return {
        "spec_sha256": call.spec_sha256,
        "request_sha256": call.request_sha256,
        "candidates": [_olx_lead(listing_id) for listing_id in ids],
        "exhausted": exhausted,
        "pages_examined": 1,
    }


@pytest.mark.asyncio
async def test_olx_timeout_uses_codex_fallback() -> None:
    class _Primary:
        async def search_suppliers(self, _plan):
            raise RetryableProviderError("AGY browser CLI timeout")

    class _Fallback:
        def __init__(self):
            self.calls = 0

        async def search_suppliers(self, _plan):
            self.calls += 1
            return "fallback-result"

    fallback = _Fallback()
    provider = OlxFallbackProvider(_Primary(), fallback)  # type: ignore[arg-type]
    assert await provider.search_suppliers(_plan()) == "fallback-result"
    assert fallback.calls == 1
    with pytest.raises(RetryableProviderError, match="timeout"):
        await provider.search_suppliers(_plan(ResearchSource.WEB))
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_olx_access_denial_uses_fallback_but_other_errors_do_not() -> None:
    class _Primary:
        def __init__(self, issue):
            self.issue = issue

        async def search_suppliers(self, _plan):
            raise UnsafeBrowserResult("blocked", access_issue=self.issue)

    class _Fallback:
        async def search_suppliers(self, _plan):
            return "fallback-result"

    assert await OlxFallbackProvider(  # type: ignore[arg-type]
        _Primary("login_wall"), _Fallback(),
    ).search_suppliers(_plan()) == "fallback-result"
    with pytest.raises(UnsafeBrowserResult):
        await OlxFallbackProvider(  # type: ignore[arg-type]
            _Primary(None), _Fallback(),
        ).search_suppliers(_plan())


@pytest.mark.asyncio
async def test_olx_malformed_agy_output_uses_fallback() -> None:
    class _Primary:
        async def search_suppliers(self, _plan):
            raise PermanentProviderError(
                "AGY browser CLI returned an invalid structured output"
            )

    class _Fallback:
        async def search_suppliers(self, _plan):
            return "fallback-result"

    assert await OlxFallbackProvider(  # type: ignore[arg-type]
        _Primary(), _Fallback(),
    ).search_suppliers(_plan()) == "fallback-result"


@pytest.mark.asyncio
async def test_codex_executor_accepts_only_pinned_olx_search(monkeypatch) -> None:
    call = StructuredBrowserProvider(
        StaticBrowserExecutor({}),
    ).prepare_supplier_search(_plan())
    executor = CodexChromeExecutor(executable=Path("/bin/true"))
    seen = []

    async def fake_invoke(prepared, *, source, direct_firm_card, **options):
        seen.append((prepared, source, direct_firm_card, options))
        return _olx_result(prepared, ["1"], exhausted=True)

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    result = await executor.execute(call)
    assert result["spec_sha256"] == call.spec_sha256
    assert len(result["candidates"]) == 1
    assert len(seen) == 1
    assert seen[0][:3] == (call, "olx", False)
    assert seen[0][3]["max_leads"] == 1
    assert seen[0][3]["excluded_listing_ids"] == ()
    assert not seen[0][3]["repair_contact_evidence"]
    assert 0 < seen[0][3]["timeout_seconds"] <= 900


@pytest.mark.asyncio
async def test_codex_olx_collects_small_validated_batches(monkeypatch) -> None:
    plan = _plan().model_copy(update={"minimum_unique_candidates": 5})
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    executor = CodexChromeExecutor(executable=Path("/bin/true"))
    seen = []

    async def fake_invoke(prepared, *, source, direct_firm_card, **options):
        seen.append(options)
        ids = ["1", "2", "3"] if len(seen) == 1 else ["4", "5"]
        return _olx_result(prepared, ids)

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    raw = await executor.execute(call)
    result = StructuredBrowserProvider(StaticBrowserExecutor({})).accept_supplier_search(
        call, plan, raw,
    )

    assert [candidate.external_id for candidate in result.candidates] == [
        "1", "2", "3", "4", "5",
    ]
    assert result.pages_examined == 2
    assert not result.exhausted
    assert [(entry["max_leads"], entry["excluded_listing_ids"]) for entry in seen] == [
        (3, ()), (2, ("1", "2", "3")),
    ]
    assert all(0 < entry["timeout_seconds"] <= 900 for entry in seen)
    assert all(not entry["repair_contact_evidence"] for entry in seen)


@pytest.mark.asyncio
async def test_codex_olx_continues_after_underfilled_batch(monkeypatch) -> None:
    plan = _plan().model_copy(update={"minimum_unique_candidates": 5})
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    executor = CodexChromeExecutor(executable=Path("/bin/true"))
    batches = [["1"], ["2"], ["3", "4", "5"]]

    async def fake_invoke(prepared, *, source, direct_firm_card, **options):
        return _olx_result(prepared, batches.pop(0))

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    raw = await executor.execute(call)

    assert [item["external_id"] for item in raw["candidates"]] == [
        "1", "2", "3", "4", "5",
    ]
    assert raw["pages_examined"] == 3
    assert raw["access_issue"] is None


@pytest.mark.asyncio
async def test_codex_olx_rejects_batch_over_lead_cap(monkeypatch) -> None:
    plan = _plan().model_copy(update={"minimum_unique_candidates": 5})
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    executor = CodexChromeExecutor(executable=Path("/bin/true"))

    async def fake_invoke(prepared, *, source, direct_firm_card, **options):
        return _olx_result(prepared, ["1", "2", "3", "4"])

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    with pytest.raises(UnsafeBrowserResult, match="exceeded lead cap"):
        await executor.execute(call)


@pytest.mark.asyncio
async def test_codex_olx_retries_contact_without_listing_claim_once(monkeypatch) -> None:
    plan = _plan()
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    executor = CodexChromeExecutor(executable=Path("/bin/true"))
    attempts = []

    async def fake_invoke(prepared, *, source, direct_firm_card, **options):
        attempts.append(options)
        raw = _olx_result(prepared, ["1"], exhausted=True)
        raw["candidates"][0]["contacts"] = [{
            "kind": "phone", "value": "+77000000000", "confidence": 1,
        }]
        if options["repair_contact_evidence"]:
            raw["candidates"][0]["evidence"][0]["claims"].append("contact")
        return raw

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    raw = await executor.execute(call)
    result = StructuredBrowserProvider(StaticBrowserExecutor({})).accept_supplier_search(
        call, plan, raw,
    )

    assert len(result.candidates[0].contacts) == 1
    assert [attempt["repair_contact_evidence"] for attempt in attempts] == [False, True]


@pytest.mark.asyncio
async def test_codex_olx_preserves_valid_first_batch_after_later_timeout(monkeypatch) -> None:
    plan = _plan().model_copy(update={"minimum_unique_candidates": 5})
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    executor = CodexChromeExecutor(executable=Path("/bin/true"))
    calls = 0

    async def fake_invoke(prepared, *, source, direct_firm_card, **options):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RetryableProviderError("Codex Chrome fallback timed out")
        return _olx_result(prepared, ["1", "2", "3"])

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    raw = await executor.execute(call)
    result = StructuredBrowserProvider(StaticBrowserExecutor({})).accept_supplier_search(
        call, plan, raw,
    )

    assert calls == 2
    assert len(result.candidates) == 3
    assert result.pages_examined == 1
    assert result.access_issue == "unknown"
    assert not result.exhausted


@pytest.mark.asyncio
async def test_coordinator_olx_plan_returns_one_small_excluded_batch(monkeypatch) -> None:
    plan = _plan().model_copy(update={
        "minimum_unique_candidates": 5,
        "batch_candidate_limit": 2,
        "excluded_external_ids": ["prior-1"],
    })
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    executor = CodexChromeExecutor(executable=Path("/bin/true"))
    seen = []

    async def fake_invoke(prepared, *, source, direct_firm_card, **options):
        seen.append(options)
        return _olx_result(prepared, ["1", "2"])

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    raw = await executor.execute(call)

    assert len(raw["candidates"]) == 2
    assert len(seen) == 1
    assert seen[0]["max_leads"] == 2
    assert seen[0]["excluded_listing_ids"] == ("prior-1",)
    assert 0 < seen[0]["timeout_seconds"] <= 180


@pytest.mark.asyncio
async def test_chrome_budget_keeps_one_local_browser_task_at_a_time(monkeypatch) -> None:
    plan = _plan()
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    shared_budget = asyncio.Semaphore(2)
    executor = CodexChromeExecutor(
        executable=Path("/bin/true"), resource_budget=shared_budget,
    )
    active = 0
    maximum = 0

    async def fake_invoke(prepared, *, source, direct_firm_card, **_options):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _olx_result(prepared, ["one"])

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    await asyncio.gather(*(executor.execute(call) for _ in range(3)))

    assert maximum == 1


@pytest.mark.asyncio
async def test_chrome_timeout_writes_safe_receipt_with_progress_counts(tmp_path) -> None:
    executable = tmp_path / "slow-codex"
    executable.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        "echo 'mcp: node_repl/js (completed)' >&2\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    diagnostics = tmp_path / "diagnostics"
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(_plan())
    executor = CodexChromeExecutor(
        executable=executable,
        diagnostics_root=diagnostics,
        batch_timeout_seconds=2,
    )

    with pytest.raises(RetryableProviderError, match="timed out"):
        await executor.execute(call)

    receipts = [json.loads(path.read_text()) for path in diagnostics.glob("*.json")]
    assert any(
        receipt["status"] == "timed_out" and receipt["browser_call_count"] == 1
        for receipt in receipts
    )
    assert all("mcp: node_repl" not in path.read_text() for path in diagnostics.glob("*.json"))


@pytest.mark.asyncio
async def test_chrome_lock_serializes_executor_instances_and_releases_on_cancel(tmp_path) -> None:
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_locality(
        LocalityResearchTask(city="Алматы", phrase="Северное кольцо")
    )
    shared_budget = asyncio.Semaphore(2)
    lock_path = tmp_path / "chrome-owner.lock"
    first = CodexChromeExecutor(
        executable=Path("/bin/true"),
        resource_budget=shared_budget,
        chrome_lock_path=lock_path,
    )
    second = CodexChromeExecutor(
        executable=Path("/bin/true"),
        resource_budget=shared_budget,
        chrome_lock_path=lock_path,
    )
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    hold_first = asyncio.Event()

    async def first_invoke(prepared, *, source, direct_firm_card):
        first_started.set()
        await hold_first.wait()
        return {
            "spec_sha256": prepared.spec_sha256,
            "request_sha256": prepared.request_sha256,
            "candidates": [],
            "pages_examined": 1,
        }

    async def second_invoke(prepared, *, source, direct_firm_card):
        second_started.set()
        return {
            "spec_sha256": prepared.spec_sha256,
            "request_sha256": prepared.request_sha256,
            "candidates": [],
            "pages_examined": 1,
        }

    first._invoke = first_invoke  # type: ignore[method-assign]
    second._invoke = second_invoke  # type: ignore[method-assign]
    first_task = asyncio.create_task(first.execute(call))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second_task = asyncio.create_task(second.execute(call))
    await asyncio.sleep(0.15)
    assert not second_started.is_set()

    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    await asyncio.wait_for(second_started.wait(), timeout=1)
    await second_task

    assert lock_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_codex_executor_opens_requested_2gis_firm_card(monkeypatch) -> None:
    plan = SourcePlan(
        item_index=0,
        source=ResearchSource.TWO_GIS,
        purpose=ResearchPurpose.BUSINESS_DISCOVERY,
        query_terms=["потолочные панели"],
        rendered_query="https://2gis.kz/almaty/firm/1234567890",
        locality=LocalityConstraint(city="Алматы", scope=SearchScope.CITYWIDE),
        minimum_unique_candidates=1,
        result_ttl_hours=24,
        empty_page_limit=1,
    )
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    executor = CodexChromeExecutor(executable=Path("/bin/true"))
    seen = []

    async def fake_invoke(prepared, *, source, direct_firm_card):
        seen.append((prepared, source, direct_firm_card))
        return {}

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    await executor.execute(call)
    assert seen == [(call, "2gis", True)]


@pytest.mark.asyncio
async def test_codex_executor_accepts_web_supplier_fallback(monkeypatch) -> None:
    plan = _plan(ResearchSource.WEB)
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(plan)
    executor = CodexChromeExecutor(executable=Path("/bin/true"))
    seen = []

    async def invoke(prepared, *, source, direct_firm_card, **_options):
        seen.append((source, direct_firm_card))
        return {
            "spec_sha256": prepared.spec_sha256,
            "request_sha256": prepared.request_sha256,
            "candidates": [],
            "exhausted": True,
            "pages_examined": 1,
        }

    monkeypatch.setattr(executor, "_invoke", invoke)
    result = await executor.execute(call)

    assert result["pages_examined"] == 1
    assert seen == [("web", False)]


@pytest.mark.asyncio
async def test_codex_executor_accepts_valid_result_after_nonzero_exit(tmp_path) -> None:
    envelope = json.dumps({"result_json": json.dumps({
        "candidates": [], "exhausted": True, "pages_examined": 1,
        "access_issue": None,
    })})
    executable = tmp_path / "codex-stub"
    executable.write_text(
        "#!/bin/sh\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = -o ]; then\n"
        "    shift\n"
        f"    printf '%s\\n' {shlex.quote(envelope)} > \"$1\"\n"
        "    exit 127\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "exit 127\n",
    )
    executable.chmod(0o700)
    provider = StructuredBrowserProvider(CodexChromeExecutor(executable=executable))
    result = await provider.search_suppliers(_plan())
    assert result.pages_examined == 1
    assert result.candidates == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("log_message", "expected_code"),
    [
        ("Usage limit reached for this account", "cli_usage_limit"),
        ("Rate limit exceeded: too many requests", "cli_rate_limit"),
        ("Authentication required: not logged in", "cli_auth_failed"),
        ("Permission denied by policy", "cli_permission_denied"),
        ("MCP server disconnected; transport closed", "mcp_disconnected"),
        ("Unexpected process failure", "cli_exit_nonzero"),
    ],
)
async def test_codex_nonzero_exit_receipt_keeps_only_safe_failure_code(
    tmp_path, log_message: str, expected_code: str,
) -> None:
    executable = tmp_path / "codex-exit"
    executable.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        f"printf '%s\\n' {shlex.quote(log_message)} >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    diagnostics = tmp_path / "diagnostics"
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(
        _plan()
    )
    executor = CodexChromeExecutor(
        executable=executable,
        diagnostics_root=diagnostics,
    )

    with pytest.raises(RetryableProviderError):
        await executor.execute(call)

    receipts = [json.loads(path.read_text()) for path in diagnostics.glob("*.json")]
    assert len(receipts) == 2  # outer and inner receipts for one invocation
    assert all(receipt["status"] == "failed" for receipt in receipts)
    assert all(receipt["error"] == expected_code for receipt in receipts)
    assert all(receipt["last_stage"] == "cli_exit" for receipt in receipts)
    assert all(log_message not in json.dumps(receipt) for receipt in receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_text", "expected_underlying"),
    [
        (
            "Timed out after 10000ms waiting for CDP command "
            "Input.synthesizeScrollGesture.",
            "browser_scroll_timeout",
        ),
        ("MCP tool returned an opaque error", None),
    ],
)
async def test_codex_timeout_receipt_classifies_only_literal_failed_tool_cause(
    tmp_path, failed_text: str, expected_underlying: str | None,
) -> None:
    failed_event = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "status": "failed",
            "result": {"content": [{"type": "text", "text": failed_text}]},
        },
    })
    executable = tmp_path / "codex-timeout"
    executable.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        f"printf '%s\\n' {shlex.quote(failed_event)} >&2\n"
        "sleep 3\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    diagnostics = tmp_path / "diagnostics"
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(
        _plan(ResearchSource.WEB)
    )
    executor = CodexChromeExecutor(
        executable=executable,
        batch_timeout_seconds=1,
        diagnostics_root=diagnostics,
    )

    with pytest.raises(RetryableProviderError, match="timed out"):
        await executor.execute(call)

    receipts = [json.loads(path.read_text()) for path in diagnostics.glob("*.json")]
    assert len(receipts) == 2
    assert all(receipt["error"] == "cli_timeout" for receipt in receipts)
    assert all(receipt["underlying_error_code"] == expected_underlying for receipt in receipts)
    assert all(failed_text not in json.dumps(receipt) for receipt in receipts)


@pytest.mark.parametrize(
    ("failed_text", "expected"),
    [
        ("Timed out waiting for CDP command Input.click", "browser_tool_timeout"),
        ("Permission denied by browser policy", "permission_denied"),
        ("Usage limit reached for this account", "usage_limit"),
        ("No known cause marker", None),
    ],
)
def test_classifies_only_allowlisted_failed_tool_markers(
    tmp_path, failed_text: str, expected: str | None,
) -> None:
    event = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "status": "failed",
            "result": {"content": [{"type": "text", "text": failed_text}]},
        },
    }
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps(event) + "\n", encoding="utf-8")

    assert CodexChromeExecutor._classify_failed_tool_cause(trace) == expected


@pytest.mark.asyncio
async def test_codex_olx_prompt_requires_visible_signed_in_tab(tmp_path) -> None:
    envelope = json.dumps({"result_json": json.dumps({
        "candidates": [], "exhausted": False, "pages_examined": 0,
        "access_issue": "login_wall",
    })})
    captured_prompt = tmp_path / "prompt.txt"
    executable = tmp_path / "codex-stub"
    executable.write_text(
        "#!/bin/sh\n"
        f"/bin/cat > {shlex.quote(str(captured_prompt))}\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = -o ]; then\n"
        "    shift\n"
        f"    printf '%s\\n' {shlex.quote(envelope)} > \"$1\"\n"
        "    exit 0\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "exit 127\n",
    )
    executable.chmod(0o700)
    plan = _plan().model_copy(update={
        "query_terms": ["Олифа 1900762"],
        "rendered_query": "Олифа 1900762 Алматы Северное Кольцо",
        "locality": LocalityConstraint(
            city="Алматы", scope=SearchScope.SPECIFIC_AREA,
            raw_area_text="Северное Кольцо",
        ),
    })
    call = StructuredBrowserProvider(
        StaticBrowserExecutor({}),
    ).prepare_supplier_search(plan)

    with pytest.raises(UnsafeBrowserResult) as blocked:
        await CodexChromeExecutor(executable=executable).execute(call)

    assert blocked.value.access_issue == "login_wall"
    prompt = captured_prompt.read_text()
    assert "Open one fresh tab in the connected Chrome session" in prompt
    assert "not reuse tabs owned by earlier browser sessions" in prompt
    assert "Confirm from its visible UI that an OLX account" in prompt
    assert "access_issue=login_wall" in prompt
    assert "Return at most 1 distinct listing card(s)" in prompt
    assert "retry once with the generic product noun or category and the same city" in prompt
    assert "Treat the requested area as a proximity preference only" in prompt
    assert "Keep the original technical requirements as suitability gates" in prompt


@pytest.mark.asyncio
async def test_codex_web_prompt_searches_terms_without_url_queries(tmp_path) -> None:
    envelope = json.dumps({"result_json": json.dumps({
        "candidates": [], "exhausted": False, "pages_examined": 1,
    })})
    captured_prompt = tmp_path / "prompt.txt"
    executable = tmp_path / "codex-stub"
    executable.write_text(
        "#!/bin/sh\n"
        f"/bin/cat > {shlex.quote(str(captured_prompt))}\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = -o ]; then\n"
        "    shift\n"
        f"    printf '%s\\n' {shlex.quote(envelope)} > \"$1\"\n"
        "    exit 0\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "exit 127\n",
    )
    executable.chmod(0o700)
    plan = _plan(ResearchSource.WEB).model_copy(update={
        "query_terms": ["Кисточка 80 мм"],
        "rendered_query": "Кисточка 80 мм Алматы Северное Кольцо",
        "locality": LocalityConstraint(
            city="Алматы", scope=SearchScope.SPECIFIC_AREA,
            raw_area_text="Северное Кольцо",
        ),
    })
    call = StructuredBrowserProvider(StaticBrowserExecutor({})).prepare_supplier_search(
        plan
    )

    result = await CodexChromeExecutor(executable=executable).execute(call)

    assert result["pages_examined"] == 1
    prompt = captured_prompt.read_text()
    assert "Use the supplied product terms and requested city to search the public web" in prompt
    assert "Open relevant public HTTPS vendor pages" in prompt
    assert "use the requested area as context, not an exact street/highway requirement" in prompt
    assert "Do not return a search-results page as product evidence" in prompt
    assert (
        "Open only the HTTPS website or public Instagram URLs present in query_terms"
        not in prompt
    )


def test_codex_executor_downgrades_unbacked_stock_claim() -> None:
    result = {"candidates": [{
        "product_availability": "publicly_advertised",
        "evidence": [{
            "availability": "publicly_advertised",
            "claims": ["product_match", "contact"],
        }],
    }]}
    CodexChromeExecutor._downgrade_unbacked_availability(result)
    candidate = result["candidates"][0]
    assert candidate["product_availability"] == "unknown"
    assert candidate["evidence"][0]["availability"] == "unknown"
