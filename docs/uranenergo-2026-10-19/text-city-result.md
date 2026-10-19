# Text and city recovery result

Checked: 2026-10-19 01:25 UTC
Branch: `codex/uranenergo-text-city-2026-10-19`
Base: `5f2ba1fc92afd31ebdd0d35a896e2818aa42c7e2`

## Delivered

- `structured_text_provider` remains `agy` by default. `codex` is an explicit operator choice; provider errors never fall back to another provider. The selected adapter is shared by intake, research profile extraction, and semantic supplier-reply parsing. Existing output models, `INTAKE_SPEC`, and persisted hashes are unchanged.
- The Codex adapter uses the existing account through an ephemeral CLI home, a schema-bound JSON envelope, read-only sandbox, fixed model/effort allowlists, bounded concurrency, timeout and output limits, process-group cleanup, and fixed redacted diagnostics. It does not persist proposals; existing caller checks remain authoritative.
- Tool/config controls were checked against the host CLI's `exec --help` and `features list`, official Codex CLI config/feature documentation, and a successful `--strict-config` synthetic call. The host CLI reports `codex-cli 0.163.0`; this does not match an older 0.162.1 note. The initial synthetic run produced CLI error events because three overrides conflicted with this CLI (Code Mode host disabled while Code Mode failed closed, the deprecated `memory_tool` key, and enabling under-development host skill discovery). Those overrides were removed; the next synthetic run passed.
- The pure city policy admits Taraz → Shymkent → Almaty in order for each item, prevents overlapping city leases for one item, and accounts for capacity shared across independent items. It does not implement database persistence or the native scheduler.

## Verification

- Synthetic greeting through the real CLI: `gpt-6.1-sol`, `xhigh`, 6.451 seconds, exact schema and both bindings valid; existing auth-file metadata was unchanged. The payload was a generic greeting only.
- Focused provider, configuration, city, intake, profile, semantic reply, and worker contracts: 1,274 passed, 6 skipped.
- Full non-PostgreSQL, non-external suite: 2,198 passed, 21 skipped, 80 deselected.
- Focused post-fix adapter/provider/auth/city tests: 1,181 passed, 6 skipped.
- Ruff on changed runtime, test, and probe files: passed.
- Disposable PostgreSQL 18 on loopback port 55449, database `test_uran_text`, with the repository's expected database/port guard: 65 passed, 15 failed, 2,219 deselected. Observed failures were in WhatsApp/account transport and pipeline-review tests outside this branch. No base-branch comparison was run, so introduction status is untriaged; root should compare them during the combined A+B+C run. The disposable cluster was stopped and its temporary data directory removed. No other database or service was used.

## Limits and handoff

- A bounded synthetic 55-item `ParsedProcurementRequest` input measured 48,254 bytes. The real CLI ended after 4.414 seconds with fixed diagnostic `cli_home_missing`; auth-file metadata stayed unchanged. The result was rejected and did not validate 55-item extraction capacity. Do not infer a per-request SLA from the successful greeting probe; no retry was made.
- AGY audio remains separate from text selection. The current audio transcript carries no language-confidence value, so `transcript_review_reason` keeps supplier voice messages in human review; no AGY voice confidence was fabricated and the review gate was not bypassed.
- Native city admission still needs the persistence/scheduler increment: atomic complete-snapshot loading, per-item fencing, shared capacity locking, cursor/checkpoint persistence, and enqueueing existing work. Production activation remains for root review.
- No production database, `.env`, business document, outbound provider, authentication state, or running service was changed.

## Commits

- City policy: `36a4c29df5900ed640017c258d67fcd99e4222da`
- Structured text provider and synthetic probe: `d0ac6f58559175a4f1b0e77270bccc9141795a32`
