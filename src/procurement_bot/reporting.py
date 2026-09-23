from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReportEvidence(StrictModel):
    label: str = Field(min_length=1, max_length=300)
    url: str | None = Field(default=None, max_length=2000)


class SupplierReportCard(StrictModel):
    supplier_name: str = Field(min_length=1, max_length=500)
    location: str | None = Field(default=None, max_length=1000)
    product_name: str = Field(min_length=1, max_length=1000)
    technical_status: str = Field(min_length=1, max_length=100)
    availability: str = Field(min_length=1, max_length=300)
    price: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="KZT", max_length=3)
    rank: int | None = Field(default=None, ge=1)
    evidence: list[ReportEvidence] = Field(default_factory=list)
    questions_remaining: list[str] = Field(default_factory=list)


class ProcurementReport(StrictModel):
    case_reference: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    city: str = Field(min_length=1, max_length=200)
    search_scope: str = Field(min_length=1, max_length=500)
    suppliers_found: int = Field(default=0, ge=0)
    suppliers_responded: int = Field(default=0, ge=0)
    suppliers_contacted: int = Field(default=0, ge=0)
    whatsapp_unavailable: int = Field(default=0, ge=0)
    whatsapp_failed: int = Field(default=0, ge=0)
    exact_matches: int = Field(default=0, ge=0)
    cards: list[SupplierReportCard] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def render_report_html(report: ProcurementReport) -> str:
    cards = "".join(_render_card(card) for card in _ordered_cards(report.cards))
    if not cards:
        cards = '<div class="empty">Подтверждённых вариантов пока нет.</div>'
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{_escape(report.title)}</title><style>{_CSS}</style></head>
<body><main><header><div class="eyebrow">Заявка {_escape(report.case_reference)}</div>
<h1>{_escape(report.title)}</h1>
<p>{_escape(report.city)} · {_escape(report.search_scope)}</p></header>
<section class="stats"><div><b>{report.suppliers_found}</b><span>найдено</span></div>
<div><b>{report.suppliers_contacted}</b><span>написали в WhatsApp</span></div>
<div><b>{report.suppliers_responded}</b><span>ответили</span></div>
<div><b>{report.exact_matches}</b><span>точных вариантов</span></div>
<div><b>{report.whatsapp_unavailable}</b><span>без подтверждённого WhatsApp</span></div>
<div><b>{report.whatsapp_failed}</b><span>ошибок отправки</span></div></section>
<section class="cards">{cards}</section>
<footer>Сформировано: {_escape(report.generated_at.isoformat())}.
Не является заказом, резервом или обязательством купить.</footer></main></body></html>"""


def _ordered_cards(cards: list[SupplierReportCard]) -> list[SupplierReportCard]:
    return sorted(
        cards,
        key=lambda card: (
            card.rank is None,
            card.rank or 10**9,
            card.supplier_name.casefold(),
        ),
    )


def _render_card(card: SupplierReportCard) -> str:
    rank = f'<span class="rank">#{card.rank}</span>' if card.rank else ""
    price = "Цена не подтверждена"
    if card.price is not None:
        price = f"{card.price:,.2f} {_escape(card.currency)}".replace(",", " ")
    evidence = "".join(f"<li>{_evidence(value)}</li>" for value in card.evidence)
    questions = "".join(f"<li>{_escape(value)}</li>" for value in card.questions_remaining)
    location = f"<p class=muted>{_escape(card.location)}</p>" if card.location else ""
    return f"""<article>{rank}<h2>{_escape(card.supplier_name)}</h2>{location}
<h3>{_escape(card.product_name)}</h3><div class="badges">
<span>{_escape(card.technical_status)}</span>
<span>{_escape(card.availability)}</span></div><p class="price">{price}</p>
<h4>Доказательства</h4><ul>{evidence or '<li>Нет сохранённых доказательств</li>'}</ul>
<h4>Что уточнить</h4><ul>{questions or '<li>Критичных уточнений нет</li>'}</ul></article>"""


def _evidence(value: ReportEvidence) -> str:
    if value.url and urlparse(value.url).scheme in {"http", "https"}:
        safe_url = html.escape(value.url, quote=True)
        return f'<a rel="noreferrer" href="{safe_url}">{_escape(value.label)}</a>'
    return _escape(value.label)


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


_CSS = """
:root{color-scheme:light;font-family:Inter,system-ui,sans-serif;
background:#f4f6f8;color:#17202a}
body{margin:0}main{max-width:960px;margin:auto;padding:36px 20px}
header{background:#14213d;color:white;padding:32px;border-radius:20px}
h1{margin:.25rem 0}.eyebrow{color:#fca311;font-weight:700}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:20px 0}
.stats div,article,.empty{background:white;border:1px solid #dfe4ea;
border-radius:16px;padding:20px}.stats b{font-size:28px;display:block}
.stats span,.muted,footer{color:#697386}.cards{display:grid;gap:16px}
article{position:relative}.rank{position:absolute;right:20px;top:20px;
background:#fca311;padding:6px 10px;border-radius:999px;font-weight:800}
.badges{display:flex;gap:8px;flex-wrap:wrap}.badges span{background:#e9f2ff;
padding:5px 9px;border-radius:8px}.price{font-size:24px;font-weight:800}
a{color:#0759b8}footer{padding:24px 4px;font-size:13px}
@media(max-width:600px){.stats{grid-template-columns:1fr}}
"""
