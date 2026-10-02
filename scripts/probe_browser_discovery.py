"""Bounded read-only diagnostic: discover public card URLs, without touching the DB."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from procurement_bot.config import Settings
from procurement_bot.intake import SearchScope
from procurement_bot.providers.browser import StructuredBrowserProvider
from procurement_bot.providers.codex_chrome import CodexChromeExecutor
from procurement_bot.providers.staged_research import prepare_discovery, validate_discovery
from procurement_bot.research import (
    LocalityConstraint,
    ResearchPurpose,
    ResearchSource,
    SourcePlan,
)


async def main(args):
    settings = Settings()
    source = ResearchSource(args.source)
    plan = SourcePlan(
        item_index=0, source=source,
        purpose=(ResearchPurpose.BUSINESS_DISCOVERY if source == ResearchSource.TWO_GIS
                 else ResearchPurpose.PRODUCT_EVIDENCE),
        query_terms=[args.query], rendered_query=args.query,
        locality=LocalityConstraint(
            city=args.city, scope=SearchScope.SPECIFIC_AREA if args.area else SearchScope.CITYWIDE,
            raw_area_text=args.area,
        ),
        minimum_unique_candidates=20, result_ttl_hours=24, empty_page_limit=1,
    )
    executor = CodexChromeExecutor(
        executable=settings.codex_2gis_executable,
        model=settings.codex_2gis_model,
        reasoning_effort=settings.codex_2gis_reasoning_effort,
        timeout_seconds=args.timeout, batch_timeout_seconds=args.timeout,
        resource_wait_seconds=30,
        retain_failure_trace=settings.browser_failure_trace_enabled,
        diagnostics_root=Path(args.diagnostics),
    )
    if args.stage == "discover_page":
        call = prepare_discovery(plan)
        result = validate_discovery(call, plan, await executor.execute(call))
    else:
        plan = plan.model_copy(update={"batch_candidate_limit": 1})
        validator = StructuredBrowserProvider(executor)
        payload = plan.browser_payload()
        payload["execution_stage"] = args.stage
        request = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        call = validator.prepare_supplier_search(plan).model_copy(update={
            "untrusted_request_json": request,
            "request_sha256": hashlib.sha256(request.encode()).hexdigest(),
            "trusted_instruction": "Read only this exact card URL, one candidate, no messages.",
        })
        result = validator.accept_supplier_search(call, plan, await executor.execute(call))
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["2gis", "olx"], required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--city", required=True)
    parser.add_argument("--area")
    parser.add_argument("--stage", default="discover_page",
                        choices=("discover_page", "read_card", "read_contacts"))
    parser.add_argument("--timeout", type=int, default=180, choices=range(30, 301))
    parser.add_argument("--diagnostics", default="./var/discovery-probe-diagnostics")
    asyncio.run(main(parser.parse_args()))
