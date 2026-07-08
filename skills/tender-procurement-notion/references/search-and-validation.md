# Search And Validation

## Candidate Evidence Fields

Record every lead with:

- source type: `2GIS`, `Kaspi`, `OLX`, `supplier site`, `Satu`, `Pulscen`, `Flagma`, `Instagram`, `marketplace`, `phone lead`;
- supplier name;
- city and pickup/delivery area;
- contact phone or contact page;
- exact product title;
- source URL;
- product photo URL when available;
- price and whether it is per unit, per kg, per bag, per meter, per set, or wholesale;
- stock wording and checked date;
- minimum order quantity;
- delivery option and delivery price when known;
- VAT/document status;
- technical match status;
- next question.

## Search Matrix

Search by the hard technical specification first. Do not start from the cheapest visible price and then force-fit the item to the tender line.

For every item, search in this order unless the user says otherwise:

1. existing Notion suppliers linked to the same tender/city;
2. 2GIS category and product search in the destination city;
3. local shop sites opened from 2GIS;
4. Kaspi in the destination city;
5. OLX in the destination city;
6. Satu, Pulscen, Flagma, supplier catalogs;
7. Instagram pages linked to real stores;
8. nearby cities by logistics rule;
9. Kazakhstan-wide suppliers when local options fail.

Use several query shapes:

- exact phrase from spec;
- normalized commodity name;
- brand/model/article;
- unit and size;
- city plus delivery terms;
- Russian/Kazakh/common misspellings if the item is local-market driven.

## Marketplace Rules

### Kaspi

Kaspi product-card price is useful evidence only when the current city, stock, and product parameters are visible. If the page title says another city or the price is far above budget, classify as reserve or rejected. Product pages can expose metadata in page HTML, but stock and city still require caution.

### OLX

OLX is a lead source. Check:

- publication or update date;
- city/district;
- whether the seller looks like a business or private seller;
- exact unit and quantity;
- whether the description says wholesale, delivery, or pickup;
- whether the offer has enough stock for the tender quantity.

Do not treat an OLX listing as confirmed until the seller confirms stock, final price, delivery, and documents.

### 2GIS

Use 2GIS to find real stores, addresses, operating hours, and categories. A 2GIS listing proves the store exists, not that the product is in stock. Link it as supplier identity evidence and keep stock as pending.

## Match Status Rules

Use `✅ Совпадает` only when all hard parameters are proven:

- purpose/application;
- unit/fascia/weight/volume;
- package count or set composition;
- model/article/color number;
- material/standard;
- required quantity;
- current price;
- available delivery or pickup path.

Use `⚠️ Требует подтверждения` when:

- the product likely fits but stock or quantity is missing;
- price may be stale;
- delivery is unknown;
- VAT/docs are unknown;
- the page is a catalog/search result;
- seller must confirm a technical parameter.

Use `❌ Не использовать без разъяснения` when:

- size/weight/volume differs;
- product purpose differs;
- color or model differs;
- the product is a near substitute and the user has not approved analogs;
- price is above the hard budget and no approval exists.

Over-budget candidates can remain in `Поставщики` as reserve/risk leads, but they must not become the primary solution while an in-budget technically valid option might exist.

## Urgent Lead Ranking

Rank leads by:

1. local stock today;
2. exact technical match;
3. price under tender unit price and positive margin;
4. supplier can deliver to the customer site/address;
5. supplier can provide receipt/invoice;
6. source quality.

For urgent same-day requests, a local supplier with a pending phone confirmation can rank above a perfectly documented out-of-city supplier.
