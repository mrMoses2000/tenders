# Notion Upsert Discipline

## First Step

Always fetch the relevant database or row before updating. Use the actual schema names returned by Notion.

Common databases:

- `Заявки` - one tender line per row.
- `Поставщики` - one supplier lead per real company/person/source.
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

## Supplier Records

Create supplier records for useful leads only. Include:

- company or seller name;
- category;
- status `Лид` unless already active;
- phone/email/site when available;
- notes with city, address, source evidence, stock/price assumptions, and next questions;
- relation to the request row.

In `Максим`, supplier candidates belong in the `Поставщики` database, not only in the request notes. Fill these fields when known: `Компания`, `📝 Заявка`, `Цена за единицу`, `Кол-во`, `Сумма товара`, `Логистика`, `Итого с логистикой`, `Цена / условия`, `Приоритет`, `Статус`, `Город`, `Категория`, `Сайт`, `2ГИС`, `Телефон`, `Адрес`, `Заметки`.

If a select option such as city is missing, omit the select and write the city in notes. Do not create schema options unless the user asks.

## Upsert Rules

Before creating a supplier, search existing suppliers by:

1. domain/site URL;
2. exact company name;
3. phone number;
4. obvious alias.

Do not create duplicates for the same supplier just because the product differs. Add the new request relation and update notes if needed.

When updating a request:

- preserve unrelated supplier relations unless the user asks to replace them;
- append all useful supplier candidates to `Поставщики`; do not link only the cheapest or only the first result;
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
