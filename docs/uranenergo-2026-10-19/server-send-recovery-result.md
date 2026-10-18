# Server send recovery result — 2026-10-19

Source implementation is complete for root review. **Un-deployed: no live DB, authentication, service/configuration, hold or outbound changes.** No supplier messages, orders, reservations of goods or payments were made. Component tests do not establish that the end-to-end procurement pipeline works.

## Source checkpoints

Repository: `mrMoses2000/tenders`; isolated workspace: `/home/moses/tenders-send-recovery`; branch: `codex/uranenergo-send-recovery-2026-10-19`.

| Checkpoint | Exact SHA | Scope |
|---|---|---|
| Reviewed source base | `5f2ba1fc92afd31ebdd0d35a896e2818aa42c7e2` | Original importer source, not rerun against a private case |
| Currency fix | `a1b70a9730d3fdae4cb5601bb24d95fa47ab9f80` | Exactly intake parser and its two test files |
| Additional native currency proof | `a6e8581218d6b5973fce343a98a9e0e4ebf80281` | Extends the PostgreSQL test to seven headers, preserving the already-published currency checkpoint |
| WAHA identity binding | `6c397250982553e26606df6774004b9118519ed9` | Exactly four runtime files, migration 043, three test files and account strategy document |

The final code tree is identical to the tested tree before Git history reconciliation (`git diff d308c9c804864756cccb747ee4a072dc33ebf87a 6c397250982553e26606df6774004b9118519ed9 --exit-code` returned zero). Final documentation is an additional commit. No old migration, intake model/schema field, `INTAKE_SPEC`, city tier policy, configured extractor, provider wiring, `config.py` or `cli.py` was changed.

## Resulting behavior

Only a complete supported KZT price header produces a numeric private tender unit price. Generic, foreign or mixed headers preserve product facts and existing source evidence, leave the private price null and add the existing request warning. The native persistence test proves that generic/USD/KZT-USD/KZT-JPY/KZT-KGS/tenge-ruble headers create no financial row and still create searchable items and the prepare job. Explicit KZT retains the existing KZT financial row. Source price is not a supplier quote; no VAT logic was added. AST comparisons confirm `ParsedProcurementRequest`, `ProcurementItem` and `ProcurementRequestPatch` contracts match the reviewed base. The unchanged intake spec SHA-256 is `ca0bfca756d63b63875f0c1f8d89781ec7723d898f8c1e3fea40474249bf9d33`.

For WAHA, existing approval and delivery/account gates precede pin loading and `/me`. Identity GET occurs outside SQL, before a request ID/reservation. Transient/unavailable/malformed identity or mismatch defers without POST, reservation or persisted provider request ID. Failure and mismatch have distinct audit events. Reservation revalidates the same pin/version/session, a 0–30 second observation and the outbox lease atomically with budget/pacing/new-contact checks. HTTP acceptance must match that exact reservation's JID/version/timestamp before journal insertion. Durable acceptance precedes conversation projection.

Migration 043 rejects every partial-NULL identity tuple, malformed JID and zero bound version. Real pre-043 accepted/unknown rows retain NULL identity after migration and worker replay: accepted facts project without any provider request; unknown facts never query current-account history or POST. Pinning leaves holds/circuits intact and excludes reserved actions. Account-before-reservation locking is verified with competing PostgreSQL writers. Unknown outcomes retain sender identity; accepted/unknown terminal transitions clear the full worker/generation/expiry tuple required by migration 038.

See the [account strategy and review](../uranenergo-2026-10-18/waha-account-identity-strategy.md) for residual `/me`→POST and `/me`→history TOCTOU limitations. The official [WAHA sessions source](https://github.com/devlikeapro/waha-docs/blob/main/content/docs/how-to/sessions/index.md) was read directly because Context7 was unavailable. No real WAHA request was issued.

## Verification commands and results

Every test ran from this isolated workspace with the shared interpreter `/home/moses/tenders/.venv/bin/python` and explicit `PYTHONPATH=/home/moses/tenders-send-recovery/src`. Imports were checked to resolve under this clone. No live checkout source or `.env` was loaded.

A new disposable PostgreSQL 18 cluster was initialized under `.operator-test-pg/data` with `initdb -A trust --no-locale --encoding=UTF8`, listened only on `127.0.0.1:55447`, and used database `test_uran_recovery`. The server was held in a foreground tool session; an initial detached `pg_ctl` process did not survive its tool call, so that initial connection-refused attempt was rerun. Tests used random schemas, repository migrations and `scripts/postgres_test_safety.py`. The test server is stopped after verification; its data/logs are private scratch artifacts.

```bash
PY=/home/moses/tenders/.venv/bin/python
ROOT=/home/moses/tenders-send-recovery

env -u DATABASE_URL -u POSTGRES_DSN \
  TEST_POSTGRES_DSN=postgresql://moses@127.0.0.1:55447/test_uran_recovery \
  TEST_POSTGRES_EXPECTED_DATABASE=test_uran_recovery TEST_POSTGRES_EXPECTED_PORT=55447 \
  PYTHONPATH="$ROOT/src" "$PY" -m pytest \
  tests/test_whatsapp_account_identity.py tests/test_outbox.py \
  tests/test_whatsapp_account_controls.py tests/test_whatsapp_delivery.py \
  tests/test_supplier_dialogue.py tests/test_waha_provider.py \
  tests/test_intake.py tests/test_intake_postgres.py \
  tests/test_operator_case_import_postgres.py tests/test_chat_actors.py -q --tb=short
# 150 passed in 36.50s; no skipped tests in this selected run.

env -u TEST_POSTGRES_DSN -u DATABASE_URL -u POSTGRES_DSN \
  PYTHONPATH="$ROOT/src" "$PY" -m pytest -m 'not external' -q --tb=short
# 1055 passed, 130 skipped, 5 warnings, 4 subtests passed in 17.22s.

env -u TEST_POSTGRES_DSN -u DATABASE_URL -u POSTGRES_DSN \
  PYTHONPATH="$ROOT/src" "$PY" -m compileall -q src tests scripts
# passed
```

`ruff check` and `ruff format --check` passed for all ten changed Python files: `src/procurement_bot/{intake,outbox,providers/waha,whatsapp_account,whatsapp_send_journal}.py`, `tests/test_{intake,intake_postgres,outbox,waha_provider,whatsapp_account_identity}.py`. Git whitespace checks passed. Added-line credential/contact scanning passed across the combined patch; the only phone/JID strings are documented synthetic fixtures. Private skills, cluster files, metadata, logs, status and raw artifacts are excluded from all explicit path-based commits.

The full repository `ruff check src tests scripts setup.py` is **not green**: it reports one existing `S108` at `tests/test_agy_provider.py:83` (`/tmp/agent-home`). Running Ruff on the file read from exact base `5f2ba1f` produced the same finding. That unrelated file was not edited.

This is not a full PostgreSQL suite pass. Broad testing deliberately unset all three DSN variables, accounting for database/environment skips. Customer-specific PostgreSQL operator scenarios/scripts absent from this sanitized checkpoint and external-provider scenarios remain outside the selected native tests; no private scripts, DSNs, customer data or services were substituted. Only the exact listed PostgreSQL tests were claimed as executed.

## Git and publication handling

The sandbox mounts the original `.git` read-only: the initial stage failed with `Unable to create .../.git/index.lock: Read-only file system`. Git metadata was copied to the writable, private `.operator-git/`; all subsequent repository commands use `git --git-dir=.operator-git --work-tree=.`. The original `.git` remains at the reviewed base; reviewers should use this explicit metadata path or fetch the remote branch. No dirty live checkout or other agent workspace was touched.

Default SSH failed with `Bad owner or permissions on /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf`. Using the existing user config (`-c core.sshCommand='ssh -F /home/moses/.ssh/config -o BatchMode=yes -o ConnectTimeout=15'`) reached the authorized GitHub remote without modifying system or authentication files. An initial push was rejected as non-fast-forward because the remote already held the early currency checkpoint. The published commit was retained, its extra native tests were appended, and the unchanged WAHA tree was recommitted above it. No force push was used. Push and `ls-remote` confirmation for the final branch are recorded in the private status checkpoint and final handoff.

## Material gaps and next step

Root still needs to review the public branch. No production migration, rollout, live account identity confirmation, account pin invocation, historical unknown-send resolution or authorized outbound pilot is included. The pin repository helper needs its authenticated operator entry point wired; it does not release a hold. A normal migration rollout must recycle worker connections/prepared-statement caches; the pre-043 replay test explicitly expires them.

The next implementation patch must wire the owner/project research autostart profile, immutable case/policy snapshot, partial item readiness, native MiniApp warnings, and evidence-backed purchase sourcing dispositions. Follow the [sanitized owner autonomy proposal](../uranenergo-2026-10-18/owner-autonomy-four-hour-strategy.md), preserve the native approval/start workflow and keep research consent separate from WhatsApp consent. Integrate the separately owned city/extractor work through root review; this patch does not implement it. Buyer-as-individual policy must compare explicit final payable quotes without turning VAT into a contact/discovery gate or deducting it arbitrarily.

The four-hour checkpoint is a dated progress report. It is neither a supplier-response guarantee nor proof of market exhaustion; unfinished eligible work must remain resumable. The handoff's native case readiness counts were not queried against a live DB during this task.
