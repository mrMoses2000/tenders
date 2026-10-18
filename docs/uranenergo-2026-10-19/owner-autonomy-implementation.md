# Native research autostart: implementation strategy

Draft implementation on the isolated recovery branch. Deployed: NO. Baseline:
`5f2ba1fc92afd31ebdd0d35a896e2818aa42c7e2`. No production access or activation.

The existing path is attachment extraction → `IntakeService.process` →
`cases.persist_intake_result` → native `prepare_research` job →
`persist_research_plan` → `request_approval` → `approve_request` →
`start_approved_research` → existing executor and queue. Intake evaluates gaps
before persistence; prepare does not reevaluate them. Thus profile defaults must
be applied with an explicit bounded reevaluation at persistence, before deciding
whether to queue prepare. The original source payload and the applied payload
must both be hash-bound in a snapshot. Delivery facts are never sourcing defaults.

1. Add migration 044, leaving 043 reserved. Default-off, immutable profile episodes
   have real user/customer references, NULL-safe scope uniqueness, consent text
   and reference, canonical policy hash, version and monotonic revocation.
   Profile changes revoke and insert; exact project matching never falls back.
   Capture immutable case/parse/profile/source/applied hashes at intake, including
   provenance and per-item eligibility, without changing the parsed-request schema.
2. Resolve profiles only for new intake or an already snapshotted Telegram intake
   continuation. Existing manual/imported cases stay manual and retain item IDs.
   Reevaluate once after missing city/scope defaults. Explicit conflicting source
   geography stays untouched and disables automatic authorization. In opt-in flow,
   item ambiguity cannot block clear items. Initially research only clear lines,
   retaining original item indices; disputed lines remain visible and need review.
3. In prepare, bind the exact persisted run/plan to the immutable snapshot in an
   additive authorization record. Keep the old three-field manual approval payload
   identical; add an exact optional binding for automation. Validate still-active
   profile, owner/project, applied parse/hash and plan/hash at capture and start.
   Request, approve, consume, start event and execute job share a transaction.
   Use native uniqueness/idempotency and recover already-started runs on retry.
4. Add minimal owner-authenticated profile repository/API and read-only per-case
   source visibility endpoint, plus existing MiniApp display. Expose stable item
   ID/ordinal, warning/ambiguity/source pointer and actual eligibility; keep raw
   artifacts, private tender prices and filesystem paths out of this projection.
5. Define an immutable owner-attested exact-item disposition interface backed by
   a case-owned artifact SHA and verbatim assertion; keep accounting separate.
   Do not apply exclusions to run plans until index/quantity compatibility is proven.

Affected files: new owner-autonomy module, migration 044, cases.py, dialogue.py,
worker.py, research_workflow.py, mini_app.py, mini_app/app.js and focused tests/docs.
No edits to intake.py, config.py, cli.py, city_tier_policy.py, text extractor,
outbox/account-binding work, or research.py. No new job engine or provider framework.

Network access belongs to the existing executor, never the policy module. No
WhatsApp policy is created. Denial/drift leaves manual review available; timeout
and provider failure remain native queue states, never market exhaustion. First
plan uses exactly the first policy city. Ordered city scheduling and the report
checkpoint runtime hook remain explicitly pending integration; no fake SLA timer.

Verification uses synthetic documents, fake providers, real migrations in random
schemas of a new local cluster in `.operator-test-pg`, TCP 127.0.0.1:55448,
database `test_uran_owner`, guarded by scripts/postgres_test_safety.py. Prove
default manual compatibility, defaults/readiness, exact native autostart,
owner/project/revocation/drift denial, partial ambiguity, immutable records,
concurrent/restarted retries, owner-only visibility and WhatsApp separation.
Expand to connected attachment/discovery/RFQ/quote/MiniApp with fake boundaries.
Commit small sanitized increments using explicit git add allowlists only.
