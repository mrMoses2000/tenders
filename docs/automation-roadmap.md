# Automation Roadmap

## High-Value Automation

1. Candidate checker
   - Input: CSV/JSON supplier leads.
   - Output: over-budget, insufficient quantity, stale source, missing delivery.
   - Status: implemented as `scripts/check_candidates.py`.

2. Supplier dedupe
   - Normalize domains, phones, company names.
   - Warn before creating duplicate Notion suppliers.

3. Notion row packer
   - Build `Доказательства / заметка` and `Что уточнить` text from structured candidates.
   - Reduce repeated hand-written notes.

4. Source extractor
   - Extract title, price, image, stock, and city from common sites:
     - Kaspi product page;
     - OLX listing/search page;
     - Satu/Pulscen product pages;
     - Domsad/local shop cards.

5. City logistics estimator
   - Store known school addresses and 2GIS search URLs.
   - Estimate whether taxi, courier, or Gazelle is needed based on weight/volume.

6. Acceptance-risk library
   - Add reusable checks for common categories:
     - сухие смеси;
     - краски/колеры;
     - электрика;
     - сантехника;
     - инструменты;
     - канцтовары.

## Guardrails

Automations should not:

- send supplier messages;
- place orders;
- store secrets;
- mark a row as fully verified without evidence;
- overwrite user-entered Notion data without preserving context.

## Next Suggested Scripts

- `scripts/dedupe_suppliers.py`
- `scripts/pack_notion_note.py`
- `scripts/extract_kaspi_product.py`
- `scripts/extract_olx_results.py`
- `scripts/estimate_delivery_class.py`
