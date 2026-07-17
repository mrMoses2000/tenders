#!/usr/bin/env python3
"""Generate the Karaganda_1 procurement and school-check reconciliation PDF."""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
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
OUTPUT = ROOT / "output" / "pdf" / "karaganda_1_dokupka_zamena_proverka_2026-07-18.pdf"

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


BUY = [
    (17, "Губка для аквариумного фильтра", "10 шт.", "65×70 мм, цилиндр, жёлтая, WP-3300A", "500", "Не закуплена; школа не нашла"),
    (22, "Колер", "20 шт.", "140 мл, цвет №3", "400", "Поставлено 100/120; допоставить остаток"),
    (25, "Лак", "10 кг", "Для наружных работ, на водной основе", "1 900", "Не закуплен; школа не нашла"),
    (28, "Лента шлифовальная", "5 шт.", "100×914 мм, мелкая", "1 600", "Не закуплена; школа не нашла"),
    (31, "Мешок для белья", "3 шт.", "100×150 см", "6 500", "Не закуплен; школа не нашла"),
    (33, "Комплект ключей и головок", "1 набор", "13 шестигранников, 1-12 мм", "4 000", "Не закуплен; школа не нашла"),
    (37, "Пакля / лён сантехнический", "1 кг", "Коса, фасовка 1 кг", "1 500", "Не закуплена; школа не нашла"),
    (40, "Перчатки лабораторные", "1 пара", "Резиновые, до локтя, кислот-/щелочестойкие", "3 100", "Хозяйственные перчатки №6 не закрывают это ТЗ"),
    (42, "Плита потолочная Armstrong", "40 шт.", "60×60 см, декоративная, тепло-/звукоизоляционная", "2 300", "Не закуплена; школа не нашла"),
    (45, "Растворитель 646", "10 шт.", "0,5 л", "600", "Не закуплен; школа не нашла"),
    (52, "Тапенер (садовый степлер)", "2 шт.", "Пластик и нержавеющая сталь", "5 900", "Не закуплен; школа не нашла"),
    (57, "Уголок для плинтуса", "20 шт.", "Наружный угол", "370", "Не закуплен; школа не нашла"),
    (61, "Цемент ПЦ-400", "5 мешков", "50 кг", "2 100", "Не закуплен; школа не нашла"),
    (64, "Шпатлёвка Glatt", "5 мешков", "25 кг", "4 300", "Не закуплена; школа не нашла"),
    (65, "Шпатлёвка Finish", "5 мешков", "25 кг", "4 300", "Не закуплена; школа не нашла"),
]

REPLACE = [
    (39, "Аппарат для сварки пластиковых труб", "1 шт.", "2000 Вт", "ЗФО: «не подходит», без конкретизации"),
    (53, "Сушилка напольная", "6 шт.", "Складная, металл, рабочая длина 18 м, нагрузка 15 кг", "В школьной ведомости стоит «замена»"),
    (54, "МОП-тряпка", "20 шт.", "Петельчатая, хлопок, 50 см, высокая впитываемость", "В школьной ведомости стоит «замена»"),
    (62, "Швабра МОП", "10 шт.", "Для влажной уборки, 55 см", "ЗФО: «не подходит», без конкретизации"),
]

VERIFY = [
    (1, "Валик", "50", "Передан по первой части; в школьной ведомости отсутствует"),
    (2, "Ёрш", "64", "Передан по первой части; в школьной ведомости отсутствует"),
    (3, "Кисть малярная", "30", "Передана по первой части; в школьной ведомости отсутствует"),
    (4, "Кисть плоская", "25", "Передана по первой части; в школьной ведомости отсутствует"),
    (5, "Пакет для мусора", "10 000", "Передан по первой части; в школьной ведомости отсутствует"),
    (6, "Перчатки латексные / хозяйственные резиновые", "852", "Пользователь подтвердил полную поставку; школьная ведомость первую часть не содержит"),
    (7, "Розетка двойная", "10", "Передана по первой части; в школьной ведомости отсутствует"),
    (8, "Смеситель для душа", "20", "Передан по первой части; в школьной ведомости отсутствует"),
    (9, "Веник", "100", "Обведён как закупленный, но школа написала «нет»"),
    (10, "Ветошь", "354 м", "Обведена как закупленная, но школа написала «нет»"),
    (11, "Головка косильная", "5", "Обведена как закупленная, но школа написала «нет»"),
    (12, "Губка металлическая", "25", "Обведена как закупленная, но школа написала «нет»"),
    (13, "Диск 190 мм T80", "4", "Обведён как закупленный, но школа написала «нет»"),
    (14, "Замок", "40", "Обведён как закупленный; строка отсутствует в школьной ведомости"),
    (15, "Известь негашёная", "200 кг", "Пользователь подтвердил полную поставку; школа написала «нет»"),
    (19, "Клей кафельный 25 кг", "9", "Пользователь подтвердил полную поставку; школа написала «нет»"),
    (20, "Клеммник LD227 16A", "10", "Обведён как закупленный, но школа написала «нет»"),
    (22, "Колер 140 мл", "100 поставленных", "Найти и пересчитать поставленные 100; отдельно допоставить 20 №3"),
    (23, "Корзина 9 л", "3", "Обведена как закупленная, но школа написала «нет»"),
    (26, "Лампа LED E40 70W", "20", "Обведена как закупленная, но школа написала «нет»"),
    (29, "Леска триммерная 2,4 мм × 100 м", "8", "Обведена как закупленная, но школа написала «нет»"),
    (30, "Марля белая, ширина 1 м", "25 м", "Обведена как закупленная, но школа написала «нет»"),
    (32, "Мешок строительный 55×95, 80 г", "100", "Обведён как закупленный, но школа написала «нет»"),
    (34, "Петля с доводчиком для ДСП", "50", "Обведена как закупленная, но школа написала «нет»"),
    (38, "Патрон E40 керамический", "10", "Обведён как закупленный, но школа написала «нет»"),
    (41, "Перчатки х/б с брезентовым наладонником", "50 пар", "Ранее отмечены как доставленные; школа написала «нет»"),
    (43, "Подоконник ПВХ 300×50 см", "30 м", "Обведён как закупленный, но школа написала «нет»"),
    (50, "Светильник LED 50 Вт, 120 см", "75", "Замена сообщена как доставленная; подтвердить принятие и модель"),
    (51, "Скобы 14 мм, 1000 шт.", "1 уп.", "Обведены как закупленные, но школа написала «нет»"),
]

SOURCE_HASHES = [
    "first/12.13.53 - c3f8dff9109b…",
    "first/12.14.11 - a2fc7c057c52…",
    "first/12.14.27 - 458b8fbfca40…",
    "first/12.14.44 - cbc45d9cdfb5…",
    "first/12.15.09 - 13e0ac46a722…",
    "second/00.01.08 - 4488c2379eae…",
    "second/00.01.09 - f99a51ac9f96…",
    "second/00.01.10 - 98991cbf0b1a…",
    "second/00.01.10 (1) - c5b2b6f00e3d…",
    "second/00.01.11 - ae7042b9a937…",
    "second/00.01.11 (1) - cb3292f69898…",
    "second/00.01.12 - e694703b7b64…",
]


styles = getSampleStyleSheet()
TITLE = ParagraphStyle(
    "TitleRU", parent=styles["Title"], fontName="DocSans-Bold", fontSize=17,
    leading=21, textColor=colors.HexColor("#16324F"), spaceAfter=5 * mm,
)
H1 = ParagraphStyle(
    "H1RU", parent=styles["Heading1"], fontName="DocSans-Bold", fontSize=12,
    leading=15, textColor=colors.HexColor("#16324F"), spaceBefore=3 * mm,
    spaceAfter=2.5 * mm,
)
BODY = ParagraphStyle(
    "BodyRU", parent=styles["BodyText"], fontName="DocSans", fontSize=8.2,
    leading=11, textColor=colors.HexColor("#1F2933"),
)
SMALL = ParagraphStyle(
    "SmallRU", parent=BODY, fontSize=7.1, leading=9,
)
WHITE = ParagraphStyle(
    "WhiteRU", parent=SMALL, fontName="DocSans-Bold", textColor=colors.white,
    alignment=TA_LEFT,
)
CENTER = ParagraphStyle(
    "CenterRU", parent=SMALL, alignment=TA_CENTER,
)
ALERT = ParagraphStyle(
    "AlertRU", parent=BODY, fontName="DocSans-Bold", fontSize=9, leading=12,
    textColor=colors.HexColor("#8A2C2C"), backColor=colors.HexColor("#FDECEC"),
    borderPadding=6, spaceAfter=4 * mm,
)


def p(text: object, style=SMALL) -> Paragraph:
    # All content in this report is controlled locally. Keep ReportLab's small
    # inline-markup subset enabled so <b> and <br/> render instead of leaking
    # into the finished PDF as literal text.
    return Paragraph(str(text), style)


def table(data, widths, header_color="#16324F", repeat_rows=1):
    result = Table(data, colWidths=widths, repeatRows=repeat_rows, hAlign="LEFT")
    result.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_color)),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "DocSans-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "DocSans"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("LEADING", (0, 0), (-1, -1), 9),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#A8B2BD")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F7F9")]),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    return result


def header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("DocSans", 7)
    canvas.setFillColor(colors.HexColor("#66788A"))
    canvas.drawString(18 * mm, 11 * mm, "Караганда_1 - сверка поставки, замены и школьского наличия")
    canvas.drawRightString(192 * mm, 11 * mm, f"стр. {doc.page}")
    canvas.restoreState()


def build_pdf() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(
        str(OUTPUT), pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=18 * mm, title="Караганда_1 - что докупить, заменить и проверить",
        author="Tender procurement agent",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
    doc.addPageTemplates([PageTemplate(id="main", frames=frame, onPage=header_footer)])

    story = [
        Paragraph("Караганда_1: итоговая сверка", TITLE),
        p("Что докупить, что заменить и что обязательно проверить в школе", BODY),
        Spacer(1, 2 * mm),
        Paragraph(
            "ВАЖНО: позиции из раздела «Проверить» повторно не покупать, пока не проверены склад команды и школа. "
            "Кружок на первичной ведомости означает закупку/наличие у команды, но не доказывает приёмку школой.",
            ALERT,
        ),
    ]

    summary = [
        [p("Категория", WHITE), p("Строк", WHITE), p("Что делать", WHITE)],
        [p("Докупить / допоставить"), p(len(BUY), CENTER), p("14 отсутствующих строк + 20 колеров цвета №3")],
        [p("Заменить / переделать"), p(len(REPLACE), CENTER), p("Сначала согласовать принимаемую модель/вариант с ЗФО")],
        [p("Проверить физически"), p(len(VERIFY), CENTER), p("Школа + склад команды; фото, количество, место хранения")],
        [p("Уже найдено школой"), p(19, CENTER), p("Повторная закупка не требуется")],
    ]
    story += [table(summary, [45 * mm, 20 * mm, 113 * mm]), Spacer(1, 4 * mm)]
    story += [
        p("<b>Контрольная стоимость по тендерным ценам:</b> докупка 238 800 ₸; незавершённые замены 129 999 ₸. "
          "Это не фактическая закупочная цена и не включает доставку.", BODY),
        Spacer(1, 3 * mm),
        p("<b>Прямые подтверждения пользователя:</b> известь и кафельный клей поставлены полностью; колеры - "
          "100 из 120, остаток 20 шт. цвета №3; хозяйственные резиновые перчатки №6 поставлены; светильники доставлены. "
          "Это подтверждение не закрывает лабораторные химстойкие перчатки №40.", BODY),
        PageBreak(),
        Paragraph("1. Докупить / допоставить", H1),
        p("Эти строки не были отмечены как закупленные и не найдены школой. Колер - подтверждённая частичная допоставка.", BODY),
        Spacer(1, 2 * mm),
    ]
    buy_data = [[p("✓", WHITE), p("№", WHITE), p("Товар и точное ТЗ", WHITE), p("Кол-во", WHITE), p("Лимит, ₸/ед.", WHITE), p("Основание", WHITE)]]
    for no, name, qty, spec, price, reason in BUY:
        buy_data.append([p("☐", CENTER), p(no, CENTER), p(f"<b>{name}</b><br/>{spec}"), p(qty), p(price), p(reason)])
    story += [table(buy_data, [9 * mm, 9 * mm, 71 * mm, 20 * mm, 22 * mm, 47 * mm], "#1F6F4A")]
    story += [Spacer(1, 4 * mm), p("<b>Итого по тендерным лимитам:</b> 238 800 ₸. Перед закупкой использовать предложения Notion, подтвердить актуальную цену, точное ТЗ, наличие, документы и доставку.", BODY)]

    story += [PageBreak(), Paragraph("2. Заменить / переделать", H1)]
    story += [Paragraph(
        "Причина по сообщению пользователя: ЗФО сказал «не подходит» без подробного технического объяснения. "
        "Перед покупкой показать фото/модель ЗФО и получить явное согласование.",
        ALERT,
    )]
    repl_data = [[p("✓", WHITE), p("№", WHITE), p("Товар", WHITE), p("Кол-во", WHITE), p("Исходное ТЗ", WHITE), p("Причина", WHITE)]]
    for no, name, qty, spec, reason in REPLACE:
        repl_data.append([p("☐", CENTER), p(no, CENTER), p(name), p(qty), p(spec), p(reason)])
    story += [table(repl_data, [9 * mm, 9 * mm, 42 * mm, 18 * mm, 57 * mm, 43 * mm], "#A45A18")]
    story += [Spacer(1, 3 * mm), p("<b>Светильники №50 не включены в повторную закупку:</b> пользователь сообщил, что они доставлены. Статус - «Замена выполнена - проверить»; школа должна подтвердить 75 шт. и принимаемую модель.", BODY)]
    story += [Spacer(1, 2 * mm), p("<b>Контрольная стоимость незавершённых замен по тендерным ценам:</b> 129 999 ₸.", BODY)]

    story += [Spacer(1, 5 * mm), Paragraph("3. Обязательно проверить в школе и на складе команды", H1)]
    story += [p("Для каждой строки: найти товар, пересчитать, сверить маркировку/ТЗ, сделать фото товара и места хранения, записать фактически принятое количество.", BODY), Spacer(1, 2 * mm)]
    verify_data = [[p("Шк.", WHITE), p("Скл.", WHITE), p("Фото", WHITE), p("№", WHITE), p("Товар", WHITE), p("Кол-во", WHITE), p("Почему проверять", WHITE)]]
    for no, name, qty, reason in VERIFY:
        verify_data.append([p("☐", CENTER), p("☐", CENTER), p("☐", CENTER), p(no, CENTER), p(name), p(qty), p(reason)])
    story += [table(verify_data, [9 * mm, 9 * mm, 9 * mm, 9 * mm, 49 * mm, 21 * mm, 72 * mm], "#3E5C76")]

    story += [
        Spacer(1, 4 * mm),
        Paragraph("4. Как зафиксировать результат проверки", H1),
        p("Заполнить для каждой проверенной строки:", BODY),
        Spacer(1, 2 * mm),
    ]
    checklist = [
        [p("Поле", WHITE), p("Что записать", WHITE)],
        [p("Фактически найдено"), p("Количество в школе и отдельно на складе команды")],
        [p("Состояние"), p("Соответствует ТЗ / повреждено / не тот товар / требует замены")],
        [p("Фото"), p("Общий вид, этикетка/модель/размер и место хранения")],
        [p("Приёмка"), p("Кто подтвердил, дата и принятое количество")],
        [p("Действие"), p("Закрыть / допоставить конкретный остаток / заменить / оставить на проверке")],
    ]
    story += [table(checklist, [45 * mm, 133 * mm])]
    story += [Spacer(1, 4 * mm), Paragraph("5. Источники сверки", H1)]
    story += [p("Первичная ведомость закупки: Karaganda/first (кружком отмечено закупленное). Школьная ведомость: Karaganda/second («нет», галочки и отметки «замена»). Прямые подтверждения пользователя имеют приоритет над рукописным «нет», но не заменяют школьную приёмку.", BODY), Spacer(1, 2 * mm)]
    source_pairs = [value.split(" - ", 1) for value in SOURCE_HASHES]
    source_data = [[
        p("Файл / время", WHITE), p("SHA-256", WHITE),
        p("Файл / время", WHITE), p("SHA-256", WHITE),
    ]]
    midpoint = (len(source_pairs) + 1) // 2
    for index in range(midpoint):
        left_a, hash_a = source_pairs[index]
        if index + midpoint < len(source_pairs):
            left_b, hash_b = source_pairs[index + midpoint]
        else:
            left_b, hash_b = "", ""
        source_data.append([p(left_a), p(hash_a), p(left_b), p(hash_b)])
    story += [table(source_data, [45 * mm, 44 * mm, 45 * mm, 44 * mm])]
    story += [Spacer(1, 4 * mm), p("Сформировано 18.07.2026. Рабочая база: Notion «Максим → Заявки → Караганда_1». Исходные техспеки и тендерные количества не изменялись.", SMALL)]

    doc.build(story)


if __name__ == "__main__":
    build_pdf()
    print(OUTPUT)
