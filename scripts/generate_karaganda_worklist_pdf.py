#!/usr/bin/env python3
"""Generate a minimal Karaganda_1 worklist for an external procurement helper."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "karaganda_1_spisok_v_rabote_2026-07-20.pdf"

FONT_DIRS = [
    Path("/System/Library/Fonts/Supplemental"),
    Path("/Library/Fonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fonts/truetype",
]


def find_font(filename: str) -> Path:
    for directory in FONT_DIRS:
        candidate = directory / filename
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Font not found: {filename}")


pdfmetrics.registerFont(TTFont("DocSans", str(find_font("DejaVuSans.ttf"))))
pdfmetrics.registerFont(TTFont("DocSans-Bold", str(find_font("DejaVuSans-Bold.ttf"))))


# (number, item, exact specification, remaining quantity, unit, tender unit limit)
BUY = [
    (17, "Губка", "Мелкопористые губки для аквариумного фильтра 65 x 70 мм, форма цилиндр, желтые WP-3300А. По согласованию с заказчиком", 10, "шт.", 500),
    (22, "Колер", "Колер цветной, 140 мл. Цвет №3. По согласованию с заказчиком", 20, "шт.", 400),
    (25, "Лак", "Лак для наружных работ на водной основе. По согласованию с заказчиком", 10, "кг", 1900),
    (28, "Лента шлифовальная", "Лента шлифовальная 100 x 914 мм, мелкая. По согласованию с заказчиком", 5, "шт.", 1600),
    (31, "Мешок для белья", "Мешок для белья, 100 x 150 см. По согласованию с заказчиком", 3, "шт.", 6500),
    (33, "Комплект ключей и головок", "Набор шестигранников - 13 предметов, размеры от 1 до 12 мм. По согласованию с заказчиком", 1, "набор", 4000),
    (37, "Пакля", "Лен сантехнический, коса 1 кг. По согласованию с заказчиком", 1, "кг", 1500),
    (40, "Перчатки лабораторные", "Перчатки лабораторные, защитные резиновые, до локтя, устойчивые к кислотам, щелочам, химически стойкие. По согласованию с заказчиком", 1, "пара", 3100),
    (42, "Плита потолочная Armstrong", "Плита для потолка АРМСТРОНГ. Потолочная панель декоративная тепло- и звукоизоляционная, 60 x 60 см. По согласованию с заказчиком", 40, "шт.", 2300),
    (45, "Растворитель", "Растворитель 0,5 л, растворитель 646. По согласованию с заказчиком", 10, "шт.", 600),
    (52, "Степлер", "Тапенер (степлер) садовый, пластик, нержавеющая сталь. По согласованию с заказчиком", 2, "шт.", 5900),
    (57, "Уголок", "Уголок для плинтуса наружный. По согласованию с заказчиком", 20, "шт.", 370),
    (61, "Цемент общестроительный", "Цемент ПЦ-400 (50 кг). По согласованию с заказчиком", 5, "шт.", 2100),
    (64, "Шпатлевка", "Шпатлевка Glatt 25 кг. По согласованию с заказчиком", 5, "шт.", 4300),
    (65, "Шпатлевка", "Шпатлевка Finish 25 кг. По согласованию с заказчиком", 5, "шт.", 4300),
]

REPLACE = [
    (39, "Машина стационарная для сварки", "Аппарат для сварки пластиковых труб 2000 Вт. По согласованию с заказчиком", 1, "шт.", 12999),
    (53, "Сушилка", "Сушилка напольная, складная с металлическим защитным покрытием, рабочая длина 18 м, максимальная нагрузка 15 кг. По согласованию с заказчиком", 6, "шт.", 5500),
    (54, "Тряпка", "Тряпка МОП петельчатая, 50 см, хлопок, высокая впитываемость. По согласованию с заказчиком", 20, "шт.", 1700),
    (62, "Швабра", "Швабра МОП для влажной уборки, 55 см. По согласованию с заказчиком", 10, "шт.", 5000),
]

assert len(BUY) == 15
assert len(REPLACE) == 4
assert len(BUY) + len(REPLACE) == 19
assert sum(qty * price for _, _, _, qty, _, price in BUY) == 238800
assert sum(qty * price for _, _, _, qty, _, price in REPLACE) == 129999


styles = getSampleStyleSheet()
TITLE = ParagraphStyle(
    "TitleRU", parent=styles["Title"], fontName="DocSans-Bold", fontSize=16,
    leading=20, textColor=colors.HexColor("#17324D"), spaceAfter=2.5 * mm,
)
SUBTITLE = ParagraphStyle(
    "SubtitleRU", parent=styles["BodyText"], fontName="DocSans", fontSize=8.5,
    leading=11, textColor=colors.HexColor("#415466"), spaceAfter=4 * mm,
)
H1 = ParagraphStyle(
    "H1RU", parent=styles["Heading1"], fontName="DocSans-Bold", fontSize=11.5,
    leading=14, textColor=colors.HexColor("#17324D"), spaceBefore=1 * mm,
    spaceAfter=2.5 * mm,
)
CELL = ParagraphStyle(
    "CellRU", parent=styles["BodyText"], fontName="DocSans", fontSize=7.4,
    leading=9.4, textColor=colors.HexColor("#18222C"), alignment=TA_LEFT,
)
CELL_CENTER = ParagraphStyle("CellCenterRU", parent=CELL, alignment=TA_CENTER)
CELL_RIGHT = ParagraphStyle("CellRightRU", parent=CELL, alignment=TA_RIGHT)
HEADER = ParagraphStyle(
    "HeaderRU", parent=CELL, fontName="DocSans-Bold", textColor=colors.white,
    alignment=TA_LEFT,
)
FOOT = ParagraphStyle(
    "FootRU", parent=CELL, fontSize=7.1, leading=9, textColor=colors.HexColor("#415466"),
)


def text(value: object, style: ParagraphStyle = CELL) -> Paragraph:
    return Paragraph(escape(str(value)), style)


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def price_cell(unit_price: int, quantity: int) -> Paragraph:
    total = unit_price * quantity
    return Paragraph(
        f"{money(unit_price)} / ед.<br/><b>{money(total)}</b> на остаток",
        CELL_RIGHT,
    )


def work_table(rows: list[tuple[int, str, str, int, str, int]], color: str) -> Table:
    data = [[
        text("№", HEADER),
        text("Товар", HEADER),
        text("Техническая спецификация", HEADER),
        text("Остаток", HEADER),
        text("Тендерный лимит, ₸", HEADER),
    ]]
    for number, name, specification, quantity, unit, unit_price in rows:
        data.append([
            text(number, CELL_CENTER),
            text(name),
            text(specification),
            text(f"{quantity:g} {unit}", CELL_CENTER),
            price_cell(unit_price, quantity),
        ])
    result = Table(
        data,
        colWidths=[9 * mm, 37 * mm, 86 * mm, 21 * mm, 29 * mm],
        repeatRows=1,
        hAlign="LEFT",
    )
    result.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(color)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#A7B1BB")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7F9")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return result


def footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("DocSans", 7)
    canvas.setFillColor(colors.HexColor("#657789"))
    canvas.drawString(14 * mm, 10 * mm, "Караганда_1 - список для закупки")
    canvas.drawRightString(196 * mm, 10 * mm, f"стр. {doc.page}")
    canvas.restoreState()


def build_pdf() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(
        str(OUTPUT), pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=13 * mm, bottomMargin=17 * mm,
        title="Караганда_1 - список позиций в работе",
        author="Tender procurement agent",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
    doc.addPageTemplates([PageTemplate(id="main", frames=frame, onPage=footer)])

    buy_total = sum(quantity * price for _, _, _, quantity, _, price in BUY)
    replace_total = sum(quantity * price for _, _, _, quantity, _, price in REPLACE)
    story = [
        Paragraph("Караганда_1 - позиции в работе", TITLE),
        Paragraph(
            "19 позиций. Тендерный лимит указан для ориентира и не является ценой поставщика.",
            SUBTITLE,
        ),
        Paragraph("1. Купить / допоставить", H1),
        work_table(BUY, "#1D6B4F"),
        Spacer(1, 3 * mm),
        Paragraph(f"Лимит на остаток раздела: <b>{money(buy_total)} ₸</b>", FOOT),
        PageBreak(),
        Paragraph("2. Подобрать согласованную замену", H1),
        Paragraph(
            "Покупать только после согласования конкретной модели или образца с заказчиком.",
            SUBTITLE,
        ),
        work_table(REPLACE, "#A35A1F"),
        Spacer(1, 3 * mm),
        Paragraph(f"Лимит на остаток раздела: <b>{money(replace_total)} ₸</b>", FOOT),
        Spacer(1, 4 * mm),
        Paragraph(
            f"Общий тендерный лимит на 19 позиций: <b>{money(buy_total + replace_total)} ₸</b>",
            FOOT,
        ),
    ]
    doc.build(story)


if __name__ == "__main__":
    build_pdf()
    print(OUTPUT)
