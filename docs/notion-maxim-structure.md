# Notion structure: Максим

Observed: 2026-07-08

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
| `Поставщики` | `collection://37b4e642-8c82-8003-86f5-000b27ac3de4` | Supplier candidates and real supplier records. Multiple rows may point to one request item. |
| `Грузопервозки` | `collection://3734e642-8c82-8059-8207-000baf7901f8` | Freight and city delivery options. |
| `Камеры хранения` | `collection://3734e642-8c82-805e-b5cd-000b8716d798` | Storage/warehouse points linked to freight. |

Child page:

- `План разговора с Зауре Коргашевной — Караганда_1`

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
| `📝 Заявка` | relation to `Заявки` | Link supplier candidate to every relevant request line. |
| `Цена за единицу` | number | Supplier unit price for the candidate item. |
| `Кол-во` | number | Confirmed/visible quantity or quantity being quoted. |
| `Сумма товара` | number | `Цена за единицу × Кол-во` when known. |
| `Логистика` | number | Delivery/logistics cost when known. |
| `Итого с логистикой` | number | Total purchase cost with logistics when known. |
| `Цена / условия` | text | Human-readable price, payment, quantity and document notes. |
| `Приоритет` | select | `1 - лучший`, `2 - резерв`, `3 - только если подтвердят`, `4 - невыгодно / риск`. |
| `Статус` | status | Usually `Лид` until confirmed/active. |
| `Город` | select | Supplier/source city. |
| `Категория` | select | Category if obvious. |
| `Сайт` | URL | Product page, website, marketplace listing or search result. |
| `2ГИС` | URL | Store identity/location proof. |
| `Телефон` | phone | Supplier phone if available. |
| `Email` | email | Supplier email if available. |
| `Адрес` | text | Supplier address when known. |
| `Контактное лицо` | text | Contact person. |
| `Заметки` | text | Technical fit, stock assumptions, risks and next questions. |

## Mandatory workflow

When working inside `Максим -> Заявки`:

1. Fetch the `Максим` page and actual schemas if they are not already known in the current context.
2. Fetch the target request row or view before researching.
3. Build the item fingerprint from `Тех. спек`, `Количество`, `Единица измерения`, `Цена за 1 единицу товара`, `Город`, `Тендер`, customer/delivery context and existing notes.
4. Search by product/specification first. Do not include the tender price or price interval in search queries unless the user explicitly asks. Price is compared after candidates are collected and checked against the technical spec.
5. Create or update several useful supplier candidates in `Поставщики`.
6. Link every useful candidate to the request through both relation sides:
   - `Поставщики.📝 Заявка`;
   - `Заявки.Поставщики`.
7. Preserve existing supplier relations unless they are duplicates or the user explicitly asks to replace them.
8. Set `Заявки.Цена каталога, ₸ с НДС` to the cheapest supplier unit price that matches hard technical requirements.
9. Do not use a wrong-spec or unresolved analogue to fill the request's main price. Put it in `Поставщики` as reserve/risk and explain in notes.
10. Update `Доказательства / заметка`, `Что уточнить`, `Дата проверки`, `Источник товара`, `Ссылка 2ГИС`, and `Соответствие техспеку` from evidence.

## Supplier count rule

Default expectation for one line item:

- at least 3 useful supplier candidates when the market has enough options;
- at least 1 primary in-budget candidate when possible;
- at least 1 reserve candidate;
- if fewer than 3 are found, write exactly which sources were exhausted.

For urgent rescue tasks, follow the user's requested minimum. If the user says "не менее 10 магазинов", create/reuse at least 10 supplier/source rows when genuinely useful, and link them to the request line.

## Price and profit rule

The work is not to find "anything available". The work is to find technically valid supply that preserves tender profit.

Rules:

- Tender unit price lives in `Заявки.Цена за 1 единицу товара`.
- Supplier candidate price lives in `Поставщики.Цена за единицу`.
- Cheapest valid request price found lives in `Заявки.Цена каталога, ₸ с НДС`.
- Tender unit price is used for margin comparison, not to limit search queries.
- Prefer supplier candidates below tender unit price, but if all technically valid candidates are above tender unit price, still record the cheapest valid market price and write the margin problem clearly.
- Do not call a candidate "best" only because it is cheap. Wrong technical spec means not usable.

## Common failure modes to avoid

- Creating only one supplier row for an item when several options exist.
- Writing supplier information only in `Доказательства / заметка` instead of `Поставщики`.
- Forgetting to link supplier rows back to `Заявки.Поставщики`.
- Filling `Цена каталога, ₸ с НДС` with an over-budget or wrong-spec offer.
- Searching by item name only and ignoring `Тех. спек`.
- Treating Kaspi/OLX/Instagram price as confirmed stock without date, city, quantity and delivery evidence.
- Replacing old supplier relations without preserving context.
