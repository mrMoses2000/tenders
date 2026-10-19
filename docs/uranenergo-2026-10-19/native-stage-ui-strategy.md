# Native Mini App stage summary — UI strategy

## Design diagnosis

The repository and workspace do not contain a root-level references/ directory. The native Mini App is the primary design reference: a 780px single-column surface, warm paper and white surfaces, near-black structural bands, restrained olive/lime and status colors, compact typography, and thin dividers. Preserve that hierarchy and spacing cadence; avoid new gradients, shadows, arbitrary cards, and small unreadable captions. Supporting text should remain at least 12px and body text at least 14px in new stage controls.

## Existing entry point and data flow

mini_app/app.js loads GET /api/cases/{case_id}, which already returns stable request items, source visibility by item, optional research progress by item, offer comparison snapshots, and case-level pipeline metrics. Each item card currently renders immediately into one long list and is filtered only by text and offer presence.

Use only these existing fields:
- source_visibility.items[].stage, sourcing_evidence, disposition_applied_to_plan, and source warnings for owner-declared actions and clarification.
- research_progress[].status/found_leads/qualified_price_suppliers and request_items.status for actual per-item run progress and persisted legacy states (researching, offers_found, no_match).
- offers[].comparison.rank/price_state for confirmed comparable supplier options; offer rows and per-item offer_count for candidates.
- pipeline_metrics.waiting_chats only as a case-level conversation count. No response-to-item mapping is returned, so the UI must not assign these chats to item groups or fabricate an “awaiting reply” item state.
- Existing purchase_lines and accounting item fields remain intact and clearly distinct from owner-declared sourcing instructions.

Effective instructions are detected only from existing evidence with applies_to_sourcing=true and a source_payload_sha256 equal to the current case source hash. Show such an instruction read-only; the backend permits one immutable effective instruction per item/parse. Evidence-only facts do not disable the form. Disable the apply-to-plan checkbox when the source projection reports item issues; keep evidence-only capture available.

No backend fields or API semantics will change in this UI slice.

## Intended behavior and primary groups

Show a compact stage summary above the existing item search, with counts and filters derived from one mutually exclusive primary classification per stable request item. Render collapsible group sections rather than a flat wall of 55 rows. Keep all original item IDs, line numbers, item forms, comparisons, and accounting controls in the existing item cards.

Primary classification precedence:
1. Full existing recorded purchase or an effective owner sourcing instruction pinned to the current parse hash. Show whether it is an accounting purchase, an owner-declared exclusion, or owner-reported quantity to source around; state when the instruction is not yet applied to a run plan.
2. Evidence-only owner purchase claim: show “Покупка требует сверки”; it must not exclude the item.
3. Source or item needs clarification.
4. At least one supplier-confirmed comparable option (real persisted comparison state only).
5. Supplier candidates from actual offers or per-item found leads.
6. Queued, active, or paused research.
7. Completed/partial/failed research requiring review, distinct from “not searched”.
8. Not started, only when no progress and no higher-priority state exists.

The case-level waiting-reply count is a separate summary metric labeled as conversations, not item count. Keep secondary badges available inside each row; they do not increment another primary-group count. Never interpret an item ready status or a price alone as purchase readiness.

## Minimal implementation scope

- mini_app/app.js: pure stage-classification/grouping helpers, summary controls, grouped item containers, and search/offer-filter integration; preserve current item card and purchase/accounting flows.
- mini_app/app.css: restrained styles reusing existing palette, dividers, control heights, and type scale; responsive single-column layout.
- tests/test_mini_app_stage_groups.py: isolated Node tests for precedence, count exclusivity, “not searched” versus finished/failed search, and case-level waiting chat treatment. No production fixture data.
- This strategy document.

## Verification

Run JavaScript syntax validation with the repository Hermes Node runtime; run the isolated stage-group behavior tests and existing Mini App offer-pagination tests; run git diff --check. Browser visual review is delegated to root after integration. No loopback fixture server or external writes are authorized.
