# Audit Of Existing Tender Instructions

Checked files in `/Users/mosesvasilenko/tenders` against the installed `tender-procurement-notion` skill and actual Karaganda/Notion workflows.

## What Already Matched Reality

- Strong rule that a cheap similar product is not enough.
- Correct five-gate purchase readiness model: spec, фасовка, price, stock, delivery.
- Good emphasis on local-first search and logistics cost.
- Useful dangerous substitution list.
- Useful supplier message drafts.
- Correct safety rule: no payment or order confirmation without user approval.

## Gaps Found

- The old files were project instructions, not an installable Codex skill.
- The installed skill covered full tender intake well, but not urgent one-line rescue work.
- Notion update behavior was too generic; it did not describe current row fields and supplier relation handling.
- There was no deterministic helper for checking candidate price/quantity/status.
- Some old instructions allowed sending supplier messages if permitted; the Codex skill now keeps message sending outside the skill by default and only drafts unless explicitly approved.
- City logistics were useful but not integrated into the skill's search order.
- There was no Git-ready repo structure or install command for Codex CLI visibility.

## Changes Made

- Added installable skill under `skills/tender-procurement-notion/`.
- Split long SOP details into references:
  - `search-and-validation.md`
  - `notion-upsert.md`
  - `logistics-and-risk.md`
  - `messages.md`
- Added `scripts/check_candidates.py` for CSV/JSON candidate checks.
- Added repository README and install script.
- Kept legacy root documents as source material.

## Remaining Improvement Ideas

- Add a Notion export/import helper once the database schema stabilizes.
- Add Playwright/Chrome scripts for repeatable Kaspi/OLX extraction where allowed.
- Add a supplier deduplication script using phone/domain normalization.
- Add city-specific reference files when repeated work accumulates.
- Add examples from real completed tenders with sensitive data removed.
