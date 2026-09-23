# Logistics And Risk

## City Search Order

Search locally first, then nearby logistics hubs.

- Караганда: Караганда, Темиртау, Астана, then Алматы only for large batches.
- Туркестан: Туркестан, Шымкент, Тараз, then Алматы for large batches.
- Шымкент: Шымкент, Туркестан, Тараз, Алматы.
- Тараз: Тараз, Шымкент, Алматы.
- Петропавловск: Петропавловск, Кокшетау, Астана.
- Актобе: Актобе, Уральск/Aтырау if route makes sense, then Астана/Алматы.

For small cheap items, a local slightly higher price can beat cheaper intercity stock after delivery risk.

## Delivery Checks

For every supplier lead, determine:

- self-delivery, pickup, taxi/courier, Gazelle, or transport company;
- delivery price;
- delivery deadline;
- loading/unloading responsibility;
- whether a passenger car is enough;
- whether goods are fragile, heavy, bulky, or need protection;
- whether several tender lines can share one delivery.

Do not assume free delivery.

## Dangerous Substitutions

Never silently substitute:

- `гипс для лепки` with construction gypsum;
- `шпатлевка` with plaster or leveling compound;
- `финишная` with starting compound;
- `25 кг` with 5/10/30 kg without explicit recalculation and approval;
- `100 мл`, `140 мл`, `310 мл` with a different volume;
- price per kg with price per bag/unit;
- color number from one catalog with the same number from another catalog;
- search result page with a concrete product card;
- Instagram post without current stock/price with a confirmed offer.

When a brand is rebranded, preserve the old requirement and record the mapping:

```text
Old required: [brand/model/color]
Supplier says current name: [new brand/model]
Evidence: [source]
Acceptance risk: [low/medium/high]
Needs customer approval: yes/no
```

## Budget Handling

If the user gives a hard unit price ceiling, classify any lead above it as `expensive reserve`, not a primary solution.

If all exact matches are over budget, report:

- cheapest exact over-budget lead;
- cheapest plausible under-budget lead and why it is not exact;
- question needed from customer or supplier.

## Same-Day Risk

Same-day leads require explicit confirmation of:

- stock at the local branch;
- quantity available now;
- payment path the user can use;
- pickup/delivery cutoff time;
- driver/courier availability;
- customer receiving time.

If not confirmed, status remains `⚠️ Требует подтверждения`.
