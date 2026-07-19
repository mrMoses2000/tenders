# Pipeline v3 — implementation report

Date: 2026-07-19
Branch: `codex/pipeline-v3-event-core`

## Outcome

The repository now contains a dependency-free, durable procurement core. It does not rely on chat memory or a single editable Notion status. Source statements are retained as evidence-backed facts; one deterministic snapshot feeds reconciliation, reports and future Notion updates.

## What changed

### 1. Append-only facts and source authority

`src/tender_agent/facts.py` introduces:

- stable request line identity independent of product aliases;
- typed facts for purchase, delivery, school presence, acceptance, rejection and replacement;
- source reference, observed time, confidence, quantity, note and metadata;
- explicit `supersedes_id` instead of destructive editing;
- deterministic active-fact selection with direct operator assertions above document interpretations.

This preserves both sides of a conflict. A school sheet can say “not found” while a later direct statement confirms delivery. The output becomes “conflict — check at school”; neither source is deleted.

### 2. Orthogonal line state

The reconciliation engine calculates separate axes:

- procurement;
- movement/delivery;
- school location;
- acceptance;
- replacement.

It also calculates delivered, found, accepted, remaining-to-deliver and remaining-to-accept quantities, conflicts and next actions. A single ambiguous status can no longer silently convert “delivered but not located” into “not supplied”.

### 3. Canonical snapshot and reports

`src/tender_agent/snapshot.py` validates line coverage, unique line numbers and quantity invariants. Its checksum excludes generation time, so identical inputs produce identical state identifiers.

`src/tender_agent/reports.py` generates the operator view from that snapshot only:

- buy or complete delivery;
- replacement;
- check at school;
- closed.

The same line may intentionally appear in more than one action group, for example “complete 20 colours” and “confirm acceptance”.

### 4. Evidence-aware intake

`src/tender_agent/intake.py` adds immutable SHA-256 source artifacts and field-level OCR observations with page, bounding box and confidence. The completeness gate detects missing lines, duplicate numbers and observations below the confidence threshold. It is an evidence contract, not an OCR engine; visual extraction remains an adapter responsibility.

### 5. Durable execution and recovery

`src/tender_agent/persistence.py` creates a versioned SQLite schema for:

- tasks and jobs;
- facts and snapshots;
- source artifacts;
- audit events;
- approvals;
- idempotency keys;
- supplier/route performance events.

`src/tender_agent/orchestrator.py` provides a worker boundary with leases, bounded attempts, retry/backoff, dead-letter state and structured audit events. An expired running lease is reclaimable after a worker crash. Task transitions are explicit and invalid transitions fail early.

SQLite is the intentional single-operator deployment. Domain contracts are storage-neutral so the same boundaries can later be backed by Postgres.

### 6. Safe Notion projection

`src/tender_agent/notion_sync.py` implements:

- a dry-run diff;
- immutable request fields (`№`, name, specification, quantity, unit and tender price);
- deterministic plan hash;
- page-by-page apply with read-after-write verification;
- explicit rollback patches after partial failure;
- a gateway protocol and in-memory integration adapter.

The module does not silently rewrite source technical specifications. A real Notion connector should implement the gateway protocol and persist the plan hash through the idempotency store before enabling writes.

### 7. Sourcing quality instead of link counting

`src/tender_agent/sourcing.py` separates:

1. supplier lead;
2. product observation;
3. supplier quote;
4. purchase-ready candidate.

Coverage checks freshness, proven technical match, confirmed price, contactability, local coverage, reserve coverage and optionally confirmed stock. Five weak links therefore fail the quality gate. Search routes are explicit and do not repeat already exhausted stages.

### 8. Messaging safety and operational history

`src/tender_agent/messaging.py` sends only an exact approved payload and records an idempotent result. No WhatsApp, Telegram, reservation, order or payment adapter is enabled by this change.

`src/tender_agent/history.py` and the SQLite performance table keep actual supplier/route observations separate from estimates for future landed-cost and reliability decisions.

## Golden reconciliation: Караганда_1

`tests/fixtures/karaganda_reconciliation.json` encodes the direct operator facts that must override document interpretations without deleting them:

- lime: 200/200 delivered, while the school sheet says not found — conflict and physical check;
- tile adhesive: 9/9 delivered — acceptance check remains;
- colourant: 100/120 delivered, colour №3 — 20 remain;
- household rubber gloves line №6: delivered;
- laboratory chemical-resistant gloves line №40: not delivered/not found;
- lights: replacement required and replacement delivered — acceptance check remains.

The test asserts that №6 and №40 never merge despite both containing “gloves”.

## CLI

New commands preserve all previous commands:

- `snapshot-validate` — validate a canonical snapshot and checksum;
- `reconcile` — calculate all line states;
- `report --format md|json` — generate operator output;
- `store-init` and `job-status` — initialize and inspect durable state;
- `notion-plan` — produce a read-only property diff;
- `coverage` — evaluate sourcing quality.

All commands are included in `scripts/run_checks.sh` smoke coverage.

## Verification design

The new tests are independent by boundary:

- domain/golden tests use no database or Notion adapter;
- persistence tests use isolated in-memory or temporary SQLite files;
- Notion tests use an in-memory gateway and deliberately injected failure;
- sourcing tests use fixed candidate contracts and clocks;
- intake tests use a temporary artifact;
- old tests remain unchanged and verify backward compatibility.

Covered failure cases include duplicate facts, fact mutation, supersession, stale quotes, five weak links, immutable Notion changes, partial Notion failure, missing approval, duplicate send, job retry, dead-letter and worker lease recovery.

## Deliberate limits

The core is implemented; external side-effect adapters are intentionally not activated:

- live Notion writes require a connector implementing `NotionGateway` plus an operator-approved apply step;
- OCR/computer vision must produce `OCRObservation` records and pass completeness checks;
- WhatsApp/Telegram senders must implement `MessagingAdapter` and cannot bypass exact-payload approval;
- source research still needs browser/contact adapters, but candidates cannot pass the quality gate without evidence.

These are safety boundaries, not missing domain logic. They prevent a search result, OCR guess or partial Notion update from becoming an unreviewed business action.
