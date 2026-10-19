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
   accounting. Evidence-only is the default and is explicitly marked as not an
   actual sourcing skip. An additional owner action may opt into a parse-hash-bound
   sourcing instruction for one exact clear request_item_id: a skip excludes that
   line, while bought/reduced quantities subtract only the explicitly stated amount
   from the next native plan. The immutable instruction IDs are bound into the run
   authorization. Candidate/ambiguous items are rejected; request_items and
   purchase/accounting rows are never rewritten.

Affected files: new owner-autonomy module, migration 044, cases.py, dialogue.py,
worker.py, research_workflow.py, mini_app.py, mini_app/app.js and focused tests/docs.
No edits to intake.py, config.py, cli.py, city_tier_policy.py, text extractor,
outbox/account-binding work, or research.py. No new job engine or provider framework.

Network access belongs to the existing executor, never the policy module. No
WhatsApp policy is created. Denial/drift leaves manual review available; timeout
and provider failure remain native queue states, never market exhaustion. First
plan uses exactly the first policy city. Ordered city scheduling and the report
checkpoint runtime hook remain explicitly pending integration; no fake SLA timer.

New intake still binds the profile automatically only when the owner has an
active matching profile. A separate authenticated Mini App adoption action now
supports an existing manual case: read-only preflight pins the applied parse hash,
profile episode and exact stable item IDs; the explicit owner POST rechecks them
under locks and captures a snapshot without rewriting the parse or item projection.
It queues the existing native prepare_research path. The production current case
has not been adopted and its photo/source records have not been imported.

Verification used synthetic cases in random schemas on the explicitly pinned,
loopback-only test_uran_integrated database (TCP 127.0.0.1:55450), guarded by
scripts/postgres_test_safety.py. The focused owner-autonomy PostgreSQL module
passed **26 tests**, including adoption, evidence-only vs effective owner skip,
original index retention and no accounting writes. PostgreSQL safety tests passed
17 cases; Ruff, Python compileall, Node --check, and git diff --check passed.
The pytest output had 16 dependency/framework warnings (SWIG deprecations and
aiohttp app/request key notices). No production services or providers were called.
The first-request supplier-draft gate remains a separate integration-lead change.
