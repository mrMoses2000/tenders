import json
import shlex
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

    async def fake_invoke(prepared, *, source, direct_firm_card):
        seen.append((prepared, source, direct_firm_card))
        return {"spec_sha256": prepared.spec_sha256}

    monkeypatch.setattr(executor, "_invoke", fake_invoke)
    result = await executor.execute(call)
    assert result == {"spec_sha256": call.spec_sha256}
    assert seen == [(call, "olx", False)]


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

    result = await CodexChromeExecutor(executable=executable).execute(call)

    assert result["access_issue"] == "login_wall"
    prompt = captured_prompt.read_text()
    assert "Open one fresh tab in the connected Chrome session" in prompt
    assert "not reuse tabs owned by earlier browser sessions" in prompt
    assert "Confirm from its visible UI that an OLX account" in prompt
    assert "access_issue=login_wall" in prompt


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
