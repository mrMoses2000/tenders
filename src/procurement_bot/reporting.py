from __future__ import annotations

import html
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReportEvidence(StrictModel):
    label: str = Field(min_length=1, max_length=300)
    url: str | None = Field(default=None, max_length=2000)


class SupplierReportCard(StrictModel):
    item_number: int = Field(default=0, ge=0)
    item_name: str = Field(default="", max_length=500)
    supplier_name: str = Field(min_length=1, max_length=500)
    supplier_phone: str | None = Field(default=None, max_length=30)
    whatsapp_confirmed: bool = False
    location: str | None = Field(default=None, max_length=1000)
    product_name: str = Field(min_length=1, max_length=1000)
    technical_status: str = Field(min_length=1, max_length=100)
    availability: str = Field(min_length=1, max_length=300)
    price: Decimal | None = Field(default=None, ge=0)
    price_unit: str | None = Field(default=None, max_length=50)
    required_quantity: Decimal | None = Field(default=None, ge=0)
    available_quantity: Decimal | None = Field(default=None, ge=0)
    purchased_quantity: Decimal | None = Field(default=None, ge=0)
    goods_total: Decimal | None = Field(default=None, ge=0)
    tender_total: Decimal | None = Field(default=None, ge=0)
    tender_difference: Decimal | None = None
    price_state: str = "unknown"
    currency: str = Field(default="KZT", max_length=3)
    rank: int | None = Field(default=None, ge=1)
    evidence: list[ReportEvidence] = Field(default_factory=list)
    questions_remaining: list[str] = Field(default_factory=list)


class ReportItem(StrictModel):
    """A request line shown even when no supplier offer has been recorded."""

    item_number: int = Field(ge=1)
    item_name: str = Field(min_length=1, max_length=500)
    quantity: Decimal | None = Field(default=None, ge=0)
    unit: str | None = Field(default=None, max_length=100)
    specification_text: str | None = None


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
    operational_metrics: dict[str, int] = Field(default_factory=dict)
    operational_status: dict[str, Any] = Field(default_factory=dict)
    cards: list[SupplierReportCard] = Field(default_factory=list)
    items: list[ReportItem] = Field(default_factory=list)
    notice: str | None = Field(default=None, max_length=1000)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def render_report_html(report: ProcurementReport) -> str:
    cards = _render_groups(_ordered_cards(report.cards), report.items)
    labels = {
        "leads": "Исследовательские лиды",
        "public_prices": "Наблюдения публичных цен",
        "supplier_prices": "Наблюдения цен из ответов поставщиков",
        "comparable_quotes": "Сопоставимые подтверждённые предложения",
        "accepted": "Отправки, принятые WAHA",
        "delivery_unknown": "Доставка без подтверждения",
        "acceptance_unknown": "Попытки отправки без подтверждения приёма",
        "accepted_pending_projection": "Приняты WAHA, ожидают записи в диалог",
        "delivered": "Доставлены на устройство",
        "read": "Прочитаны",
        "delivery_failed": "Ошибка доставки по ACK",
        "waiting_chats": "Незавершённые переговоры",
        "active_groups": "Активные рабочие группы",
        "outbound_projected": "Исходящие сообщения в журнале диалогов",
        "accounts": "Связанные WhatsApp-сессии",
        "held": "Сессии на ручной паузе",
        "circuits_open": "Открытые защитные контуры",
        "restricted": "Сессии с ограничением или timelock",
        "unavailable": "Сессии недоступны по GET",
        "state_unknown": "Состояние сессии неизвестно",
        "state_stale": "Проверка состояния старше 30 минут",
        "outbound_allowed": "Разрешено по свежему состоянию и правилам аккаунта",
        "daily_message_limit": "Суточный лимит сообщений по связанным сессиям",
        "daily_new_contact_limit": "Суточный лимит новых контактов по связанным сессиям",
        "max_concurrent_posts": "Лимит одновременных отправок по связанным сессиям",
        "unresolved_reservations": "Неизвестный или незавершённый исход отправки",
        "ingress_accepted_7d": "Принятые webhook события за 7 дней",
        "ingress_ignored_7d": "Игнорированные webhook события за 7 дней",
        "ingress_rejected_7d": "Отклонённые webhook события за 7 дней",
    }
    operational = ""
    if report.operational_metrics:
        rows = "".join(
            f"<tr><td>{_escape(label)}</td><td>{report.operational_metrics.get(key, 0)}</td></tr>"
            for key, label in labels.items()
        )
        operational = (
            "<section><h2>Поиск, предложения и связь</h2><table>" + rows + "</table>"
            "<p>Лид и публичная цена не подтверждают наличие или соответствие товара. "
            "Принято WAHA — подтверждение транспорта. Доставка и прочтение учитываются "
            "по ACK; недоступная история не доказывает отсутствие ответа.</p></section>"
        )
    status_rows = ""
    if report.operational_status:
        account = report.operational_status.get("account", {})
        research = report.operational_status.get("research", {})
        ingress = report.operational_status.get("ingress", {})
        status_values = [
            ("Связанные с заявкой WhatsApp-сессии", account.get("accounts")),
            ("Сессии на ручной паузе", account.get("held")),
            ("Открытые защитные контуры", account.get("circuits_open")),
            ("Сессии с ограничением или timelock", account.get("restricted")),
            ("Сессии недоступны по GET", account.get("unavailable")),
            ("Неоднозначные отправки в резерве", account.get("unresolved_reservations")),
            ("Фактический статус WAHA по последнему GET", account.get("session_status")),
            ("Проверка состояния сессии", account.get("provider_observed_at")),
            ("Следующая проверка аккаунта", account.get("next_probe_at")),
            ("Суточный лимит сообщений аккаунтов", account.get("daily_message_limit")),
            ("Суточный лимит новых контактов", account.get("daily_new_contact_limit")),
            ("Одновременные отправки по аккаунтам", account.get("max_concurrent_posts")),
            ("Состояние поиска", research.get("run_status")),
            ("Последний сохранённый прогресс поиска", research.get("last_real_progress_at")),
            ("Причина ожидания поиска", research.get("wait_reason")),
            ("Следующее пробуждение", research.get("next_wake_at")),
            ("Очередь без активного задания", research.get("queued_without_runnable_job")),
            ("Очередь с завершённым заданием", research.get("queued_with_terminal_job")),
            ("Поиск остановился без задания", research.get("stalled")),
            ("Последнее событие webhook", ingress.get("last_seen_at")),
        ]
        status_rows = "".join(
            f"<tr><td>{_escape(label)}</td><td>{_escape(_display(value))}</td></tr>"
            for label, value in status_values
            if value is not None and value != ""
        )
        if status_rows:
            status_rows = (
                "<section><h2>Состояние автоматизации</h2><table>" + status_rows +
                "</table><p>Состояние WhatsApp отражает последнюю проверку подключения. "
                "Статус ожидания не подтверждает, что поставщик ответил.</p></section>"
            )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{_escape(report.title)}</title><style>{_CSS}</style></head>
<body><main><header><div class="eyebrow">Заявка {_escape(report.case_reference)}</div>
<h1>{_escape(report.title)}</h1>
<p>{_escape(report.city)} · {_escape(report.search_scope)}</p>
<p>Сравнивается только стоимость товара. Доставка учитывается после внесения факта;
НДС отдельно не рассчитывается.</p></header>
<section class="stats"><div><b>{report.suppliers_found}</b><span>найдено</span></div>
<div><b>{report.suppliers_contacted}</b><span>написали в WhatsApp</span></div>
<div><b>{report.suppliers_responded}</b><span>ответили</span></div>
<div><b>{report.exact_matches}</b><span>точных вариантов</span></div>
<div><b>{report.whatsapp_unavailable}</b><span>без подтверждённого WhatsApp</span></div>
<div><b>{report.whatsapp_failed}</b><span>ошибок отправки</span></div></section>
{f'<section class="notice">{_escape(report.notice)}</section>' if report.notice else ""}
{operational}
{status_rows}
<section class="cards">{cards}</section>
<footer>Сформировано: {_escape(report.generated_at.isoformat())}.
Не является заказом, резервом или обязательством купить.</footer></main></body></html>"""


def _ordered_cards(cards: list[SupplierReportCard]) -> list[SupplierReportCard]:
    return sorted(
        cards,
        key=lambda card: (
            card.item_number,
            card.rank is None,
            card.rank or 10**9,
            card.supplier_name.casefold(),
        ),
    )


def _render_groups(cards: list[SupplierReportCard], items: list[ReportItem]) -> str:
    if not cards and not items:
        return '<div class="empty">Предложений пока нет.</div>'
    groups: dict[tuple[int, str], list[SupplierReportCard]] = {}
    for card in cards:
        groups.setdefault((card.item_number, card.item_name), []).append(card)
    item_keys = [(item.item_number, item.item_name) for item in items]
    for key in groups:
        if key not in item_keys:
            item_keys.append(key)
    parts: list[str] = []
    for number, name in item_keys:
        values = groups.get((number, name), [])
        heading = f"Позиция {number}: {name}" if number else (name or "Предложения")
        if not values:
            item = next(
                (item for item in items if item.item_number == number and item.item_name == name),
                None,
            )
            context = ""
            if item is not None:
                required = " ".join(
                    part
                    for part in (
                        str(item.quantity) if item.quantity is not None else "",
                        item.unit or "",
                    )
                    if part
                )
                if required:
                    context += f"<p>Требуется: {_escape(required)}</p>"
                if item.specification_text:
                    context += (
                        "<details><summary>Техспек</summary><p>"
                        f"{_escape(item.specification_text)}</p></details>"
                    )
            parts.append(
                f'<section class="item-group"><h2>{_escape(heading)}</h2>'
                f'<div class="empty">Предложений пока нет.{context}</div></section>'
            )
            continue
        ranked = [card for card in values if card.rank is not None and card.rank <= 5]
        others = [card for card in values if card not in ranked]
        top = (
            "".join(_render_card(card) for card in ranked)
            if ranked
            else '<div class="empty">Подтверждённых сопоставимых цен пока нет.</div>'
        )
        rest = (
            f"<details><summary>Остальные предложения · {len(others)}</summary>"
            + "".join(_render_card(card) for card in others)
            + "</details>"
            if others
            else ""
        )
        parts.append(
            f'<section class="item-group"><h2>{_escape(heading)}</h2>'
            f"<h3>Лучшие подтверждённые предложения</h3>{top}{rest}</section>"
        )
    return "".join(parts)


def _render_card(card: SupplierReportCard) -> str:
    rank = f'<span class="rank">#{card.rank}</span>' if card.rank else ""
    price = "Цена не подтверждена"
    if card.price is not None:
        price = (
            f"{card.price:,.2f} {_escape(card.currency)}"
            f"{f' за {_escape(card.price_unit)}' if card.price_unit else ' · единица не уточнена'}"
        ).replace(",", " ")
    goods = (
        f"<p>Стоимость товара на партию: {_money(card.goods_total, card.currency)}"
        f" · купить: {_escape(card.purchased_quantity)}</p>"
        if card.goods_total is not None and card.purchased_quantity is not None
        else ""
    )
    tender = (
        f"<p>По тендеру: {_money(card.tender_total, card.currency)}; "
        f"разница без доставки: {_money(card.tender_difference, card.currency)}</p>"
        if card.tender_total is not None and card.tender_difference is not None
        else ""
    )
    evidence = "".join(f"<li>{_evidence(value)}</li>" for value in card.evidence)
    questions = "".join(f"<li>{_escape(value)}</li>" for value in card.questions_remaining)
    location = f"<p class=muted>{_escape(card.location)}</p>" if card.location else ""
    phone = ""
    if card.supplier_phone:
        digits = "".join(char for char in card.supplier_phone if char.isdigit())
        wa = (
            f' · <a rel="noreferrer" href="https://wa.me/{digits}">WhatsApp</a>'
            if card.whatsapp_confirmed and len(digits) in {10, 11, 12, 13, 14, 15}
            else ""
        )
        phone = f"<p>Телефон: {_escape(card.supplier_phone)}{wa}</p>"
    return f"""<article>{rank}<h3>{_escape(card.supplier_name)}</h3>{location}{phone}
<h3>{_escape(card.product_name)}</h3><div class="badges">
<span>{_escape(card.technical_status)}</span>
<span>{_escape(card.availability)}</span><span>{_escape(card.price_state)}</span>
</div><p class="price">{price}</p>{goods}{tender}
<h4>Доказательства</h4><ul>{evidence or "<li>Нет сохранённых доказательств</li>"}</ul>
<h4>Что уточнить</h4><ul>{questions or "<li>Критичных уточнений нет</li>"}</ul></article>"""


def _evidence(value: ReportEvidence) -> str:
    if value.url and urlparse(value.url).scheme in {"http", "https"}:
        safe_url = html.escape(value.url, quote=True)
        return f'<a rel="noreferrer" href="{safe_url}">{_escape(value.label)}</a>'
    return _escape(value.label)


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _display(value: object) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def _money(value: Decimal | None, currency: str) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f} {_escape(currency)}".replace(",", " ")


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
.item-group{display:grid;gap:12px;margin:26px 0}.item-group>h2{margin:0}
.item-group>h3{margin:0;color:#697386}.item-group details{display:grid;gap:12px}
.item-group summary{cursor:pointer;padding:12px;background:#fff;border-radius:10px}
.badges{display:flex;gap:8px;flex-wrap:wrap}.badges span{background:#e9f2ff;
padding:5px 9px;border-radius:8px}.price{font-size:24px;font-weight:800}
a{color:#0759b8}footer{padding:24px 4px;font-size:13px}
@media(max-width:600px){.stats{grid-template-columns:1fr}}
"""
