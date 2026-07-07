# Tender Agent Pipeline

## 1. Intake

Identify whether the request is:

- full tender packet;
- existing Notion tender update;
- urgent single-line procurement;
- supplier/status cleanup;
- delivery/acceptance reconciliation.

Fetch Notion first when the user points to an existing workspace path such as `Максим -> Заявки -> Караганда_1`.

## 2. Build The Fingerprint

For each line item, extract:

- item name;
- exact technical spec;
- quantity;
- unit;
- tender unit price;
- tender line total;
- city and destination;
- hard gates: size, volume, weight, article, model, color, material, purpose, package count.

Normalize duplicate-looking names carefully. Same item name from different suppliers or different locations is not automatically the same tender line.

## 3. Search

Search local suppliers first:

1. Notion existing suppliers.
2. 2GIS businesses and linked websites.
3. Local stores and branch pages.
4. Kaspi with city awareness.
5. OLX city/category.
6. Satu, Pulscen, Flagma.
7. Instagram pages connected to real stores.
8. Nearby city route.

For urgent work, keep expanding until the requested minimum number of leads is found or all sensible routes are exhausted.

## 4. Validate

Compare candidate to fingerprint:

- exact match;
- pending supplier confirmation;
- do not use without clarification;
- no verified solution.

Keep source price separate from tender price. Do not label unverified marketplace prices as confirmed procurement price.

## 5. Notion Update

Update the row with:

- current date;
- best source URL;
- source photo URL if useful;
- match status;
- supplier relations;
- evidence note;
- exact call/message script;
- price lead if visible and relevant.

Create supplier records only for useful leads. Do not create duplicate suppliers.

## 6. User Handoff

For urgent requests, give:

- top ranked suppliers;
- price and budget fit;
- what to say on the call;
- exact blockers;
- what was updated in Notion.

For full tenders, give:

- records created/updated;
- exact/pending/rejected counts;
- validated subtotal;
- pending supplier/customer questions.
