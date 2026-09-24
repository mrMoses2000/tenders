"""Audit and import the Notion export of Максим / Караганда_1.

Dry-run is the default. The complete archive is read locally and never added
to Git. Apply is one PostgreSQL transaction and uses Notion page IDs as stable
identities. Imported movement and acceptance values remain source assertions.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

KARAGANDA_TENDER_ID = "37b4e6428c828004935ff95b4b1787f1"
PAGE_ID = re.compile(r" ([0-9a-f]{32})\.md$")
RELATION_ID = re.compile(r"([0-9a-f]{32})(?:\.md|\.csv)")
PROPERTY = re.compile(r"^([^:\n]+): (.*)$", re.MULTILINE)
LINK_COLUMN = {
    "customer": "customer_id",
    "case": "case_id",
    "item": "request_item_id",
    "supplier": "supplier_id",
    "offer": "offer_id",
}


@dataclass(frozen=True)
class Page:
    page_id: str
    category: str
    title: str
    fields: dict[str, str]

    @property
    def url(self) -> str:
        return f"https://app.notion.com/{self.page_id}"


def relation_ids(value: str) -> tuple[str, ...]:
    return tuple(RELATION_ID.findall(value))


def number(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    return Decimal(value.replace("\u00a0", "").replace(" ", "").replace(",", "."))


def digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def observed_at(value: str) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return datetime.strptime(value, "%B %d, %Y").replace(tzinfo=UTC)


def load_pages(archive: Path) -> dict[str, Page]:
    pages: dict[str, Page] = {}
    with ZipFile(archive) as zip_file:
        for path in zip_file.namelist():
            match = PAGE_ID.search(path)
            if match is None:
                continue
            category = next(
                (name for name in ("Тендера", "Заявки", "Поставщики", "Предложения", "Клиенты")
                 if f"/{name}/" in path),
                None,
            )
            if category is None:
                continue
            body = zip_file.read(path).decode("utf-8-sig")
            title = body.splitlines()[0].removeprefix("# ").strip()
            page_id = match.group(1)
            if page_id in pages:
                raise ValueError(f"duplicate page ID in archive: {page_id}")
            pages[page_id] = Page(page_id, category, title, dict(PROPERTY.findall(body)))
    return pages


def select_karaganda(pages: dict[str, Page]) -> tuple[Page, list[Page], list[Page], list[Page]]:
    tender = pages.get(KARAGANDA_TENDER_ID)
    if tender is None or tender.category != "Тендера":
        raise ValueError("Караганда_1 tender page is absent")
    items = sorted(
        (page for page in pages.values() if page.category == "Заявки"
         and KARAGANDA_TENDER_ID in relation_ids(page.fields.get("Тендер", ""))),
        key=lambda page: int(page.fields.get("№", "0")),
    )
    item_ids = {page.page_id for page in items}
    offers = sorted(
        (page for page in pages.values() if page.category == "Предложения"
         and any(item_id in item_ids for item_id in relation_ids(page.fields.get("Заявка", "")))),
        key=lambda page: page.page_id,
    )
    supplier_ids = {
        supplier_id for offer in offers
        for supplier_id in relation_ids(offer.fields.get("Поставщик", ""))
    }
    suppliers = sorted(
        (pages[supplier_id] for supplier_id in supplier_ids if supplier_id in pages),
        key=lambda page: page.page_id,
    )
    issues: list[str] = []
    if len(items) != 65 or len(offers) != 214:
        issues.append(f"unexpected counts: items={len(items)}, offers={len(offers)}")
    if len({int(page.fields.get("№", "0")) for page in items}) != len(items):
        issues.append("duplicate or missing line numbers")
    for offer in offers:
        item_links = relation_ids(offer.fields.get("Заявка", ""))
        supplier_links = relation_ids(offer.fields.get("Поставщик", ""))
        if len(item_links) != 1 or item_links[0] not in item_ids:
            issues.append(f"offer {offer.page_id} has no unique Karaganda item")
        if len(supplier_links) != 1 or supplier_links[0] not in supplier_ids:
            issues.append(f"offer {offer.page_id} has no unique supplier")
    if len(suppliers) != len(supplier_ids):
        issues.append("some offer suppliers are absent from export")
    if issues:
        raise ValueError("; ".join(issues))
    return tender, items, offers, suppliers


def audit(items: list[Page], offers: list[Page], suppliers: list[Page]) -> dict[str, object]:
    offer_item_ids = {relation_ids(page.fields.get("Заявка", ""))[0] for page in offers}
    return {
        "items": len(items),
        "offers": len(offers),
        "suppliers": len(suppliers),
        "items_with_offers": len(offer_item_ids),
        "items_without_offers": len(items) - len(offer_item_ids),
        "offers_with_price": sum(
            number(page.fields.get("Цена предложения")) is not None for page in offers
        ),
        "offer_sources": dict(Counter(page.fields.get("Тип источника", "") for page in offers)),
        "offer_stages": dict(Counter(page.fields.get("Статус работы", "") for page in offers)),
        "missing_units": sum(not page.fields.get("Единица измерения") for page in items),
        "missing_delivered": sum(not page.fields.get("Поставлено всего") for page in items),
        "missing_accepted": sum(not page.fields.get("Принято школой") for page in items),
        "missing_remaining": sum(not page.fields.get("Остаток к закупке") for page in items),
    }


async def link(connection, page: Page, kind: str, local_id: UUID) -> None:
    column = LINK_COLUMN[kind]
    existing = await connection.fetchrow(
        "SELECT entity_kind,customer_id,case_id,request_item_id,supplier_id,offer_id "
        "FROM external_record_links WHERE source_system='notion' AND external_id=$1",
        page.page_id,
    )
    if existing is not None:
        if existing["entity_kind"] != kind or existing[column] != local_id:
            raise ValueError(f"Notion page {page.page_id} maps to a different entity")
        return
    await connection.execute(
        """
        INSERT INTO external_record_links(
            source_system,external_id,entity_kind,customer_id,case_id,
            request_item_id,supplier_id,offer_id,source_url
        ) VALUES ('notion',$1,$2,$3,$4,$5,$6,$7,$8)
        """,
        page.page_id, kind, local_id if kind == "customer" else None,
        local_id if kind == "case" else None,
        local_id if kind == "item" else None,
        local_id if kind == "supplier" else None,
        local_id if kind == "offer" else None, page.url,
    )


async def linked_id(connection, page_id: str, kind: str) -> UUID | None:
    row = await connection.fetchrow(
        """
        SELECT customer_id,case_id,request_item_id,supplier_id,offer_id
        FROM external_record_links
        WHERE source_system='notion' AND external_id=$1 AND entity_kind=$2
        """,
        page_id, kind,
    )
    return row[LINK_COLUMN[kind]] if row is not None else None


async def evidence(connection, page: Page, *, label: str, at: datetime) -> UUID:
    source_hash = digest(page.fields)
    key = f"notion:{label}:{page.page_id}:{source_hash}"
    evidence_id = await connection.fetchval(
        "SELECT id FROM source_evidence WHERE idempotency_key=$1", key
    )
    if evidence_id is not None:
        return evidence_id
    return await connection.fetchval(
        """
        INSERT INTO source_evidence(
            source_type,source_locator,source_url,external_id,title,captured_at,
            content_sha256,raw_payload,idempotency_key
        ) VALUES ('import',$1,$2,$3,$4,$5,$6,$7,$8) RETURNING id
        """,
        page.url, page.fields.get("Источник") or page.url, page.page_id,
        page.title, at, source_hash, page.fields, key,
    )


async def import_supplier(connection, page: Page) -> UUID:
    from procurement_bot.phone import normalize_phone

    supplier_id = await linked_id(connection, page.page_id, "supplier")
    if supplier_id is not None:
        return supplier_id
    kind = page.fields.get("Тип поставщика", "")
    supplier_type = (
        "sole_proprietor" if kind == "ИП" else
        "individual" if kind == "Частное лицо" else
        "market_stall" if kind in ("Павильон / торговая точка", "Рынок / кластер") else
        "business" if kind == "Компания" else "unknown"
    )
    supplier_id = await connection.fetchval(
        "INSERT INTO suppliers(display_name,supplier_type) VALUES ($1,$2) RETURNING id",
        page.fields.get("Компания") or page.title, supplier_type,
    )
    await link(connection, page, "supplier", supplier_id)
    evidence_id = await evidence(connection, page, label="supplier", at=datetime.now(UTC))
    await connection.execute(
        """
        INSERT INTO supplier_aliases(
            supplier_id,alias_type,display_value,normalized_value,source_evidence_id
        ) VALUES ($1,'source_record',$2,$2,$3)
        """,
        supplier_id, page.page_id, evidence_id,
    )
    city = page.fields.get("Город", "")
    address = page.fields.get("Адрес", "")
    if city or address:
        await connection.execute(
            """
            INSERT INTO supplier_locations(
                supplier_id,location_key,city,address_text,source_provider,
                source_evidence_id,confidence
            ) VALUES ($1,$2,$3,$4,'notion',$5,0.6)
            """,
            supplier_id, digest([page.page_id,city,address]), city, address, evidence_id,
        )
    for field, contact_type in (("WhatsApp", "whatsapp"), ("Телефон", "phone")):
        raw = page.fields.get(field, "")
        if not raw:
            continue
        try:
            normalized = normalize_phone(raw)
        except ValueError:
            continue
        await connection.execute(
            """
            INSERT INTO supplier_contacts(
                supplier_id,contact_type,display_value,normalized_value,
                source_evidence_id,confidence
            ) VALUES ($1,$2,$3,$4,$5,0.6) ON CONFLICT DO NOTHING
            """,
            supplier_id, contact_type, raw, normalized, evidence_id,
        )
    return supplier_id


async def import_customer(connection, page: Page, owner: UUID) -> UUID:
    customer_id = await linked_id(connection, page.page_id, "customer")
    if customer_id is not None:
        actual_owner = await connection.fetchval(
            "SELECT owner_user_id FROM customers WHERE id=$1", customer_id
        )
        if actual_owner != owner:
            raise ValueError(f"customer {page.page_id} belongs to another owner")
        return customer_id
    customer_id = await connection.fetchval(
        """
        INSERT INTO customers(owner_user_id,display_name)
        VALUES ($1,$2)
        ON CONFLICT (owner_user_id,display_name) DO UPDATE
        SET display_name=EXCLUDED.display_name RETURNING id
        """,
        owner, page.title,
    )
    await link(connection, page, "customer", customer_id)
    return customer_id


async def import_item(connection, case_id: UUID, page: Page) -> UUID:
    item_id = await linked_id(connection, page.page_id, "item")
    quantity = number(page.fields.get("Количество"))
    if quantity is None or quantity <= 0:
        raise ValueError(f"item {page.page_id} has invalid quantity")
    line_number = int(page.fields["№"])
    if item_id is None:
        item_id = await connection.fetchval(
            """
            INSERT INTO request_items(
                case_id,line_number,name,specification_text,quantity,unit,status
            ) VALUES ($1,$2,$3,$4,$5,$6,'ready') RETURNING id
            """,
            case_id, line_number, page.title,
            page.fields.get("Тех. спек", ""), quantity,
            page.fields.get("Единица измерения", ""),
        )
        await link(connection, page, "item", item_id)
    else:
        row = await connection.fetchrow(
            "SELECT case_id,line_number,name,quantity FROM request_items WHERE id=$1", item_id
        )
        if row is None or row["case_id"] != case_id or row["line_number"] != line_number:
            raise ValueError(f"item {page.page_id} mapping changed")
    price = number(page.fields.get("Цена за 1 единицу товара"))
    if price is not None:
        current = await connection.fetchval(
            "SELECT tender_unit_price_private FROM request_item_financials "
            "WHERE request_item_id=$1", item_id,
        )
        if current is None:
            await connection.execute(
                """
                INSERT INTO request_item_financials(
                    request_item_id,tender_unit_price_private,source_system
                ) VALUES ($1,$2,'notion') ON CONFLICT DO NOTHING
                """,
                item_id, price,
            )
        elif current != price:
            raise ValueError(f"item {page.page_id} tender price changed; review required")
    source_hash = digest(page.fields)
    await connection.execute(
        """
        INSERT INTO request_item_source_snapshots(
            request_item_id,source_system,external_id,source_hash,source_url,captured_at,
            reported_delivered_quantity,reported_accepted_quantity,
            reported_rejected_quantity,reported_remaining_quantity,
            movement_label,reconciliation_label,location_label,raw_payload
        ) VALUES ($1,'notion',$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        ON CONFLICT (source_system,external_id,source_hash) DO NOTHING
        """,
        item_id, page.page_id, source_hash, page.url, datetime.now(UTC),
        number(page.fields.get("Поставлено всего")),
        number(page.fields.get("Принято школой")),
        number(page.fields.get("Отклонено / замена")),
        number(page.fields.get("Остаток к закупке")),
        page.fields.get("Статус", ""), page.fields.get("Доп. статус", ""),
        page.fields.get("Наличие в школе", ""), page.fields,
    )
    return item_id


def offer_status(page: Page) -> str:
    stage = page.fields.get("Статус работы", "")
    if stage in ("Устарело", "Недоступно"):
        return "withdrawn"
    if stage == "Отклонено" or page.fields.get("Соответствие ТЗ", "").startswith("❌"):
        return "mismatch"
    return "lead"


async def import_offer(
    connection, page: Page, item_ids: dict[str, UUID], supplier_ids: dict[str, UUID]
) -> UUID:
    item_page_id = relation_ids(page.fields.get("Заявка", ""))[0]
    supplier_page_id = relation_ids(page.fields.get("Поставщик", ""))[0]
    offer_id = await linked_id(connection, page.page_id, "offer")
    if offer_id is None:
        offer_id = await connection.fetchval(
            """
            INSERT INTO offers(
                request_item_id,supplier_id,supplier_product_name,model_sku,status,
                idempotency_key
            ) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id
            """,
            item_ids[item_page_id], supplier_ids[supplier_page_id],
            page.fields.get("Товар поставщика") or page.title,
            page.fields.get("Модель / артикул", ""), offer_status(page),
            f"notion:offer:{page.page_id}",
        )
        await link(connection, page, "offer", offer_id)
    else:
        row = await connection.fetchrow(
            "SELECT request_item_id,supplier_id FROM offers WHERE id=$1", offer_id
        )
        if (row is None or row["request_item_id"] != item_ids[item_page_id]
                or row["supplier_id"] != supplier_ids[supplier_page_id]):
            raise ValueError(f"offer {page.page_id} relation changed; review required")
    at = observed_at(page.fields.get("Дата проверки", ""))
    evidence_id = await evidence(connection, page, label="offer", at=at)
    price = number(page.fields.get("Цена предложения"))
    available = number(page.fields.get("Количество доступно"))
    stock = page.fields.get("Наличие", "")
    availability = (
        "out_of_stock" if stock == "Нет" else
        "in_stock" if available is not None and stock in ("Есть", "Частично") else
        "in_stock_unquantified" if stock == "Есть" else "unknown"
    )
    documents = {"Да": "available", "Нет": "unavailable"}.get(
        page.fields.get("Документы подтверждены", ""), "unknown"
    )
    delivery = {"Да": "delivery", "Нет": "unavailable"}.get(
        page.fields.get("Доставка подтверждена", ""), "unknown"
    )
    vat = {"С НДС": "included", "Без НДС": "excluded"}.get(
        page.fields.get("Цена и НДС", ""), "unknown"
    )
    level = page.fields.get("Уровень доказательств", "1")[:1]
    await connection.execute(
        """
        INSERT INTO offer_observations(
            offer_id,source_evidence_id,observed_at,observed_by,source_type,
            price_amount,currency,price_unit,pack_qty,moq,order_multiple,
            available_qty,availability_status,vat_status,document_status,
            delivery_status,evidence_level,raw_payload,idempotency_key
        ) VALUES ($1,$2,$3,'notion_export','import',$4,$5,$6,$7,$8,$9,$10,
                  $11,$12,$13,$14,$15,$16,$17)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        offer_id, evidence_id, at, price, "KZT" if price is not None else None,
        page.fields.get("Единица цены") or None,
        number(page.fields.get("Количество в ценовой единице")),
        number(page.fields.get("Минимальный заказ")),
        number(page.fields.get("Кратность заказа")),
        available, availability, vat, documents, delivery,
        int(level) if level.isdigit() and 1 <= int(level) <= 5 else 1,
        page.fields, f"notion:observation:{page.page_id}:{digest(page.fields)}",
    )
    return offer_id


async def apply_import(tender: Page, items: list[Page], offers: list[Page],
                       suppliers: list[Page], pages: dict[str, Page],
                       owner_username: str) -> dict[str, object]:
    from procurement_bot.config import get_settings
    from procurement_bot.db import create_pool

    pool = await create_pool(get_settings().postgres_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", 1_487_391_114)
            owner = await connection.fetchval(
                "SELECT id FROM users WHERE username=$1 AND active=TRUE", owner_username
            )
            if owner is None:
                raise ValueError("active owner username not found")
            customer_page_ids = relation_ids(tender.fields.get("🎓 НИШы", ""))
            customer_id = None
            if customer_page_ids:
                customer_page = pages.get(customer_page_ids[0])
                if customer_page is None or customer_page.category != "Клиенты":
                    raise ValueError("tender customer relation is absent from export")
                customer_id = await import_customer(connection, customer_page, owner)
            case_id = await linked_id(connection, tender.page_id, "case")
            if case_id is None:
                case_id = await connection.fetchval(
                    """
                    INSERT INTO procurement_cases(
                        owner_user_id,title,city,status,source_kind,customer_id
                    ) VALUES ($1,$2,'Караганда','ready','notion_import',$3) RETURNING id
                    """,
                    owner, tender.title, customer_id,
                )
                await link(connection, tender, "case", case_id)
            else:
                actual_owner = await connection.fetchval(
                    "SELECT owner_user_id FROM procurement_cases WHERE id=$1", case_id
                )
                if actual_owner != owner:
                    raise ValueError("existing imported case belongs to another owner")
            item_ids = {page.page_id: await import_item(connection, case_id, page)
                        for page in items}
            supplier_ids = {page.page_id: await import_supplier(connection, page)
                            for page in suppliers}
            for page in offers:
                await import_offer(connection, page, item_ids, supplier_ids)
            return {"case_id": str(case_id), **audit(items, offers, suppliers)}
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="complete Notion Markdown & CSV ZIP")
    parser.add_argument("--apply", action="store_true", help="write one transaction to PostgreSQL")
    parser.add_argument("--owner-username", help="active Telegram owner for imported case")
    args = parser.parse_args()
    pages = load_pages(args.archive)
    tender, items, offers, suppliers = select_karaganda(pages)
    report = audit(items, offers, suppliers)
    report["archive_sha256"] = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    if args.apply:
        if not args.owner_username:
            parser.error("--owner-username is required with --apply")
        report = asyncio.run(apply_import(tender, items, offers, suppliers, pages,
                                          args.owner_username))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
