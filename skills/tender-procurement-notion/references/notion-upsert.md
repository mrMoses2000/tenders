# Notion Upsert Discipline

## First Step

Always fetch the relevant database or row before updating. Use the actual schema names returned by Notion.

Common databases:

- `Заявки` - one tender line per row.
- `Предложения` - one dated product offer for one request and one supplier.
- `Поставщики` - reusable directory: one real company/person/store/source identity.
- `Тендера` - one tender/project page.
- `Клиенты` - customer organization.

For the current `Максим` workspace, the actual structure is documented in repository file `docs/notion-maxim-structure.md`. Use it as the map for database names, relation directions, and price fields.

## Request Row Fields

Use these common properties when present:

- `Наименование` - title.
- `Тех. спек` - concise but complete requirement.
- `Количество`
- `Единица измерения`
- `Цена за 1 единицу товара` - tender unit price, not supplier price.
- `Цена каталога, ₸ с НДС` - cheapest technically valid supplier unit price found for the request row after market search.
- `НДС подтверждён` - set `__YES__` only when explicitly proven.
- `Источник товара`
- `Источник фото`
- `Ссылка 2ГИС`
- `Соответствие техспеку`
- `Дата проверки`
- `Поставщики`
- `Доказательства / заметка`
- `Что уточнить`

Keep tender price and supplier price separate. Never overwrite `Цена за 1 единицу товара` with supplier price. Do not use tender price as a search-query constraint. Never fill `Цена каталога, ₸ с НДС` from a wrong-spec offer just because it is cheap or visible.

During supplier research, also preserve the source `Тех. спек`, `Количество`, `Единица измерения`, and tender price. Change them only when the user explicitly corrects the source request.

## Supplier Records

Create supplier records for useful leads only. Include:

- company or seller name;
- category;
- status `Лид` unless already active;
- phone/email/site when available;
- supplier-level notes with identity, reputation, address and operating conditions; item evidence and price assumptions belong in `Предложения`;
- relation to the request row.

In `Максим`, store persistent supplier data when known: `Компания`, `Тип поставщика`, structured select `Город`, optional `Филиал / уточнение`, `БИН / ИИН`, `Телефон`, `WhatsApp`, `Email`, `Сайт`, `2ГИС`, `Адрес`, `Категории товаров`, document/payment capability, reliability, contact/purchase history, and `Общие заметки`.

Do not write product-specific names, listing URLs, technical descriptions, prices, quantities, logistics, conditions, or priority into supplier rows. The corresponding legacy properties were removed from `Максим.Поставщики` on 2026-07-11 and must not be recreated.

For approved expandable dictionaries (`Город`, `Город поставщика`, `Единица цены`), add a missing option when the value is a real normalized city/unit. Do not create options from commentary such as `не подтвержден` or `доставка в Караганду`; keep the select empty and explain that context in notes/evidence.

## Upsert Rules

Before creating a supplier, search existing suppliers by:

1. domain/site URL;
2. exact company name;
3. phone number;
4. obvious alias.

Do not create duplicates for the same supplier just because the product differs. Add the new request relation and update notes if needed.

## Offer Records

Create one `Предложения` row for each request × supplier × dated observation. Reuse an existing row only when it is the same observation and the update adds evidence rather than replacing history.

Maintain when available:

- `Заявка` and `Поставщик` relations;
- supplier item name, `Модель / артикул`, `Фасовка / размер`, and `Единица цены`;
- source type, exact URL, source image, supplier city, pickup point, and checked date;
- supplier unit price, VAT state, minimum order, and confirmed available quantity;
- technical fit, stock, delivery, documents, work stage, priority, and economy;
- forecast logistics and actual allocated logistics;
- compact evidence and exact next supplier question;
- actual purchase fields only after a proven purchase.

Never copy required request quantity into `Количество доступно`. If stock, delivery, documents, or VAT are not proved, use `Не проверено`/`Неизвестно`; do not infer `Нет`, zero, or confirmation.

`Город поставщика` and `Единица цены` are select dictionaries. Add a missing real option to the schema rather than putting commentary into the select. Leave an unknown city empty and explain it in evidence. `Источник фото` must be a direct stable image URL; use `Фото товара` for an uploaded screenshot or supplier image.

Rollup and formula properties are read-only. `Заявки.Выбранное предложение` is set only after the user makes or explicitly confirms the commercial choice; cheapest price alone is not enough.

Keep exactly one selected offer. The `Выбранный ...` rollups copy supplier, source, direct photo URL, technical status, stock, work status, price, logistics, full cost, profit, and profit as a percentage of tender revenue. If no offer is selected, these fields must remain empty. They never overwrite the source `Тех. спек`, required quantity, unit, or tender price.

When updating a request:

- preserve unrelated supplier relations unless the user asks to replace them;
- append all useful supplier candidates to `Поставщики` and create their `Предложения`; do not link only the cheapest or only the first result;
- append or summarize new evidence instead of erasing important history;
- write the current date in `Дата проверки`;
- use a status that matches evidence, not optimism;
- put the call script in `Что уточнить`.
- update `Цена каталога, ₸ с НДС` to the cheapest valid technically matching supplier unit price when one is found, even if it is above tender price; if above tender price, explain the margin problem clearly.

## Evidence Note Pattern

Use a compact note:

```text
Checked YYYY-MM-DD. Need [quantity] of [spec]. Best leads: [supplier/price/source]. Exact match pending because [missing parameter]. Reject/reserve: [reason]. Next action: [call/check/order by user].
```

## One-Line Urgent Update Pattern

For urgent line-item searches, update the row with:

- best source URL;
- cheapest technically valid price lead found, with margin warning if above tender price;
- all useful supplier relations;
- `⚠️ Требует подтверждения` unless stock/delivery/docs are proven;
- evidence note listing all ranked leads;
- call script with quantity, budget, deadline, address, photo request, and document request.

Do not mark `Получен и подтвержден` unless the user says goods were delivered and accepted.
