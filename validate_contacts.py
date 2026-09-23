#!/usr/bin/env python3
"""Static QA for the two supplier CSV files in this tender workspace."""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


REQUIRED_COLUMNS = [
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

PHONE_DIGITS = re.compile(r"\D+")
PHONE_DISPLAY = re.compile(r"^\+7 \d{3} \d{3} \d{2} \d{2}(?:\s*;.*)?$")
FIRM_ID = re.compile(r"/(?:firm|branches)/(\d+)")


def normalized_phone(value: str) -> str:
    digits = PHONE_DIGITS.sub("", value or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def duplicate_values(values: list[str]) -> list[str]:
    counts = Counter(value for value in values if value)
    return sorted(value for value, count in counts.items() if count > 1)


def validate(path: Path) -> list[str]:
    problems: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            return [f"нет колонок: {', '.join(missing)}"]
        rows = list(reader)

    if len(rows) < 30:
        problems.append(f"только {len(rows)} строк (нужно минимум 30)")

    phone_values = [normalized_phone(row["phone"].split(";")[0]) for row in rows]
    whatsapp_values = [normalized_phone(row["whatsapp"].split(";")[0]) for row in rows]
    firm_ids = []
    for row in rows:
        match = FIRM_ID.search(row["twogis_url"])
        firm_ids.append(match.group(1) if match else "")

    for label, values in (
        ("телефоны", phone_values),
        ("WhatsApp", whatsapp_values),
        ("2GIS ID", firm_ids),
    ):
        duplicates = duplicate_values(values)
        if duplicates:
            problems.append(f"повторяющиеся {label}: {', '.join(duplicates)}")

    allowed_statuses = {
        "✅ Совпадает",
        "⚠️ Требует подтверждения",
        "❌ Не использовать без разъяснения",
        "⛔ Точное решение не найдено",
    }
    for index, row in enumerate(rows, 2):
        if not row["supplier_name"].strip():
            problems.append(f"строка {index}: пустое название")
        if not normalized_phone(row["phone"]):
            problems.append(f"строка {index}: нет телефона")
        elif not PHONE_DISPLAY.match(row["phone"].strip()):
            problems.append(f"строка {index}: телефон не в формате +7 XXX XXX XX XX")
        parsed = urlparse(row["twogis_url"])
        if parsed.scheme not in {"http", "https"} or "2gis" not in parsed.netloc:
            problems.append(f"строка {index}: некорректная 2GIS-ссылка")
        if row["priority"] not in {"A", "B", "C", "D"}:
            problems.append(f"строка {index}: неизвестный приоритет {row['priority']!r}")
        if row["technical_status"] not in allowed_statuses:
            problems.append(f"строка {index}: неизвестный статус {row['technical_status']!r}")
        verified = row["whatsapp_verified"].strip().lower()
        if row["whatsapp"].strip() and verified not in {"true", "да", "yes", "1"}:
            problems.append(f"строка {index}: WhatsApp указан без флага подтверждения")
        if not row["whatsapp"].strip() and verified in {"true", "да", "yes", "1"}:
            problems.append(f"строка {index}: флаг WhatsApp есть, номера нет")
        address_and_notes = f"{row['address']} {row['notes']}".lower()
        if "караганда" not in address_and_notes and "резерв" not in address_and_notes:
            problems.append(f"строка {index}: не доказана Караганда и не указан резерв")
        combined = " ".join(row.values()).lower()
        if row["technical_status"] == "✅ Совпадает":
            if "фрез" in row["position"].lower() and re.search(r"\bh\s*25\b|\b25\s*мм\b", combined):
                problems.append(f"строка {index}: H25 ошибочно помечено точным совпадением")
            if "марл" in row["position"].lower() and re.search(r"\b(?:70|80|90)\s*(?:см|cm)\b", combined):
                problems.append(f"строка {index}: марля уже 1 м помечена точным совпадением")

    print(f"{path.name}: {len(rows)} строк; проблем: {len(problems)}")
    return problems


def main() -> int:
    targets = [Path(arg) for arg in sys.argv[1:]]
    if not targets:
        targets = [Path("karaganda_frezy_30.csv"), Path("karaganda_marlya_30.csv")]
    total = 0
    for target in targets:
        if not target.exists():
            print(f"{target}: файл не найден")
            total += 1
            continue
        problems = validate(target)
        for problem in problems:
            print(f"  - {problem}")
        total += len(problems)
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
