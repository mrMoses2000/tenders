from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
from decimal import Decimal
from pathlib import Path

from .economics import allocate_delivery_cost, calculate_landed_cost
from .adapters import plan_source_escalation
from .extractor import extract_public_product, parse_product_html
from .logistics import ProcurementScenario, compare_procurement_scenarios
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
    else:
        data = json.loads(args.path.read_text(encoding="utf-8"))
        items = [MarketItemHypothesis(**item) for item in data["items"]]
        output = evaluate_market_trip(
            market_name=data["market_name"], city=data["city"], trip_cost=data["trip_cost"],
            minimum_net_savings=data.get("minimum_net_savings", 0), items=items,
        )
    print(json.dumps(_json_ready(output), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
