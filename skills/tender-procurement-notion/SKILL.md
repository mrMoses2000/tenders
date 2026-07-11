---
name: tender-procurement-notion
description: Kazakhstan tender and urgent procurement research with Notion for schools, NIS, public bodies, private companies, contractors, and other organizations. Use when Codex must parse tender specs or Notion заявки, find and compare suppliers on Kaspi, OLX, 2GIS, Satu, Pulscen, Instagram, local supplier sites, validate technical conformity, rank purchase options, draft supplier questions, or update existing Notion tender databases. Never use to place orders, send messages, reserve goods, or pay.
---

# Tender Procurement + Notion

## Mission

Find real purchasable options for Kazakhstan tender/request lines, verify them against the technical specification, and keep the existing Notion workspace auditable.

The customer may be any organization: NIS is a frequent pattern, not a product boundary.

Optimize for this order:

1. technical conformity;
2. real stock and same-day or realistic logistics;
3. traceable supplier identity;
4. lowest valid purchase price found in the market;
5. document/payment practicality;
6. margin after delivery.

Cheap but unverified offers are leads, not solutions.
Wrong-spec offers are not solutions even if they are cheap. Tender price is for comparison and margin calculation, not for narrowing the search query.

## Allowed Work

Allowed without extra approval:

- read tender files, photos, spreadsheets, PDFs, Word docs, and existing Notion records;
- search public websites, Kaspi, OLX, 2GIS, supplier pages, Instagram pages, Satu/Pulscen/Flagma, and local shop catalogs;
- create or update Notion pages, properties, relations, source links, notes, and draft supplier questions;
- prepare call/message scripts and ranked supplier lists.

Never do without explicit approval in the same conversation:

- send WhatsApp, email, OLX, Kaspi, Instagram, website-form, or SMS messages;
- call, request callbacks, reserve, add to cart, order, pay, confirm terms, or share personal/payment data;
- use passwords, 2FA codes, cards, bank details, or account security settings.

If a site reaches login, payment, reservation, order confirmation, callback, or message-send state, stop and report the exact checkpoint.

## Operating Modes

### Urgent Single-Line Rescue

Use this when the user says a contract is at risk, needs delivery today, or names one item from an existing Notion request.

1. Fetch the Notion row first when available.
2. Extract the hard fingerprint: name, quantity, unit, exact size/fascia/color/model, tender price, delivery city, customer address if known.
3. Search local city first, then nearby cities, then Kazakhstan-wide only if same-day/local fails.
4. Produce at least the requested number of supplier leads; if not possible, explain which routes were exhausted.
   - Default minimum is 5 useful supplier candidates per line when the market has enough options.
   - If the user asks for a minimum such as 10 stores, satisfy that minimum with real useful leads or state why it is impossible.
5. Separate leads into:
   - `buy first` - likely in budget and locally actionable;
   - `call next` - plausible but missing stock/price/delivery;
   - `expensive reserve` - available but over budget;
   - `do not use` - fails technical spec.
6. Create/reuse the company in `Поставщики`, then create one `Предложения` row per request × supplier × dated observation. Keep the compatibility relation `Заявки.Поставщики`, then update source, date, lowest valid price lead, evidence note, and exact call script.
7. Give the user a short ranked call list, not a long essay.

### Full Tender Intake

Use this when the user gives a file/photo/specification with multiple lines.

1. Reconstruct every line item.
2. Recalculate line totals and the grand total.
3. Detect duplicate item names from different suppliers, organizations, sites, schools, buildings, or departments; do not merge unless the user or source says to aggregate.
4. Create or update Notion tender, request, and supplier records.
5. Search and validate suppliers line by line. Search from the item/specification only; do not put the tender price or budget interval into search queries. After collecting candidates, compare technical fit and choose the cheapest valid offer.
6. Report validated subtotal only for fully verified lines; keep pending lines separate.

### Notion Cleanup Or Update

Use this when the user asks to correct rows, supplier relations, statuses, delivery notes, or ordering.

1. Fetch database schemas first.
2. Reuse existing pages and suppliers when possible.
3. Do not delete or overwrite unrelated rows.
4. Preserve old supplier relations unless the user asks to replace them.
5. Record what changed in `Доказательства / заметка` or `Что уточнить`.

## Tool Order

1. Notion app/tool for existing database schema, row state, and updates.
2. Chrome or Computer Use for dynamic/logged-in pages such as Kaspi, OLX, 2GIS, Instagram, or visible UI interactions.
3. Browser/public web for static pages and broad search.
4. Local scripts in this skill for candidate checks and report shaping.

When the repository deterministic core is available, use it for unit normalization, duplicate detection, arithmetic, delivery allocation, stable payload generation, and public HTML extraction before spending model or visual-browser work on the same deterministic task. It never replaces evidence judgment or performs a Notion write.

Prefer source evidence over memory. Current prices, stock, contacts, and delivery promises must be freshly checked.

## Technical Validation

Each candidate must get one of these statuses:

- `✅ Совпадает` - every mandatory requirement is proven by source evidence.
- `⚠️ Требует подтверждения` - likely acceptable but stock, quantity, price, delivery, documents, or a hard parameter is missing.
- `❌ Не использовать без разъяснения` - a mandatory parameter differs or acceptance risk is high.
- `⛔ Точное решение не найдено` - enough search was done and no trustworthy solution exists.

Hard parameters include model/article, purpose, size, volume, weight, color number, set composition, material, voltage/current, count per package, destination/site allocation, and delivery deadline.

If the source says `310 мл` and the candidate is `280 мл`, or the tender says `25 кг` and the candidate is priced per kg, do not call it exact.

## Notion Rules

Read `references/notion-upsert.md` before creating or updating Notion records in a tender database.

For the `Максим` workspace path, also follow `docs/notion-maxim-structure.md` from the `/Users/mosesvasilenko/tenders` repository when available. If you do not have that file in context, fetch the `Максим` page and current database schemas before editing.

The source request is immutable during supplier research: do not change `Тех. спек`, `Количество`, `Единица измерения`, or `Цена за 1 единицу товара` unless the user explicitly corrects the tender source.

Default request fields to maintain:

- `Источник товара`
- `Источник фото`
- `Соответствие техспеку`
- `Цена каталога, ₸ с НДС`
- `НДС подтверждён`
- `Дата проверки`
- `Поставщики`
- `Ссылка 2ГИС`
- `Доказательства / заметка`
- `Что уточнить`

Supplier records should be created only for real leads with a name/site/phone/page and connected to the relevant request row. If the supplier city option does not exist in Notion, omit the select property and put the city in notes.

`Поставщики` is a reusable company directory. Store persistent identity, contacts, categories, document/payment capability, reliability, and purchase history there. Do not put a product-specific name, URL, technical description, price, quantity, logistics estimate, conditions, or priority into supplier data; those belong to `Предложения`. The product-shaped legacy fields were removed from the `Максим` schema on 2026-07-11 and must not be recreated.

For each researched request line, create or reuse several supplier candidates when available and create a separate `Предложения` row for every useful candidate. Link each offer to exactly one `Заявка` and one real `Поставщик` whenever supplier identity is known. Keep `Заявки.Поставщики` populated during the compatibility period. Do not leave options only in notes.

For every offer:

- preserve the seller's model/article and packaging separately from the price unit;
- use independent statuses for technical fit, stock, delivery, documents, work stage, VAT, and economy;
- write `Количество доступно` only when the source or supplier actually proves stock quantity; required tender quantity is not stock;
- represent missing evidence as `Не проверено` or `Неизвестно`, never as `Нет` or zero;
- store forecast logistics separately from actual allocated logistics;
- treat formula and rollup fields as read-only;
- record actual price, quantity, delivery share, and purchase date only from a user-reported or otherwise proven fact;
- create a new dated observation when price or terms materially change instead of erasing useful history.

Do not set `Заявки.Выбранное предложение` automatically just because an offer is cheapest. Selection is a commercial decision and requires the user's choice after technical fit, availability, documents, delivery, and full cost are understood.

When the user selects an offer, keep exactly one page in `Заявки.Выбранное предложение`. Selected supplier, source, photo URL, technical status, stock, work status, price, logistics, cost, profit, and profit percentage are read-only rollups from that relation. Original request name/specification/quantity/unit/tender price never come from an offer.

Set `Цена каталога, ₸ с НДС` in the request row to the lowest valid supplier unit price found after technical validation. Do not fill this field from a wrong-spec analogue. If the cheapest technically valid option is above the tender price, still record it as the market result, but clearly explain the margin problem in supplier notes and `Доказательства / заметка`.

## References

Load only the relevant file:

- `references/search-and-validation.md` - detailed supplier-search matrix and candidate evidence rules.
- `references/notion-upsert.md` - Notion schema mapping and upsert discipline.
- `references/logistics-and-risk.md` - city search order, delivery rules, and dangerous substitutions.
- `references/messages.md` - supplier question drafts; draft only unless the user explicitly approves sending.
- `references/deterministic-core.md` - local deterministic helpers, safe public HTML extractor, and command examples.
- Repository doc `docs/notion-maxim-structure.md` - actual `Максим` Notion map and mandatory relation/price workflow.

Use `scripts/check_candidates.py` when you have a CSV/JSON shortlist and need fast price/quantity/status checks.

## Final Response

For urgent procurement, answer with:

1. what Notion row was updated;
2. top 3-5 suppliers to call first;
3. price range and budget fit;
4. exact script/questions for the supplier;
5. blockers that still require user confirmation.

For full tender work, report created/updated records, exact matches, pending confirmations, rejected substitutions, and validated subtotal. Never claim an order is placed unless the user did it and told you.
