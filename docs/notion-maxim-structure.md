# Notion structure: Максим

Observed: 2026-07-11

This document maps the working Notion area used for procurement requests.

Root page:

- `Максим`
- URL: `https://app.notion.com/p/3734e6428c8280b2a4adfe1eb12cfb89`
- Parent: `Moses`

## Child databases

| Database | Data source | Purpose |
|---|---|---|
| `Тендера` | `collection://37b4e642-8c82-8009-951e-000ba16ea48f` | Tender/project header: city, customer relation, total, status, row count. |
| `Заявки` | `collection://3734e642-8c82-80fb-a95c-000b05301c9f` | One line item per tender/request row. This is the main work table. |
| `Клиенты` | `collection://3734e642-8c82-80f7-b2cc-000be68fe7b8` | Customer/destination organizations, addresses and contacts. |
| `Поставщики` | `collection://37b4e642-8c82-8003-86f5-000b27ac3de4` | Reusable directory of real companies, sellers, stores and market points. Product observations never live here. |
| `Предложения` | `collection://30f4933a-6bf6-4c2f-bf52-19304b4a8231` | One concrete offer/observation for one request line and one supplier. Stores item-specific price, logistics, evidence and readiness. |
| `Архив legacy-поставщиков` | `collection://87154a63-c650-4e20-99b5-be2048537a7c` | Soft-deleted product-shaped supplier cards removed during migration 1.6. Never use for new procurement. |
| `Грузопервозки` | `collection://3734e642-8c82-8059-8207-000baf7901f8` | Freight and city delivery options. |
| `Камеры хранения` | `collection://3734e642-8c82-805e-b5cd-000b8716d798` | Storage/warehouse points linked to freight. |

Child page:

- `План разговора с Зауре Коргашевной — Караганда_1`

Migration state on 2026-07-11:

- the six pilot offers for №23 and six for №61 are detached and marked `Устарело`; both lines are ready for fresh research;
- all 31 unfinished `Караганда_1` rows have empty research/output fields and no selected offer;
- all 35 delivered rows retain `Статус = Доставлен в школу`;
- 67 product-shaped legacy supplier cards are outside the working directory in `Архив legacy-поставщиков`;
- supplier product fields and the duplicate single-select `Категория` have been deleted;
- views in `Предложения`: `Все активные`, `Связаться`, `Экономика`, `Статусы`, `Закуплено / история`.
- Formula/rollup economics and a user-controlled selected-offer relation are active.
- Existing direct `Заявки ↔ Поставщики` relations remain during the compatibility period.

## `Заявки`

Important views:

- `Караганда_1`: filtered by `Город = Караганда`, sorted by `№` ascending.
- `Туркестан`
- `Актюбинск`
- `НИШ Туркестан — 2026`

Core properties:

| Property | Type | How to use |
|---|---|---|
| `№` | number | Display/order number. Preserve existing order. |
| `Наименование` | title | Short item name from tender/spec. |
| `Тех. спек` | text | Hard technical requirement. Search must be based on this first. |
| `Количество` | number | Required quantity. |
| `Единица измерения` | select | Unit from tender. |
| `Цена за 1 единицу товара` | number | Tender unit price. Do not overwrite with supplier price. |
| `Цена за общее кол-во` | formula | Tender line total. Read-only. |
| `Цена каталога, ₸ с НДС` | number | Cheapest technically valid supplier unit price found for the item after market search. |
| `Цена закупки, ₸ без НДС` | formula | Read-only calculation from catalog/source price. |
| `Сумма закупки, ₸ без НДС` | formula | Read-only calculation. |
| `Разница до логистики, ₸` | formula | Read-only margin before logistics. |
| `Поставщики` | relation to `Поставщики` | Must contain all useful supplier candidates for this line, not only the winner. |
| `Предложения` | relation to `Предложения` | All dated offers/observations researched for this line. |
| `Количество предложений` | rollup | Count of related offers. Read-only. |
| `Выбранное предложение` | relation to `Предложения` | Commercial choice explicitly made/confirmed by the user. Do not auto-select by lowest price. |
| `Выбранная цена` | rollup | Selected offer price. Read-only. |
| `Выбранная логистика прогноз` | rollup | Selected forecast logistics. Read-only. |
| `Выбранная себестоимость прогноз` | rollup | Selected full forecast cost. Read-only. |
| `Выбранная прибыль прогноз` | rollup | Selected forecast profit. Read-only. |
| `Выбранная прибыль, % от выручки` | rollup | Selected forecast profit divided by tender revenue. Read-only. |
| `Выбранная прибыль факт, % от выручки` | rollup | Selected actual profit divided by tender revenue. Read-only. |
| `Выбранный поставщик` | rollup | Supplier from the user-selected offer. Read-only. |
| `Выбранный источник` | rollup | Source URL from the user-selected offer. Read-only. |
| `Выбранное фото (URL)` | rollup | Direct image URL from the user-selected offer. Read-only. |
| `Выбранное соответствие ТЗ` | rollup | Technical status from the selected offer. Read-only. |
| `Выбранное наличие` | rollup | Stock state from the selected offer. Read-only. |
| `Выбранный статус работы` | rollup | Work status from the selected offer. Read-only. |
| `Выбранная фактическая цена` | rollup | Selected actual purchase price. Read-only. |
| `Выбранная себестоимость факт` | rollup | Selected actual full cost. Read-only. |
| `Выбранная прибыль факт` | rollup | Selected actual profit. Read-only. |
| `Соответствие техспеку` | select | Evidence-based match status. |
| `Источник товара` | URL | Best current product/source URL for the primary candidate. |
| `Ссылка 2ГИС` | URL | Store identity/location proof when useful. |
| `Дата проверки` | date | Freshness of latest research/update. |
| `Доказательства / заметка` | text | Compact evidence summary and ranking. |
| `Что уточнить` | text | Exact supplier/customer call script/questions. |
| `Статус` | status | Delivery/load status. Do not mark delivered unless user/source says so. |
| `Статус сверки` | select | Acceptance/reconciliation status. |
| `Фото товара` | file | Product photo/attachment. |
| `Источник фото` | URL | External photo source. |
| `Тендер` | relation to `Тендера` | Tender/project header. |
| `Город` | select | Delivery city. |

Known status values:

- `Соответствие техспеку`: `✅ Совпадает`, `⚠️ Требует подтверждения`, `❌ Не использовать без разъяснения`, `⛔ Точное решение не найдено`.
- `Статус`: `Не загружен`, `Доставлен в школу`, `Доставлен на склад`, `В пути`, `Получен и подтвержден`.

Observed `Караганда_1` numbering:

- Rows `№1-8` are from `ИП БАЙЖИГИТОВА А,А`.
- Rows starting with `№9` include the later Karaganda/Bastaubayeva work.
- Do not renumber or reorder by supplier unless the user explicitly asks. Preserve view order by `№`.

## `Поставщики`

Core properties:

| Property | Type | How to use |
|---|---|---|
| `Компания` | title | Supplier/company/source name. Use a concrete source name. |
| `📝 Заявка` | relation to `Заявки` | Compatibility relation to relevant request lines. |
| `Предложения` | relation to `Предложения` | Concrete dated product offers from this supplier. |
| `Тип поставщика` | select | Company, marketplace seller, store, market/pavilion, original counterparty, etc. |
| `Город` | select | Structured main supplier city; options are extended as real cities appear. |
| `Филиал / уточнение` | text | Branch/address clarification or explanation that shipment city is unknown. |
| `БИН / ИИН` | text | Legal identifier when proven. |
| `WhatsApp` | phone | Direct WhatsApp-capable contact when known. |
| `Категории товаров` | multi-select | Reusable sourcing categories. |
| `Работа с документами` | select | Document capability state. |
| `Виды документов` | multi-select | Invoice, receipt, waybill, VAT invoice, etc., only when proven. |
| `Способы оплаты` | multi-select | Supported payment methods when proven. |
| `Надёжность` | select | Evidence-based supplier reliability. |
| `Дата последнего контакта` | date | Latest real interaction. |
| `Дата последней закупки` | date | Latest proven purchase. |
| `Успешных закупок` | number | Count of proven successful purchases. |
| `Причина не работать` | text | Durable exclusion reason, if any. |
| `Статус` | status | Usually `Лид` until confirmed/active. |
| `Сайт` | URL | Stable supplier/company/store home page. Exact product/listing URL belongs in `Предложения.Источник`. |
| `2ГИС` | URL | Store identity/location proof. |
| `Телефон` | phone | Supplier phone if available. |
| `Email` | email | Supplier email if available. |
| `Адрес` | text | Supplier address when known. |
| `Контактное лицо` | text | Contact person. |
| `Общие заметки` | text | Supplier-level reputation and operating notes only; no item price or technical description. |

The item fields `Цена за единицу`, `Кол-во`, `Сумма товара`, `Логистика`, `Итого с логистикой`, `Цена / условия`, `Приоритет` and duplicate `Категория` were removed in migration 1.6. Any attempt to recreate them in `Поставщики` is a schema regression. Store those facts in `Предложения`.

## `Предложения`

One row represents one request × supplier × dated observation. A price/terms change should normally create a new observation rather than erase useful history.

Key field groups:

| Group | Properties and rule |
|---|---|
| Identity | `Предложение`, one `Заявка`, one `Поставщик` when identity is known, `Товар поставщика`, `Модель / артикул`, `Фасовка / размер`. |
| Evidence | `Тип источника`, `Источник`, direct `Источник фото`, attachment `Фото товара`, `Дата проверки`, `Доказательства`, `Что уточнить`. |
| Price | `Цена предложения`, select `Единица цены`, `Цена и НДС`, `Минимальный заказ`. |
| Availability | `Наличие`, `Количество доступно`, `Срок / дата доступности`. Required tender quantity is never copied into available quantity. |
| Fulfilment | select `Город поставщика`, `Способ получения`, `Точка отгрузки / самовывоза`, `Прогноз логистики`, `Доставка подтверждена`. |
| Validation | `Соответствие ТЗ`, `Документы подтверждены`, `Статус работы`, `Приоритет`. Unknown evidence stays `Не проверено`/`Неизвестно`. |
| Forecast | rollups `Требуемое количество`, `Тендерная цена за единицу`; formulas for goods cost, full cost, profit, profit as % of revenue, and `Экономика`. Read-only. |
| Actual | `Фактическая цена`, `Фактическое количество`, `Доля фактической логистики`, `Дата закупки`; formulas for actual cost/profit/profit as % of revenue. Fill only from proven facts. |

Independent states must not be collapsed into one optimistic status. An offer can match the technical spec while stock, documents, delivery, or VAT remain unknown.

## Mandatory workflow

When working inside `Максим -> Заявки`:

1. Fetch the `Максим` page and actual schemas if they are not already known in the current context.
2. Fetch the target request row or view before researching.
3. Build the item fingerprint from `Тех. спек`, `Количество`, `Единица измерения`, `Цена за 1 единицу товара`, `Город`, `Тендер`, customer/delivery context and existing notes.
4. Search by product/specification first. Do not include the tender price or price interval in search queries unless the user explicitly asks. Price is compared after candidates are collected and checked against the technical spec.
5. Create or reuse persistent company identities in `Поставщики`.
6. Create a `Предложения` row for each useful supplier observation and link it to exactly one request and one supplier when known.
7. Keep every useful candidate connected through the compatibility relations:
   - `Поставщики.📝 Заявка`;
   - `Заявки.Поставщики`.
8. Preserve existing supplier relations unless they are duplicates or the user explicitly asks to replace them.
9. Set `Заявки.Цена каталога, ₸ с НДС` to the cheapest supplier unit price that matches hard technical requirements.
10. Do not use a wrong-spec or unresolved analogue to fill the request's main price. Keep it as a reserve/risk offer and explain the issue.
11. Update request evidence fields from the ranked offer set.
12. Set `Выбранное предложение` only after the user explicitly chooses/confirms it; never infer a winner from price alone.

## Supplier count rule

Default expectation for one line item:

- at least 5 useful supplier candidates when the market has enough options;
- at least 1 primary in-budget candidate when possible;
- at least 1 reserve candidate;
- if fewer than 5 are found, write exactly which sources were exhausted.

For urgent rescue tasks, follow the user's requested minimum. If the user says "не менее 10 магазинов", create/reuse at least 10 supplier/source rows when genuinely useful, and link them to the request line.

## Price and profit rule

The work is not to find "anything available". The work is to find technically valid supply that preserves tender profit.

Rules:

- Tender unit price lives in `Заявки.Цена за 1 единицу товара`.
- Supplier candidate price lives in `Предложения.Цена предложения`.
- Cheapest valid request price found lives in `Заявки.Цена каталога, ₸ с НДС`.
- Tender unit price is used for margin comparison, not to limit search queries.
- Prefer supplier candidates below tender unit price, but if all technically valid candidates are above tender unit price, still record the cheapest valid market price and write the margin problem clearly.
- Do not call a candidate "best" only because it is cheap. Wrong technical spec means not usable.

## Common failure modes to avoid

- Creating only one supplier row for an item when several options exist.
- Writing supplier information only in notes instead of `Поставщики` and `Предложения`.
- Forgetting to link supplier rows back to `Заявки.Поставщики`.
- Copying required quantity into `Количество доступно` without stock evidence.
- Encoding unknown stock, delivery, documents, or VAT as `Нет`.
- Putting a product name or item-specific price into the reusable supplier identity.
- Auto-selecting the cheapest offer without user confirmation and readiness checks.
- Filling `Цена каталога, ₸ с НДС` with an over-budget or wrong-spec offer.
- Searching by item name only and ignoring `Тех. спек`.
- Treating Kaspi/OLX/Instagram price as confirmed stock without date, city, quantity and delivery evidence.
- Replacing old supplier relations without preserving context.
