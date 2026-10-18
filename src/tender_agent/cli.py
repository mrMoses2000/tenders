from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .adapters import plan_source_escalation
from .economics import allocate_delivery_cost, calculate_landed_cost
from .extractor import extract_public_product, parse_product_html
from .logistics import ProcurementScenario, compare_procurement_scenarios
from .notion_sync import plan_notion_sync
from .persistence import StateStore
from .reports import render_operator_markdown
from .snapshot import snapshot_from_dict
from .sourcing import CoveragePolicy, SourcingCandidate, evaluate_coverage
from .trips import MarketItemHypothesis, evaluate_market_trip


def _json_ready(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_ready(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tender-agent", description="Deterministic tender helpers")
    commands = parser.add_subparsers(dest="command", required=True)

    extract_file = commands.add_parser("extract-file", help="Extract a local HTML fixture")
    extract_file.add_argument("path", type=Path)
    extract_file.add_argument("--source-url", default="")

    extract_url = commands.add_parser("extract-url", help="Fetch and extract a public HTML page")
    extract_url.add_argument("url")
    extract_url.add_argument("--timeout", type=float, default=10)
    extract_url.add_argument("--max-bytes", type=int, default=2_000_000)
    extract_url.add_argument("--retries", type=int, default=1)

    landed = commands.add_parser("landed-cost", help="Calculate forecast economics")
    landed.add_argument("--tender-price", required=True)
    landed.add_argument("--quantity", required=True)
    landed.add_argument("--offer-price")
    landed.add_argument("--logistics", default="0")
    landed.add_argument("--extra", default="0")
    landed.add_argument("--minimum-order")
    landed.add_argument("--order-multiple")

    allocate = commands.add_parser("allocate-delivery", help="Allocate delivery by weights")
    allocate.add_argument("--total", required=True)
    allocate.add_argument("weights", nargs="+", help="item=weight")

    source_plan = commands.add_parser("source-plan", help="Explain extractor/browser/contact routing")
    source_plan.add_argument("url")
    source_plan.add_argument("--html", type=Path, help="Optional already-downloaded HTML fixture")
    source_plan.add_argument("--required", nargs="+", default=["product_name", "price"])

    compare = commands.add_parser("compare-scenarios", help="Rank local/intercity scenarios from JSON")
    compare.add_argument("path", type=Path)

    trip = commands.add_parser("trip-economics", help="Evaluate an offline market trip from JSON")
    trip.add_argument("path", type=Path)

    snapshot_validate = commands.add_parser(
        "snapshot-validate", help="Validate a canonical procurement snapshot"
    )
    snapshot_validate.add_argument("path", type=Path)

    reconcile = commands.add_parser(
        "reconcile", help="Reconcile line definitions and facts into a canonical snapshot"
    )
    reconcile.add_argument("path", type=Path)

    report = commands.add_parser("report", help="Render an operator report from a snapshot")
    report.add_argument("path", type=Path)
    report.add_argument("--format", choices=("md", "json"), default="md")

    store_init = commands.add_parser("store-init", help="Initialize or validate the SQLite state store")
    store_init.add_argument("path", type=Path)

    jobs = commands.add_parser("job-status", help="Show durable job counts")
    jobs.add_argument("path", type=Path)

    notion_plan = commands.add_parser("notion-plan", help="Create a no-write Notion property diff")
    notion_plan.add_argument("current", type=Path)
    notion_plan.add_argument("desired", type=Path)

    coverage = commands.add_parser("coverage", help="Evaluate sourcing quality coverage")
    coverage.add_argument("path", type=Path)
    coverage.add_argument("--minimum-leads", type=int, default=5)
    coverage.add_argument("--minimum-actionable", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "extract-file":
        result = parse_product_html(args.path.read_text(encoding="utf-8"), args.source_url)
        output = result.as_dict()
    elif args.command == "extract-url":
        output = extract_public_product(
            args.url, timeout=args.timeout, max_bytes=args.max_bytes, retries=args.retries
        ).as_dict()
    elif args.command == "landed-cost":
        result = calculate_landed_cost(
            tender_unit_price=Decimal(args.tender_price),
            required_quantity=Decimal(args.quantity),
            offer_unit_price=Decimal(args.offer_price) if args.offer_price is not None else None,
            logistics=Decimal(args.logistics),
            extra_cost=Decimal(args.extra),
            minimum_order=Decimal(args.minimum_order) if args.minimum_order is not None else None,
            order_multiple=Decimal(args.order_multiple) if args.order_multiple is not None else None,
        )
        output = result.as_dict()
    elif args.command == "allocate-delivery":
        weights: dict[str, Decimal] = {}
        for value in args.weights:
            if "=" not in value:
                raise SystemExit(f"Invalid weight '{value}'; expected item=weight")
            key, weight = value.split("=", 1)
            weights[key] = Decimal(weight)
        output = {key: str(value) for key, value in allocate_delivery_cost(args.total, weights).items()}
    elif args.command == "source-plan":
        extracted = None
        if args.html:
            extracted = parse_product_html(args.html.read_text(encoding="utf-8"), args.url)
        output = plan_source_escalation(args.url, extracted, required_fields=tuple(args.required))
    elif args.command == "compare-scenarios":
        data = json.loads(args.path.read_text(encoding="utf-8"))
        output = compare_procurement_scenarios(ProcurementScenario(**item) for item in data)
    elif args.command == "trip-economics":
        data = json.loads(args.path.read_text(encoding="utf-8"))
        items = [MarketItemHypothesis(**item) for item in data["items"]]
        output = evaluate_market_trip(
            market_name=data["market_name"], city=data["city"], trip_cost=data["trip_cost"],
            minimum_net_savings=data.get("minimum_net_savings", 0), items=items,
        )
    elif args.command in {"snapshot-validate", "reconcile", "report"}:
        data = json.loads(args.path.read_text(encoding="utf-8"))
        snapshot = snapshot_from_dict(data)
        if args.command == "snapshot-validate":
            output = {
                "valid": True, "request_id": snapshot.request_id,
                "lines": len(snapshot.lines), "facts": len(snapshot.facts),
                "checksum": snapshot.checksum,
            }
        elif args.command == "reconcile":
            output = snapshot.as_dict()
        elif args.format == "md":
            print(render_operator_markdown(snapshot), end="")
            return 0
        else:
            output = {
                "request_id": snapshot.request_id,
                "checksum": snapshot.checksum,
                "states": snapshot.states,
            }
    elif args.command in {"store-init", "job-status"}:
        with StateStore(args.path) as store:
            store.migrate()
            output = {
                "database": str(args.path.resolve()), "schema_version": 1,
                "jobs": store.job_counts(),
            }
    elif args.command == "notion-plan":
        current = json.loads(args.current.read_text(encoding="utf-8"))
        desired = json.loads(args.desired.read_text(encoding="utf-8"))
        output = plan_notion_sync(current, desired)
    else:
        data = json.loads(args.path.read_text(encoding="utf-8"))
        candidates = [
            SourcingCandidate(
                **{**item, "checked_at": datetime.fromisoformat(item["checked_at"])}
            )
            for item in data
        ]
        output = evaluate_coverage(
            candidates,
            CoveragePolicy(
                minimum_leads=args.minimum_leads,
                minimum_actionable=args.minimum_actionable,
            ),
        )
    print(json.dumps(_json_ready(output), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
