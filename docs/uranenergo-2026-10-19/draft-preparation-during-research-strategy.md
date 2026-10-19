# Draft preparation during active research — strategy — 2026-10-19

## Current flow

`run_pipeline_tick` verifies the runtime run is still autonomous, runs transport/research reconciliation, coalesces legacy preparation jobs, and then schedules `request_outreach` only when `outbound_ready` is true. The same gate protects due reminders and reacquiring dialogue slots. Thus an account hold or missing pin blocks the first draft job, while first account pinning depends on the conversation drafted from a persisted supplier lead. Research completion and contact enrichment already request preparation through the existing coalescing queue.

`request_outreach` only records a source generation and enqueues a single `prepare_supplier_outreach` job per case. The worker builds approval-gated draft records through `prepare_supplier_outreach`; that function does not call the provider. The worker may make read-only WAHA status/number checks before drafting. Physical delivery remains separately gated by approval, pin, account health, hold/circuit, and budgets in the existing delivery path.

## Intended narrow behavior

Preserve the current `outbound_ready` behavior. In addition, when the tick runs for an enabled, unpaused research run that is still `running`, permit the same coalesced draft job despite outbound hold/unpinned state only if WhatsApp is enabled, `research_auto_prepare_outreach` is enabled, the case is not a Notion import, and at least one persisted offer row exists in `exact`, `confirm`, or `lead` state. This is a scheduling condition only: it does not release a hold, reacquire slots, create reminders, approve a draft, or send a message. With no persisted candidate row or disabled preparation configuration, the held-account path queues nothing.

The request source key remains the existing stable case/generation key. `request_outreach` retains its transactional/advisory-lock coalescing, so repeat ticks do not fan out jobs. No database schema or existing job contract changes.

## Files and verification

Runtime change: one condition in `src/procurement_bot/autonomous_pipeline.py`. Tests: focused native PostgreSQL tick tests in `tests/test_autonomous_pipeline.py` for a held/unpinned account with a persisted lead (one coalesced preparation job and no outbound event), no offers, disabled configuration, and replay/coalescing. No worker, delivery, migration, provider, live database, or service changes are planned. Use only the disposable local PostgreSQL service on port 55451 with stubbed provider status.

## Limits

Passing this verifies draft-job scheduling and the no-send invariant in the isolated test DB. It does not validate a deployed worker or enable production delivery. Account activation and release remain owner-controlled and require the separate review/deploy process.
