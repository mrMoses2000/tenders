#!/usr/bin/env python3
"""Merge agent shortlists, apply tender deduplication, and emit final 30-row CSVs."""

from __future__ import annotations

import csv
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
COLUMNS = [
    "position",
    "priority",
    "supplier_name",
    "supplier_type",
    "address",
    "phone",
    "whatsapp",
    "whatsapp_verified",
    "twogis_url",
    "website",
    "instagram",
    "business_hours",
    "relevance_evidence",
    "technical_status",
    "question_to_supplier",
    "notes",
]
FIRM_ID = re.compile(r"/(?:firm|branches)/(\d+)")


def load(paths: list[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows.extend({column: (row.get(column) or "").strip() for column in COLUMNS} for row in reader)
    return rows


def phone_key(value: str) -> str:
    primary = (value or "").split(";")[0]
    digits = re.sub(r"\D", "", primary)
    return ("7" + digits[1:]) if len(digits) == 11 and digits.startswith("8") else digits


def firm_key(url: str) -> str:
    match = FIRM_ID.search(url or "")
    return match.group(1) if match else ""


def text_key(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", (value or "").casefold())


def dedupe(rows: list[dict[str, str]], label: str) -> tuple[list[dict[str, str]], list[str]]:
    priority = {"A": 0, "B": 1, "C": 2, "D": 3}
    rows = sorted(enumerate(rows), key=lambda item: (priority.get(item[1]["priority"], 9), item[0]))
    kept: list[dict[str, str]] = []
    removed: list[str] = []
    seen: dict[str, tuple[str, str]] = {}
    for _, row in rows:
        if not row["supplier_name"] or not phone_key(row["phone"]) or not row["twogis_url"]:
            removed.append(f"{label}: {row['supplier_name'] or '(без названия)'} — нет названия, телефона или 2GIS-ссылки")
            continue
        keys = [
            ("телефон", phone_key(row["phone"])),
            ("WhatsApp", phone_key(row["whatsapp"])),
            ("2GIS ID", firm_key(row["twogis_url"])),
            ("название+адрес", text_key(row["supplier_name"]) + "|" + text_key(row["address"])),
        ]
        collision = next(((kind, value, seen[value]) for kind, value in keys if value and value in seen), None)
        if collision:
            kind, _, (kept_name, _) = collision
            removed.append(f"{label}: {row['supplier_name']} — дубль {kept_name} по полю {kind}")
            continue
        kept.append(row)
        for kind, value in keys:
            if value:
                seen[value] = (row["supplier_name"], kind)
    return kept, removed


def write(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows[:30])


def main() -> None:
    frezy, frezy_removed = dedupe(
        load([ROOT / "frezy_candidates_raw.csv", ROOT / "frezy_candidates_extra.csv"]),
        "Фрезы",
    )
    marlya, marlya_removed = dedupe(load([ROOT / "marlya_candidates_raw.csv"]), "Марля")
    write(ROOT / "karaganda_frezy_30.csv", frezy)
    write(ROOT / "karaganda_marlya_30.csv", marlya)
    report = ["# Удалённые дубли", ""] + [f"- {line}" for line in frezy_removed + marlya_removed]
    if len(report) == 2:
        report.append("- Дубли не обнаружены.")
    (ROOT / "dedupe_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"Фрезы: {len(frezy)} уникальных; записано {min(30, len(frezy))}; удалено {len(frezy_removed)}")
    print(f"Марля: {len(marlya)} уникальных; записано {min(30, len(marlya))}; удалено {len(marlya_removed)}")


if __name__ == "__main__":
    main()
