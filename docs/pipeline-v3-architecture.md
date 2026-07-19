# Pipeline v3: event-driven procurement core

Status: approved for implementation on branch `codex/pipeline-v3-event-core`.
Date: 2026-07-19.

## System goal

Turn photos, documents, Notion rows and direct operator statements into an auditable procurement state, validated sourcing coverage, safe sync plans and operator reports without depending on conversation memory.

## Runtime boundary

```text
Ingress (Codex / future Telegram / files / Notion)
    -> intake artifacts and OCR observations
    -> append-only facts
    -> reconciliation snapshot
    -> sourcing/validation/economics
    -> quality gate
    -> projections: Notion diff, MD/JSON report, message drafts
```

The domain core performs no network writes. Adapters own Notion, browser and messaging side effects.

## Deployment units

Version 3.0 runs as one Python process plus SQLite. Its boundaries are kept explicit so a later deployment can split into:

- ingress/API;
- procurement worker;
- browser runner;
- Notion adapter;
- messaging adapter;
- Postgres/Redis.

The first implementation intentionally avoids microservices: one operator does not need distributed complexity before the state machine and audit contracts are stable.

## Persistence

SQLite stores execution state, facts, snapshots, artifacts, audit events, approvals and idempotency keys. Notion remains the operational business view. Source files remain immutable artifacts addressed by SHA-256.

## Failure model

- invalid facts or snapshot invariants fail before side effects;
- jobs use bounded attempts, leases and dead-letter state;
- Notion writes are planned first, reject immutable property changes and verify after apply;
- messaging requires a persisted approval of the exact payload;
- source/contact/stock unknowns stay explicit;
- every side effect uses an idempotency key.

## Observability

Every task and event includes `correlation_id`, task/request/line identifiers, stage, attempt, timestamps and structured details. Logs must not contain credentials or payment data.

## Compatibility

Existing `RequestLine`, `CandidateOffer`, economics, extractor and CLI commands remain supported. Version 3 adds contracts around them rather than replacing working logic.
