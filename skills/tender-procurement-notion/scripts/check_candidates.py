#!/usr/bin/env python3
"""Check tender supplier candidates from CSV or JSON.

The script is intentionally small and dependency-free. It helps an agent avoid
routine mistakes: price over ceiling, insufficient quantity, stale check dates,
and accidental "ready" status without required evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any


READY_STATUSES = {
    "exact",
    "ready",
    "можно",
    "можно заказывать",
    "✅ совпадает",
    "✅ exact match verified",
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("candidates", [])
        if not isinstance(data, list):
            raise SystemExit("JSON must be a list or an object with candidates list")
        return [dict(row) for row in data]

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("₸", "").replace("тг", "").replace(",", ".")
    cleaned = "".join(ch for ch in cleaned if ch.isdigit() or ch in ".-")
    if cleaned in {"", ".", "-"}:
        return None
    return float(cleaned)


def is_stale(value: Any, today: date, max_age_days: int) -> bool:
    if not value:
        return True
    text = str(value)[:10]
    try:
        checked = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return True
    return (today - checked).days > max_age_days


def status_of(row: dict[str, Any]) -> str:
    return str(row.get("status") or row.get("match_status") or row.get("статус") or "").strip().lower()


def get(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def check_row(row: dict[str, Any], args: argparse.Namespace, today: date) -> dict[str, Any]:
    unit_price = number(get(row, "unit_price", "price", "цена", "цена_за_единицу"))
    quantity = number(get(row, "available_qty", "quantity_available", "наличие", "доступно"))
    checked_at = get(row, "checked_at", "date", "дата_проверки")
    status = status_of(row)

    issues: list[str] = []

    if unit_price is None:
        issues.append("missing unit price")
    elif args.price_limit is not None and unit_price > args.price_limit:
        issues.append(f"price over limit: {unit_price:g} > {args.price_limit:g}")

    if quantity is None:
        issues.append("missing available quantity")
    elif args.required_qty is not None and quantity < args.required_qty:
        issues.append(f"insufficient quantity: {quantity:g} < {args.required_qty:g}")

    if args.max_age_days is not None and is_stale(checked_at, today, args.max_age_days):
        issues.append("stale or missing checked_at")

    if status in READY_STATUSES:
        for field in ("source_url", "supplier", "delivery", "unit"):
            if not get(row, field, field.capitalize(), field.upper()):
                issues.append(f"ready status missing {field}")

    return {
        "supplier": get(row, "supplier", "поставщик", "company") or "",
        "product": get(row, "product", "товар", "title") or "",
        "unit_price": unit_price,
        "available_qty": quantity,
        "status": status,
        "ok": not issues,
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="CSV or JSON candidate file")
    parser.add_argument("--price-limit", type=float)
    parser.add_argument("--required-qty", type=float)
    parser.add_argument("--max-age-days", type=int, default=2)
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    args = parser.parse_args()

    rows = load_rows(args.input)
    today = date.today()
    checked = [check_row(row, args, today) for row in rows]

    if args.format == "json":
        print(json.dumps({"checked": checked}, ensure_ascii=False, indent=2))
        return 0 if all(row["ok"] for row in checked) else 1

    print("| supplier | product | unit_price | qty | status | result |")
    print("|---|---|---:|---:|---|---|")
    for row in checked:
        result = "OK" if row["ok"] else "; ".join(row["issues"])
        print(
            f"| {row['supplier']} | {row['product']} | "
            f"{row['unit_price'] if row['unit_price'] is not None else ''} | "
            f"{row['available_qty'] if row['available_qty'] is not None else ''} | "
            f"{row['status']} | {result} |"
        )
    return 0 if all(row["ok"] for row in checked) else 1


if __name__ == "__main__":
    raise SystemExit(main())
