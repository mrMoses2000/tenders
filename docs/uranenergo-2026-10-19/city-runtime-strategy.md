# Native city-tier runtime strategy — 2026-10-19

Status: recorded before runtime/schema edits. Isolated branch codex/uranenergo-city-runtime-2026-10-19 at c7e4294c58e40eb179b4f15418fe5f9e1f04def4. Synthetic PG only; 045 belongs to account work, this branch reserves additive 046. No live data, services, credentials, supplier contacts, or outbound activity.

## Existing flow and gap

owner_autonomy.py captures immutable case/parse policy snapshots, binds a policy authorization per research_run_id, checks revocation/drift and starts through native approval/start_approved_research. research_workflow.py consumes approval, sets case_pipeline_runtime.research_run_id and enqueues execute_research. worker.py executes/completes; autonomous_pipeline.py ticks the current runtime. city_tier_policy.py is a pure ordered selector with success budgets/provider pauses/reply independence, but has no durable per-item cursor, later-city admission, current-run fence or four-hour report dispatch; its case-wide model alone cannot progress unaffected items around one blocked item.

Canonical case, parse version, source payload/hash, original item UUIDs and geography remain authoritative. Policy cities are exactly Тараз, Шымкент, Алматы; current canonical case city is Тараз. Map only exact Russian values and bounded aliases Taraz/Shymkent/Almaty to enum IDs. Unknown city fails closed. Never transliterate or rewrite source data.

## Intended state and behavior

Keep the single case_pipeline_runtime.research_run_id pointer and immutable historical runs. Each city run batches all eligible original request_item_ids for that city. Track tier independently per item: no item overlaps cities, while eligible others advance if one is blocked. Later batches include only items whose prior city is confirmed terminal. Pending replies are separate dialogue work and never block discovery progression.

Migration 046 adds immutable per-run admission binding (snapshot/run IDs, tier index, exact Russian policy city and enum city, plan hash, source parse/payload hashes, ordered original item UUIDs, admission digest), immutable per-original-item/per-city outcomes (status, terminal proof, counters/reason), and per-item active-run/tier fence. Validate owner/case/parse/snapshot hashes, city at authorized policy index, exact run plan/hash/city projection, original item membership, tier predecessor and one active lease per item. Arbitrary city cannot bypass policy. Ephemeral later-city plans change only the search city and eligible subset; keep source hashes and item UUID/index mapping. Never mutate canonical data. Approval, admission, pointer and native job enqueue must be atomic using existing transactions/locks or fail closed.

Statuses distinguish satisfied, exhausted, budget_complete, blocked, paused, incomplete, failed, not_started. Only validated successful completion or successfully consumed explicit policy budget proves exhaustion. Auth/CAPTCHA/quota/timeout/provider error/partial/stale/unknown remain resumable, never exhausted. Full source-bound owner skip/purchase and source-backed technically verified comparable offers satisfy later-tier search. Partial quantity and evidence-only do not; retain original UUID and don't imply reduced demand verified. Every transition checks active owner/profile, revocation, snapshot/parse/plan hashes, current item IDs and holds. Stale run completion may update only its own result and cannot replace current pointer or advance a moved item fence. Reuse existing consent/auth/approval; add city admission binding, not a new consent.

The four-hour checkpoint is independent of deadline and all-city completion. Migration 046 stores a unique case/snapshot/start-generation checkpoint and report/outbox identity/state. Native tick creates/reuses one partial versioned report plus one owner Telegram outbox event via existing helpers with checkpoint-id idempotency; replay/restart cannot duplicate either or stop search. Existing hold/revocation delivery rules still apply. If generation/outbox cannot be atomic with current helpers, separate idempotent jobs and record that boundary.

## Scope and verification

### Native boundaries verified before implementation

- `owner_autonomy.autostart_snapshotted_research` holds the case start advisory
  lock and one transaction; `persist_research_plan` keys runs by case/plan hash.
  `bind_research_policy` currently permits only the first city. Extend binding
  validation only through an immutable city admission; keep legacy first-city
  and manual approvals compatible.
- `research_workflow.start_approved_research` locks approval/run/case, consumes
  approval, changes lifecycle, replaces the single runtime pointer, writes the
  start event and enqueues `execute_research` in the same transaction. Reserve
  item fences here, before consumption. Only admitted city starts may continue
  from `researching`/`contacting`; ordinary follow-up contracts stay unchanged.
- `ParallelResearchRunExecutor.execute_task` validates native job lease and
  checkpoint owner generation before persisting provider results. Its
  `_terminal_result_if_ready` delegates to `ResearchRunExecutor._terminalize`.
  The latter currently changes the case regardless of the current pointer:
  admitted city runs must terminalize only themselves. Worker
  `_finish_research_result` and the native tick reconcile durable item results;
  neither a returned executor status nor `partial` alone proves exhaustion.
- Checkpoints expose `source_exhausted`, `own_database_reused`,
  `semantic_intents_exhausted`, access/provider errors, cursors and leases.
  Require every expected query/checkpoint, no live lease/open work, and no
  failed/incomplete semantic intent/card. Actual provider-confirmed source end
  alone yields exhausted. At each admission, immutably snapshot the native
  executor's already-reviewed per-item successful-work cap (target suppliers and
  maximum successful batches per query); fully successful target/cap completion
  yields distinct budget_complete, never exhausted. Failed, blocked, paused,
  timed-out, quota/CAPTCHA/auth, incomplete, or no-progress work consumes no
  success budget. Preserve legacy policy hashes and do not add a new consent
  requirement for existing profile snapshots.
- On a rolling upgrade, the runtime pointer can already reference an approved
  first-city Taraz run created under migration 044 before 046 exists. A narrowly
  fenced bootstrap may attach only that exact current run when its live owner
  authorization, snapshot, applied parse/hash, immutable plan/hash, city and
  original item UUID/index projection all validate; it inserts only 046 admission
  and cursor/outcome rows, never rewrites source/auth/plan or creates a duplicate
  run. Ambiguous or drifting runs fail closed. Test an active 044 run, apply 046,
  recover the exact original run, and only then advance eligible items.
- Serialize city selection with the same case start lock. Reconcile every
  original item separately, then batch all eligible items at the earliest
  eligible tier. Resumable items retain their old run/city fence. Once other
  eligible work finishes, restore an unresolved admitted run as the case pointer
  for native recovery; only its still-fenced items may execute. Provider network
  access remains exclusively in the existing executor, outside SQL transactions.
- Further resume inspection: native `resume_run` only requeues `paused` branches;
  successful checkpoints carrying CAPTCHA/access/incomplete stop reasons otherwise
  have no resume path. Extend that existing explicit resume boundary for admitted
  runs: revalidate consent/source, refuse to displace open discovery, prepare only
  still-fenced unresolved items as paused, then reuse native generation increment
  and queue admission. Keep cursors, pages, receipts and prior outcomes intact.
  This is explicit resume, not automatic exhaustion or a new provider retry loop.
- `run_pipeline_tick` recovers current-run branches and runs dialogue/report
  scheduling. Add city reconciliation before recovery and the checkpoint before
  any city pointer change. Pending dialogue replies never enter eligibility.
- `build_case_report` uses a case advisory lock, versions artifacts, and inserts
  report event plus Telegram document outbox within its transaction. A checkpoint
  uses a fixed persisted run and refresh key, always a partial snapshot, and
  suppresses optional top-five messages so it has exactly one owner event.
  File creation is content-addressed but outside database rollback; a failed
  transaction can leave an unreferenced local file, never a published artifact.
  Run checkpoint generation in a savepoint: failure keeps the checkpoint pending
  while city progression/recovery continues. Checkpoint document outbox keys use
  the dedicated `telegram.city-checkpoint` namespace for delivery gating.
- Telegram outbox currently has no policy/hold delivery check. Add a check only
  for checkpoint-linked events at the existing send boundary. Database identity
  is exactly-once; external Telegram delivery retains native retry semantics
  and cannot promise exactly-once network delivery after an uncertain response.

The exact route opts into this slice; other existing owner city policies retain
their native behavior. Snapshot cities remain the exact Russian strings. Bounded
aliases exist only in the enum adapter, never in persisted policy/source fields.

Likely files: new city_tier_runtime.py and migration 046; targeted owner_autonomy.py/research_workflow.py admission hooks, worker.py terminal hook, autonomous_pipeline.py report hook, new focused PG test. First map trigger conventions, terminal/recovery, report versioning and outbox; amend this strategy before dependent contract edits. No unrelated API/canonical/WhatsApp/text code.

Use only 127.0.0.1:55453/test_uran_city, random schema, synthetic owner/case/items, fake research and Telegram boundary. Cover exact aliases/order, batched original IDs/indexes, three tiers, full skip/comparable exclusion, partial/evidence non-exclusion, one blocked among advancing items, reply independence, resumable failure/quota/CAPTCHA, arbitrary city/order/hash/revocation/hold rejection, lease/replay/stale-fence races, crash/restart, exactly-once four-hour report/version/outbox without stopping search. Assert durable hashes/IDs/pointers and fake calls; unit selector tests are not end-to-end proof. Run focused tests/lint/syntax and guarded isolated PG only; no production activation claim.

## Preflight refinement — preserve imported raw item text

A read-only audit of the active snapshot `64cd42eb-8c15-4f15-80f1-96b526ed5352`
found that the legacy operator importer stores raw `request_items.specification_text`,
while the applied normalized payload models a sole whole-spec requirement named
`Техническая характеристика` whose `expected_value` is that same raw text. The source
projection may therefore accept either the current native `_requirement_text` rendering
or this exact, narrowly typed imported form (one requirement only, exact name, string
value exactly equal to the stored text). Do not strip prefixes, normalize whitespace,
or ignore text identity. Keep exact original UUID/name/quantity/unit/analogs checks and
full canonical `item_requirements` equality, including hard/source fields. Add synthetic
coverage for accepted importer form and rejection of raw-text or canonical hard-requirement
drift. The production audit uses a short repeatable-read transaction with row-share locks
and no DML because `load_valid_snapshot` itself issues `SELECT FOR SHARE`, which PostgreSQL
rejects in a strictly READ ONLY transaction; distinguish this no-write intent from the
server's `READ ONLY` transaction mode in the result.

## Preflight refinement — preserve imported raw item text

A read-only audit of the active snapshot `64cd42eb-8c15-4f15-80f1-96b526ed5352`
found that the legacy operator importer stores raw `request_items.specification_text`,
while the applied normalized payload models a sole whole-spec requirement named
`Техническая характеристика` whose `expected_value` is that same raw text. The source
projection may therefore accept either the current native `_requirement_text` rendering
or this exact, narrowly typed imported form (one requirement only, exact name, string
value exactly equal to the stored text). Do not strip prefixes, normalize whitespace,
or ignore text identity. Keep exact original UUID/name/quantity/unit/analogs checks and
full canonical `item_requirements` equality, including hard/source fields. Add synthetic
coverage for accepted importer form and rejection of raw-text or canonical hard-requirement
drift. The production audit uses a short repeatable-read transaction with row-share locks
and no DML because `load_valid_snapshot` itself issues `SELECT FOR SHARE`, which PostgreSQL
rejects in a strictly READ ONLY transaction; distinguish this no-write intent from the
server's `READ ONLY` transaction mode in the result.
