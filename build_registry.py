#!/usr/bin/env python3
"""Build the human-readable supplier registry from the two audited CSV files."""

from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "karaganda_supplier_registry.md"


def load(name: str) -> list[dict[str, str]]:
    with (ROOT / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", "<br>").strip() or "—"


def table(title: str, rows: list[dict[str, str]]) -> str:
    headers = [
        "№",
        "Приоритет",
        "Название",
        "Тип магазина",
        "Адрес",
        "Телефон",
        "WhatsApp",
        "2GIS",
        "Сайт/Instagram",
        "Что может быть",
        "Доказательство релевантности",
        "Статус соответствия",
        "Что уточнить",
        "Комментарий",
    ]
    lines = [f"## {title}", "", "| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for index, row in enumerate(rows, 1):
        sites = "<br>".join(filter(None, [clean(row["website"]) if row["website"].strip() else "", clean(row["instagram"]) if row["instagram"].strip() else ""])) or "—"
        twogis = f"[карточка]({row['twogis_url']})" if row["twogis_url"].strip() else "—"
        comment_parts = []
        if row["business_hours"].strip():
            comment_parts.append(f"Режим: {row['business_hours'].strip()}")
        if row["notes"].strip():
            comment_parts.append(row["notes"].strip())
        values = [
            str(index),
            row["priority"],
            row["supplier_name"],
            row["supplier_type"],
            row["address"],
            row["phone"],
            row["whatsapp"],
            twogis,
            sites,
            row["position"],
            row["relevance_evidence"],
            row["technical_status"],
            row["question_to_supplier"],
            "; ".join(comment_parts),
        ]
        lines.append("| " + " | ".join(clean(value) for value in values) + " |")
    return "\n".join(lines)


def stats(rows: list[dict[str, str]]) -> dict[str, int]:
    return {
        "total": len(rows),
        "whatsapp": sum(row["whatsapp_verified"].strip().lower() in {"true", "да", "yes", "1"} for row in rows),
        "profile": sum(row["priority"] == "A" for row in rows),
        "reserve": sum(row["priority"] in {"C", "D"} for row in rows),
        "pending": sum(row["technical_status"] == "⚠️ Требует подтверждения" for row in rows),
    }


def top_ten(title: str, rows: list[dict[str, str]]) -> str:
    ordered = sorted(enumerate(rows), key=lambda item: ({"A": 0, "B": 1, "C": 2, "D": 3}.get(item[1]["priority"], 9), item[0]))
    lines = [f"### {title}", ""]
    for number, (_, row) in enumerate(ordered[:10], 1):
        channel = f"WhatsApp {row['whatsapp']}" if row["whatsapp"].strip() else f"телефон {row['phone']}"
        lines.append(f"{number}. {clean(row['supplier_name'])} — {channel}; {clean(row['address'])}.")
    return "\n".join(lines)


def phone_only(frezy: list[dict[str, str]], marlya: list[dict[str, str]]) -> str:
    seen: set[tuple[str, str]] = set()
    lines = ["### Контакты без WhatsApp, которым можно только позвонить", ""]
    for group, rows in (("фрезы", frezy), ("марля", marlya)):
        for row in rows:
            key = (row["supplier_name"], row["phone"])
            if not row["whatsapp"].strip() and key not in seen:
                seen.add(key)
                lines.append(f"- {clean(row['supplier_name'])} ({group}) — {clean(row['phone'])}.")
    if len(lines) == 2:
        lines.append("- Нет.")
    return "\n".join(lines)


def notes_section() -> str:
    parts = ["### Удалённые дубли с причиной удаления", ""]
    found = False
    for filename in ("dedupe_report.md", "frezy_notes.md", "frezy_extra_notes.md", "marlya_notes.md"):
        path = ROOT / filename
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            found = True
            parts.extend([f"#### {filename}", "", text, ""])
    if not found:
        parts.append("- Удалённые дубли не зафиксированы в исходных заметках.")
    return "\n".join(parts)


def main() -> None:
    frezy = load("karaganda_frezy_30.csv")
    marlya = load("karaganda_marlya_30.csv")
    fs, ms = stats(frezy), stats(marlya)

    sections = [
        "# Реестр поставщиков: Караганда — фрезы и марля",
        "",
        "Дата проверки: 25 августа 2026 года. Наличие, цена и точные технические параметры, не доказанные публичным каталогом, требуют подтверждения у поставщика. Сообщения и звонки не выполнялись.",
        "",
        "## Краткое резюме",
        "",
        f"- Фрезы: {fs['total']} контактов; подтверждённый WhatsApp — {fs['whatsapp']}; приоритет A — {fs['profile']}; резерв C/D — {fs['reserve']}; требуют подтверждения — {fs['pending']}.",
        f"- Марля: {ms['total']} контактов; подтверждённый WhatsApp — {ms['whatsapp']}; приоритет A — {ms['profile']}; резерв C/D — {ms['reserve']}; требуют подтверждения — {ms['pending']}.",
        "- Точный статус `✅ Совпадает` допустим только при доказательстве всех обязательных размеров, текущего остатка, количества, цены и пути получения. Карточка 2GIS сама по себе подтверждает магазин и контакты, но не наличие конкретного товара.",
        "",
        table("Фрезы — поставщики", frezy),
        "",
        table("Марля — поставщики", marlya),
        "",
        "## Приоритетные списки",
        "",
        top_ten("Топ-10 магазинов, которым писать первыми по фрезам", frezy),
        "",
        top_ten("Топ-10 магазинов, которым писать первыми по марле", marlya),
        "",
        phone_only(frezy, marlya),
        "",
        notes_section(),
        "",
        "## Тексты для будущей рассылки",
        "",
        "### Фрезы — первый контакт",
        "",
        "> Здравствуйте! Ищем две позиции по фото:",
        ">",
        "> 1. Обкаточная фреза с верхним подшипником — D10 мм, хвостовик 8 мм, общая длина 50 мм. Нужно 2 штуки.",
        "> 2. Прямая пазовая фреза — D10 мм, хвостовик 8 мм, рабочая длина 35 мм. Нужна 1 штука.",
        ">",
        "> Подскажите, пожалуйста, есть ли такие в наличии и сколько стоят? Если есть, пришлите фото фрез или маркировки на упаковке.",
        "",
        "### Фрезы — продолжение начатого диалога",
        "",
        "> Ещё нужны две позиции по фото:",
        ">",
        "> 1. Обкаточная фреза с верхним подшипником — D10 мм, хвостовик 8 мм, общая длина 50 мм, 2 штуки.",
        "> 2. Прямая пазовая фреза — D10 мм, хвостовик 8 мм, рабочая длина 35 мм, 1 штука.",
        ">",
        "> Проверьте, пожалуйста, наличие и цену. Если есть, пришлите фото фрез или маркировки на упаковке.",
        "",
        "### Марля",
        "",
        "> Здравствуйте! Подскажите, есть ли у вас белая тканевая марля шириной строго 1 метр? Нужно 25 погонных метров, желательно одним отрезом или рулоном. Напишите, пожалуйста, точную ширину, цену за метр и есть ли сейчас 25 метров в наличии.",
        "",
        "Отдельный обязательный вопрос: уточнить причину возврата предыдущей марли.",
    ]
    OUTPUT.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
