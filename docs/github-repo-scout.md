# GitHub repo scout: tender/procurement projects

Date: 2026-07-07

Scope: find whether existing GitHub projects can accelerate our tender/procurement workflow: spec parsing, supplier search, candidate validation, Notion updates, evidence, logistics and purchase readiness.

## Executive decision

There is no strong ready-made GitHub project that should become the base of our workflow.

Our workflow is not a classic tender portal. We are solving an agentic procurement operation:

- parse a technical specification from DOCX/PDF/photos;
- normalize line items and blocking parameters;
- search Kazakhstan suppliers across Kaspi, OLX, 2GIS, Satu, Pulscen, Instagram and supplier sites;
- validate exactness, price ceiling, stock, documents, delivery timing and risks;
- update Notion without duplicates;
- keep proof links, screenshots, candidate status and next action.

Most public "tender management" repos are about publishing tenders and collecting bids. They do not solve local urgent sourcing and Notion-backed decision support.

## Useful repositories and how to use them

| Repository | Classification | Why it matters | What to take | What not to do |
|---|---:|---|---|---|
| [open-contracting/cardinal-rs](https://github.com/open-contracting/cardinal-rs) | Reference only | MIT project for procurement indicators over OCDS data. Active as of 2026-07-06. | Ideas for red-flag checks, structured validation, scoring of procurement risk. | Do not integrate now; our data is supplier-candidate data, not full OCDS releases. |
| [open-contracting/credere](https://github.com/open-contracting/credere) | Reference only | BSD-3-Clause project for helping MSMEs participate in public procurement. Active as of 2026-07-06. | UX concepts for supplier participation and document readiness. | Do not use as base; market/country assumptions differ. |
| [frappe/erpnext](https://github.com/frappe/erpnext) | Reference only | Mature ERP with buying/procurement entities. GPL-3.0, very large. | Entity ideas: supplier, quotation, purchase order, receipt, item master, status lifecycle. | Do not embed or fork; too heavy and license/ops footprint is wrong for our lightweight agent repo. |
| [odoo/odoo](https://github.com/odoo/odoo) | Reference only | Mature ERP with purchasing modules. Very large platform. | Workflow ideas around RFQ, vendor comparison, receipt/invoice statuses. | Do not embed or fork; this would turn a Notion+agent workflow into ERP implementation work. |
| [gorserg/openprocurement.tender.opencd](https://github.com/gorserg/openprocurement.tender.opencd) | Historical reference | Apache-2.0 plugin around Open Procurement API. Last pushed 2016. | High-level tender-status vocabulary and OpenProcurement/ProZorro-style separation of tender, award, contract. | Do not use directly; stale and not aimed at Kazakhstan local sourcing. |
| [alexluchen/Tender-Procurement-Management-System](https://github.com/alexluchen/Tender-Procurement-Management-System) and similar generic repos | Reject as base | Typical tender portal/project template. Low stars, unclear license, not focused on agent research. | Maybe screenshots/screens for UI inspiration only. | Do not fork or integrate; high mismatch and low confidence. |

## Kazakhstan-specific search result

GitHub search for `goszakup`, `gov.kz`, Kazakhstan procurement parsers and tender bots did not produce a strong reusable candidate during this pass. Treat Kazakhstan public-procurement scraping as a separate future track, not as a dependency for the urgent Notion sourcing workflow.

## Practical impact for our repo

The best path is to keep `/Users/mosesvasilenko/tenders` as our own operational toolkit and borrow patterns, not codebases.

Recommended next automation layer:

1. Add a normalized candidate schema inspired by procurement/ERP systems:
   - request, lot, line item, technical requirement, supplier, offer, price, availability, delivery, evidence, risk, next action.
2. Add source-specific extractors:
   - Kaspi product/merchant snapshot;
   - OLX listing snapshot;
   - 2GIS supplier card snapshot;
   - generic supplier-site offer snapshot.
3. Add a Notion upsert helper:
   - match by request + line item + supplier + URL;
   - update existing candidate rows;
   - never duplicate the same supplier offer;
   - keep attachments/evidence separate from the main line item.
4. Add a validation/scoring CLI:
   - exact technical match;
   - partial match with blocking differences;
   - price ceiling;
   - stock confidence;
   - same-day delivery risk;
   - document readiness.
5. Keep browser/manual research in the loop:
   - markets like Алматы-1, Барахолка, Байсат and local Karaganda shops are not represented well online;
   - the agent should produce call/message scripts and uncertainty blocks instead of inventing availability.

## Decision

Do not import a full GitHub tender system.

Use external projects only as reference material. Build small, auditable scripts and Notion-focused skills around our actual procurement workflow.
