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
    call = StructuredBrowserProvider(
        StaticBrowserExecutor({}),
    ).prepare_supplier_search(_plan())

    with pytest.raises(UnsafeBrowserResult) as blocked:
        await CodexChromeExecutor(executable=executable).execute(call)

    assert blocked.value.access_issue == "login_wall"
    prompt = captured_prompt.read_text()
    assert "Open one fresh tab in the connected Chrome session" in prompt
    assert "not reuse tabs owned by earlier browser sessions" in prompt
    assert "Confirm from its visible UI that an OLX account" in prompt
    assert "access_issue=login_wall" in prompt
    assert "Return at most 1 distinct listing card(s)" in prompt


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
