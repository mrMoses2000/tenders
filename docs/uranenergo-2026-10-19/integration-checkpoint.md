# Connected synthetic checkpoint for Luna — 2026-10-19

## Current integrated account-operator checkpoint — 2026-10-19

The published account-operator branch at `e1cf00a7f31031386df6475f0afec46c2ffb55bd` was merged normally into this isolated integration branch. Merge commit: `22a1312` (`Merge branch codex/uranenergo-account-operator-miniapp-2026-10-19`). The merge preserved the integration-side source-bound research retry fix from `c7e4294`; no conflicts or manual resolutions were needed. The older writer checkpoint below is retained as history and is superseded for current status by this section.

The Mini App exposes authenticated account readback, explicit identity pinning, and exact-snapshot legacy-unknown attestation. It does not expose hold/circuit release. The research tick may now request the existing coalesced draft-preparation job during a still-running native research run when a persisted exact/confirm/lead offer exists and the native WhatsApp/auto-preparation settings are enabled. The held/unpinned account path still does not prepare reminders or reacquire dialogue slots. Draft preparation remains approval-gated; physical send authorization and delivery checks were not changed. Migration 045 is present in this isolated source tree only and has not been applied in production. No 046 runtime is included.

Selected verification on the merged tree:

- `tests/test_uran_connected_postgres.py tests/test_whatsapp_account_identity.py`: **46 passed**, 14 dependency/aiohttp warnings, using disposable schemas on verified `127.0.0.1:55450/test_uran_integrated`. This includes the connected owner/API workflow, held-account synthetic delivery guard, and source-bound index-0 skip/replay that preserves the original second-item UUID.
- `tests/test_mini_app_whatsapp_operator.py`: **1 passed**, 3 existing aiohttp AppKey warnings, on the required separate `127.0.0.1:55451/test_uran_account_operator` target.
- Held-research tick cases: **5 passed** on verified `127.0.0.1:55450/test_uran_integrated`.
- Ruff, Python compileall, Node 22 syntax check (`node --check mini_app/app.js`), and `git diff --check HEAD^1 HEAD`: passed.

Both PostgreSQL targets were already running and were checked before connecting. Tests used generated fixtures and stubbed network boundaries; no production database, live WAHA request, provider search, service change, or external message was part of these checks.

Validation limits: an earlier broad run found 15 failures in `test_autonomous_pipeline.py` and `test_pipeline_reviews.py`, and reproduced the same names on clean baseline `5f2ba1f`. The complete broad set was not rerun after this merge, so this checkpoint claims only the selected checks above. At report time, the first production Taraz run was blocked at the search-profile loader by a `SearchProfileConflict`; the separate activation checkout owns that small fix. This merged integration source must not be packaged or released until the reviewed fix SHA is included and the Taraz run advances. No production migration 045, pin, quarantine, hold/circuit action, or worker restart is authorized by this checkpoint.

This is an uncommitted implementation checkpoint, not completion of the city-runtime assignment or deployment approval. The only runtime change is inside `JobWorker._prepare_supplier_outreach`: draft/conversation preparation no longer requires an already open/pinned account or an outbound-enabled provider client. Session availability, contact verification, and repository owner/contact/opt-out checks remain. Physical send authorization and delivery code are unchanged.

## Exact revisions

- Branch: `codex/uranenergo-integrated-2026-10-19`.
- Integration began: `b4bd777`.
- Initial combined tests: `27ec66efd5d90f2504cdb0cdbc7e9542b5332300`.
- Final connected test and clean-code reproduction: `032da6651e30f2634f6675d6fbee378247c2332b` plus this writer's uncommitted diff.
- Owner: `f7d7f2fbc0224d41d6fd001c153a5acb5141c7bf`.
- Owner portability: `57e065d49a0e5edc03efc773b4777fcfa3007184`.
- City policy: `36a4c29df5900ed640017c258d67fcd99e4222da`.
- Text runtime: `d0ac6f58559175a4f1b0e77270bccc9141795a32`.
- Text docs: `77bd019d39dea10a4fa150bf58d4e76a9109cf13`.
- Incoming text capacity note: `3224feaa906d76bd3ac998d1314ed225c7b1c7c0`.
- Incoming text schema/timeout fix: `7b91687ef772f1729f439fdc9fc6db1cdc67b957`.
- Clean comparison baseline: `5f2ba1fc92afd31ebdd0d35a896e2818aa42c7e2`.

Luna/root advanced HEAD during execution. This writer made no commits, pushes, staged changes, or edits to incoming/protected files.

## Connected evidence

`tests/test_uran_connected_postgres.py`: **6 passed**, 14 existing dependency/aiohttp warnings, 11.88 seconds on the final run.

Four parametrized tests connect generated PDF/DOCX and owner default-off/opt-in through:

1. Native `persist_telegram_message`, duplicate update rejection, queued download, signature/size validation, content-addressed storage, original source artifact, native extraction, intake persistence and replay. A fresh real `JobWorker` handles each job; replay does not repeat the file download, parse, or research start.
2. Native research prepare/request/approve/start. Default-off remains requested until explicit owner approval; opt-in uses its immutable native policy binding. The stored request is exactly `city=Тараз`, `search_scope=citywide`, `source_type=document`; Russian ordered cities and the profile policy hash remain unchanged. The opted-in ambiguous second item remains `needs_review`, and no browser work or offer is created for it.
3. `ParallelResearchRunExecutor` and the real `ResearchRunExecutor` browser-result validator/persistence path. The deterministic browser port supplies hash-bound source evidence and contacts. The test never inserts an offer directly. Native terminal status may be `partial` because the small fixture does not meet the supplier-count target; this is not a claim that all research is complete.
4. Native queued worker outreach while the synthetic account is held/unpinned and the fake client has outbound disabled. Draft conversations make owner pinning possible. Explicit approval can enqueue an event, but a held-account delivery attempt allocates no provider ID/reservation and makes no POST. Pinning preserves the hold; native budget configuration, fresh fake health, and explicit owner release are separate operations.
5. Wrong fake WAHA self-account returns `whatsapp_account_identity_mismatch` before provider ID allocation, reservation, or POST. A subsequent correct identity delivers through the native outbox and acceptance journal. Restart sends no duplicate; fake logs contain exactly one physical-send call. The test varies only the synthetic quiet-hour window through the policy loader so it can run at any wall-clock hour; it does not replace account, recipient, approval, identity or budget checks.
6. The authenticated `WahaWebhookHttpIngress` rejects a missing secret, accepts a quote from the exact active outbound recipient/transport, deduplicates replay, and queues native reply processing. Amount `1250`, currency `KZT`, quoted unit `m`, and full original delivery wording remain in supplier-message evidence. Amount/unit reach the native quote snapshot and a Telegram-authenticated MiniApp case read; unauthenticated reads are rejected.

Two additional generated DOCX seven-column tables use the actual deterministic table parser with `USD` and unspecified currency. They call no structured extractor, retain original currency text and source prices in the extraction, emit a warning, and create no KZT financial row.

The new main test was also run against clean `032da66` using that worktree's `src` via `PYTHONPATH`. It fails at the worker draft stage with `whatsapp_account_outbound_held`, demonstrating the regression fixed by the 17-line worker diff. This expected failure is separate from the six passing changed-code tests.

## Exact commands and results

All write-capable tests used only role `moses`, `127.0.0.1:55450/test_uran_integrated`, unique disposable schemas, generated temporary files and injected fakes. The existing cluster remains running and was not stopped/reinitialized/dropped. Before the runtime feedback corrected the sandbox context, a separate `/tmp/uran-writer-pg` directory was initialized and its TCP startup failed with `Operation not permitted`; it never acquired a listener and was not used for any test. No further cluster creation was attempted.

The test interpreter is `/tmp/uran-review-tools/bin/python` (Python 3.14); dependencies were installed into `/tmp/uran-writer-deps` without changing repository dependency files.

Final connected check, from `/home/moses/tenders-integrated-recovery`:

```bash
env -u DATABASE_URL -u POSTGRES_DSN \
  PYTHONPATH=/tmp/uran-writer-deps:src \
  TEST_POSTGRES_DSN=postgresql://moses@127.0.0.1:55450/test_uran_integrated \
  TEST_POSTGRES_EXPECTED_DATABASE=test_uran_integrated \
  TEST_POSTGRES_EXPECTED_PORT=55450 \
  /tmp/uran-review-tools/bin/python -m pytest -q \
  tests/test_uran_connected_postgres.py --tb=short \
  --junitxml=/tmp/uran-connected-checkpoint.xml
```

Output: `/tmp/uran-connected.log`. Result: **6 passed**.

Original requested failure comparison: run the same environment prefix above with:

```bash
/tmp/uran-review-tools/bin/python -m pytest -q \
  tests/test_autonomous_pipeline.py tests/test_pipeline_reviews.py --tb=short \
  --junitxml=/tmp/uran-combined-before.xml
```

Combined result: **15 failed, 40 passed**, 33.32 seconds. The identical command from clean disposable worktree `/tmp/uran-integrated-base5f-writer` at `5f2ba1f`, changing only the XML output to `/tmp/uran-base5f-before.xml`, produced **the same 15 failures, 40 passes**, 33.18 seconds. Logs: `/tmp/uran-combined-before.log`, `/tmp/uran-base5f-before.log`. The native guard behavior was not weakened to satisfy those fixtures.

Broader regression command with the same safe environment:

```bash
/tmp/uran-review-tools/bin/python -m pytest -q \
  tests/test_worker.py tests/test_supplier_outreach.py \
  tests/test_whatsapp_account_identity.py tests/test_whatsapp_account_controls.py \
  tests/test_whatsapp_approval_workflow.py tests/test_whatsapp_ingress.py \
  tests/test_whatsapp_inbound.py tests/test_owner_autonomy_postgres.py \
  --tb=short --junitxml=/tmp/uran-bootstrap-regression.xml
```

Result: **122 passed, 4 failed**, 45.57 seconds. Log: `/tmp/uran-bootstrap-regression.log`. Those four failures also reproduce on clean `032da66` without this diff:

```bash
# CWD: /tmp/uran-integrated-head-writer, same safe environment prefix
/tmp/uran-review-tools/bin/python -m pytest -q \
  tests/test_worker.py::test_terminal_research_schedules_idempotent_report_job \
  'tests/test_supplier_outreach.py::test_postgres_multi_item_outreach_turns_are_retry_safe[True]' \
  tests/test_whatsapp_account_identity.py::test_real_pre043_null_identity_rows_migrate_and_replay_without_provider \
  --tb=short --junitxml=/tmp/uran-bootstrap-failures-clean-head.xml
```

The four observed causes are an owner-guard unit stub lacking `fetchrow`, an unpinned-account send fixture, and two tests expecting the migration list to contain only 043 even though 044 is now present. Log: `/tmp/uran-bootstrap-failures-clean-head.log`. An initial attempt to run this extra comparison at `5f2ba1f` collected no tests because its account-identity module does not exist there; no result was inferred from that attempt.

Clean-code reproduction of the new test, from `/tmp/uran-integrated-head-writer`:

```bash
env -u DATABASE_URL -u POSTGRES_DSN \
  PYTHONPATH=/tmp/uran-writer-deps:/tmp/uran-integrated-head-writer/src \
  TEST_POSTGRES_DSN=postgresql://moses@127.0.0.1:55450/test_uran_integrated \
  TEST_POSTGRES_EXPECTED_DATABASE=test_uran_integrated \
  TEST_POSTGRES_EXPECTED_PORT=55450 \
  /tmp/uran-review-tools/bin/python -m pytest -xq \
  '/home/moses/tenders-integrated-recovery/tests/test_uran_connected_postgres.py::test_native_document_to_research_outreach_and_authenticated_quote[False-pdf]' \
  --tb=short
```

Expected failure: `prepare_supplier_outreach` remains pending with `whatsapp_account_outbound_held`. Log: `/tmp/uran-connected-clean-head.log`.

Static checks:

```bash
PYTHONPATH=/tmp/uran-writer-deps /tmp/uran-review-tools/bin/python -m ruff check \
  src/procurement_bot/worker.py tests/test_uran_connected_postgres.py
PYTHONPATH=/tmp/uran-writer-deps /tmp/uran-review-tools/bin/python -m ruff format --check \
  tests/test_uran_connected_postgres.py
git diff --check
```

All passed.

## Exact 15-failure diagnosis after comparison

Every row below fails on both initial combined HEAD and clean `5f2ba1f`; none is newly attributable to the owner/city/text merges on that comparison. The shared test seed creates a recipient/transport identity that differs from several callers’ hard-coded identity. The matching-transport connected fixture passes without loosening those guards.

| Module / test | Observed failure and diagnosis |
| --- | --- |
| autonomous_pipeline / `test_receipt_race_replay_deadlines_reminders_rotation_and_late_slot` | Line 608: inbound is `quarantined_unknown_sender`, so conversation ID is NULL. Sender does not match the seeded contact. |
| autonomous_pipeline / `test_complete_whatsapp_delivery_persists_reply_deadline` | Line 957, `conversations.py:676`: message transport does not match the seeded conversation. |
| autonomous_pipeline / `test_completed_send_records_truth_without_resurrecting_opt_out` | Line 1040: same transport mismatch before the asserted opt-out projection. |
| autonomous_pipeline / `test_timed_reminder_is_blocked_after_supplier_reply[0]` | Line 1131, `whatsapp_delivery.py:199`: recipient does not match active contact, before reminder-specific validation. |
| autonomous_pipeline / `test_timed_reminder_is_blocked_after_supplier_reply[5]` | Same recipient mismatch. |
| autonomous_pipeline / `test_final_account_reservation_rechecks_case_and_contact_state[opt_out]` | Line 1206: final delivery revalidation rejects the mismatched recipient first. |
| autonomous_pipeline / `test_dead_unknown_reserved_send_reconciles_without_post[dead-False]` | Line 1326: test expects an old unbound action to issue history GET; actual call log stays empty. Account-bound identity is missing and transport is also inconsistent. No retry/GET should be enabled to satisfy this expectation. |
| autonomous_pipeline / `test_dead_unknown_reserved_send_reconciles_without_post[dead-True]` | Line 1333: fixture journal does not establish a matching active transport; action remains dead instead of sent. |
| autonomous_pipeline / `test_dead_unknown_reserved_send_reconciles_without_post[sending-False]` | Line 1326: same absent history GET for an unbound old action. |
| autonomous_pipeline / `test_contact_repair_authorization_checks_owner_and_original_dead_job` | Line 1496: `ImportError`, private legacy `scripts.verify_uran_contact_recovery` is absent. No private script was copied/imported from another checkout. |
| pipeline_reviews / `test_reviews_deduplicate_owner_alerts_and_preserve_unknown_until_exact_history` | Line 63: history reconciliation returns 0 rather than 1; proof names the wrong physical chat. |
| pipeline_reviews / `test_supplier_terminal_failure_notifies_case_owner_and_is_persisted` | Line 106: no reply-processing review exists. Mismatched inbound sender was quarantined, so the queried message ID is NULL and no real reply-processing subject exists. |
| pipeline_reviews / `test_dead_reply_with_exact_projection_proof_resolves_and_is_not_recreated` | Line 162: failed-reply review stays open. The purported inbound/projection proof refers to no matching persisted inbound message. |
| pipeline_reviews / `test_dead_reply_without_case_scoped_projection_proof_stays_open` | Line 262: expected review count is 0 instead of 1; the fixture lacks the persisted inbound subject after quarantine. |
| pipeline_reviews / `test_review_context_is_case_scoped_and_tolerates_missing_sources` | Line 362: `pipeline_reviews.subject_id` NOT NULL violation. The message lookup yielded NULL after sender quarantine; keep the constraint. |

## Remaining gaps and protected-file boundary

- **City runtime is not implemented in this checkpoint.** No migration 046, immutable per-city authorization/outcome repository, Taraz → Shymkent → Almaty transition, current-pointer stale-completion fence, failure resumption or idempotent four-hour report dispatch has been added. The existing pure city policy is not presented as runtime coverage. The requested batched original-item-ID tier behavior still needs implementation and PostgreSQL tests.
- **The four current-case purchased-item exclusions are not applied by this synthetic suite.** The owner implementation can skip exact source-bound lines, and this checkpoint proves an index-0 skip preserves original index 1 and the original second item UUID through the actual offer. The real four item dispositions still require a private owner-reviewed plan/preflight; no current case was adopted.
- **Partial owner reductions are not purchase-ready.** The requested plan can be reduced while the canonical request item remains unchanged. Comparison/accounting does not yet consume a typed source-backed remaining-demand quantity; only full skip semantics are covered.
- **Delivery terms are preserved as raw supplier evidence, not projected facts.** `offer_observations.raw_payload.text` retains the complete delivery sentence. Quote snapshots currently select amount/unit/stock fields, and protected `mini_app.py:_public_quote_facts` does not allow delivery terms. A future typed, source-backed delivery-term projection requires a reviewed schema/reply/snapshot change and a minimal protected MiniApp allowlist/projection update; that file was not touched.
- **Report generation/timer dispatch is not connected.** Native terminal report jobs stay queued outside this test's selected stage drains. There is no all-search-completion or four-hour-report claim.
- **Historical unknown sends/quarantine are not exercised or modified.** No migration 045 or pilot-grant framework. No old unknown action was queried, resent, converted to not-sent, or cleared from exposure. The global unknown gate's interaction with new scoped sends remains unconnected here.
- The harness injects fake structured intake for its four full flows, a fake browser port, fake Telegram download and fake WAHA methods. Native browser validation, offer persistence, auth and durable job/outbox code run; live provider capacity, HTTP adapter behavior, deployment and production readiness are unverified. Two currency checks separately use the native table parser.
- The existing account-pin authorization race described in the strategy remains outside this minimal worker fix. Adoption can race a claimed pre-adoption manual prepare job; the reviewed current-case release path must first verify zero pending/running jobs and no run, while the general case needs a transaction-level case fence.

## Exact writer paths

- `src/procurement_bot/worker.py` — only `_prepare_supplier_outreach`, four added / thirteen removed lines.
- `tests/test_uran_connected_postgres.py` — connected PostgreSQL harness and generated fixtures, including a source-bound first-item skip/retry proof that leaves a native offer bound to the original second item UUID.
- `src/procurement_bot/research_repository.py`, `src/procurement_bot/owner_autonomy.py`, and `src/procurement_bot/research_execution.py` — additive, strict sourcing instruction metadata is persisted before idempotency comparison; retries reuse the same exact scope, and executor accepts only the legacy shape or the canonical metadata extension.
- `tests/test_research_repository.py` — rejects malformed, noncanonical, and duplicate instruction IDs before database writes.
- `docs/uranenergo-2026-10-19/integration-strategy.md` — preserve root decisions, update approved scope/revisions and observed bootstrap fix.
- `docs/uranenergo-2026-10-19/integration-checkpoint.md` — this handoff and diagnostics.

Review patch: `/tmp/uran-integrated-writer.patch`. Test XML/logs and the two disposable detached comparison worktrees are retained under `/tmp` for Luna. No explicit or implicit staging, commit, push, deployment, real outbound message, production account/service access, browser or Chrome use occurred.
