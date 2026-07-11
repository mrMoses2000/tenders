# Source routing, logistics and offline trips

Version: 0.1
Date: 2026-07-11

## Source routing

The default order is deterministic:

1. bounded public HTML extraction;
2. model validation of compact extracted evidence;
3. Chrome only for missing public fields likely rendered dynamically;
4. Computer Use only after semantic browser extraction fails and visual interaction is truly required;
5. supplier contact draft for facts a page cannot prove;
6. explicit partial result instead of invented data.

| Source | Programmatic first pass | Typical escalation |
|---|---|---|
| Local store | JSON-LD, OpenGraph, title/meta | Chrome for dynamic variants; contact for stock/docs. |
| Kaspi | Public product metadata | Chrome for seller/city/variant state; contact for quantity/docs. |
| OLX | Public ad metadata | Chrome for dynamic ad details; direct contact for current stock. |
| 2GIS | Company identity and location | Chrome for dynamic contacts; another source/contact for product terms. |
| Instagram | Public metadata only | Chrome if publicly visible; never bypass login or messaging controls. |

The router explains its decision with `first_step`, missing fields, and separate browser/contact flags.

## Local versus intercity

Each scenario contains explicit inputs:

- unit price and quantity;
- logistics and other expenses;
- optional risk reserve percentage;
- lead time;
- evidence level;
- technical validity.

Ranking uses:

```text
goods cost + logistics + extra cost + explicit risk reserve
```

A technically invalid cheap analogue receives no rank. Risk reserve is never inferred silently; it must be supplied by policy or the operator.

## Route history

Route observations are summarized only when origin, destination and cargo type match. The result contains minimum, median, maximum, observation count and confidence:

- 1 observation — low confidence;
- 2–3 — medium;
- 4+ — high.

Estimated quotes and actual paid trips must keep different evidence levels. Historical accuracy requires actual user-reported facts.

## Offline market trip

An offline item is stored as a hypothesis with:

- fallback landed price;
- expected market price range;
- quantity and unit;
- explicit probability of finding the item;
- evidence note.

Expected savings are calculated from the midpoint of the price range and probability. The output includes best-case savings, expected savings, trip cost, expected net savings, break-even success probability and a route sheet.

The recommendation intentionally says `как гипотеза`. A market range never becomes supplier-confirmed price, stock, or technical conformity without a real shop/pavilion/contact observation.

## CLI

```bash
PYTHONPATH=src python3 -m tender_agent source-plan \
  https://kaspi.kz/shop/p/test --html tests/fixtures/product_jsonld.html \
  --required product_name price confirmed_stock

PYTHONPATH=src python3 -m tender_agent compare-scenarios tests/fixtures/scenarios.json
PYTHONPATH=src python3 -m tender_agent trip-economics tests/fixtures/trip.json
```
