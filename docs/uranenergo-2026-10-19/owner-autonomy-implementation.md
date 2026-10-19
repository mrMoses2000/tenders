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
5. Record immutable item dispositions through either a native Telegram artifact
   whose stored SHA is checked against the case, or an authenticated operator
   attestation carrying a source reference and owner-declared SHA-256. Operator
   hashes are claims, not server-verified file bytes. Both paths require an exact
   owner/case/item mapping and verbatim assertion; they remain separate from
   accounting. Current dispositions are visible as evidence only and do not yet
   change plan quantities or exclude rows from research.

Affected files: new owner-autonomy module, migration 044, cases.py, dialogue.py,
worker.py, research_workflow.py, mini_app.py, mini_app/app.js and focused tests/docs.
No edits to intake.py, config.py, cli.py, city_tier_policy.py, text extractor,
outbox/account-binding work, or research.py. No new job engine or provider framework.

Network access belongs to the existing executor, never the policy module. No
WhatsApp policy is created. Denial/drift leaves manual review available; timeout
and provider failure remain native queue states, never market exhaustion. First
plan uses exactly the first policy city. Ordered city scheduling and the report
checkpoint runtime hook remain explicitly pending integration; no fake SLA timer.

This slice binds profiles only during new Telegram intake or an already
profile-bound continuation. Existing manually imported cases remain manual until a separate owner-attested adoption
flow supplies a read-only parse-hash/stable-item-ID preflight and captures the
current applied parse without replacing its item projection. No live
environment, production database, WhatsApp or payment surface was used.

Verification ran synthetic documents and fake providers against the real
migrations in random schemas of the isolated local `.operator-test-pg` cluster
(TCP 127.0.0.1:55448, database `test_uran_owner`), guarded by
`scripts/postgres_test_safety.py`. The focused command
`bash .operator-test-pg/run-tests.sh tests/test_owner_autonomy_postgres.py -q`
passed **22 tests**. Ruff, Python compileall, Node `--check`, and
`git diff --check` passed. The pytest output had 14 dependency/framework warnings
(SWIG deprecations and aiohttp app/request key notices). No production services or
providers were called. Connected attachment/discovery/RFQ/quote behavior and the
current imported-case adoption flow remain outside this checkpoint.
