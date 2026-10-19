# Text and city recovery result

Checked: 2026-10-19 01:25 UTC
Branch: `codex/uranenergo-text-city-2026-10-19`
Base: `5f2ba1fc92afd31ebdd0d35a896e2818aa42c7e2`

## Delivered

- `structured_text_provider` remains `agy` by default. `codex` is an explicit operator choice; provider errors never fall back to another provider. The selected adapter is shared by intake, research profile extraction, and semantic supplier-reply parsing. Existing output models, `INTAKE_SPEC`, and persisted hashes are unchanged.
- The Codex adapter uses the existing account through an ephemeral CLI home, a schema-bound JSON envelope, read-only sandbox, fixed model/effort allowlists, bounded concurrency, timeout and output limits, process-group cleanup, and fixed redacted diagnostics. Its configurable timeout remains 45 seconds by default with a reviewed upper bound of 180 seconds. It does not persist proposals; existing caller checks remain authoritative.
- The adapter removes Pydantic `pattern` and `default` keywords from the provider-facing schema because the Codex CLI rejects the lookaround regex generated for Decimal constraints. It still validates the response against the unchanged Pydantic model, so the local numeric constraints remain enforced. Structured `invalid_json_schema` events now map to a fixed diagnostic ahead of stderr heuristics; broad `CODEX_HOME` matching was removed because it misclassified a nonfatal PATH-alias warning.
- Tool/config controls were checked against the host CLI's `exec --help` and `features list`, official Codex CLI config/feature documentation, and a successful `--strict-config` synthetic call. The host CLI reports `codex-cli 0.163.0`; this does not match an older 0.162.1 note. The initial synthetic run produced CLI error events because three overrides conflicted with this CLI (Code Mode host disabled while Code Mode failed closed, the deprecated `memory_tool` key, and enabling under-development host skill discovery). Those overrides were removed; the next synthetic run passed.
- The pure city policy admits Taraz → Shymkent → Almaty in order for each item, prevents overlapping city leases for one item, and accounts for capacity shared across independent items. It does not implement database persistence or the native scheduler.

## Verification

- Synthetic greeting through the real CLI: `gpt-6.1-sol`, `xhigh`, 6.451 seconds, exact schema and both bindings valid; existing auth-file metadata was unchanged. The payload was a generic greeting only.
- Focused provider, configuration, city, intake, profile, semantic reply, and worker contracts: 1,274 passed, 6 skipped.
- Full non-PostgreSQL, non-external suite: 2,198 passed, 21 skipped, 80 deselected.
- Current focused adapter, provider configuration, authentication readiness, city policy, intake, semantic reply, and worker tests: 1,256 passed, 7 skipped. The focused extractor/config subset was separately 77 passed.
- Ruff on changed runtime, configuration, tests, and probe files: passed.
- Disposable PostgreSQL 18 on loopback port 55449, database `test_uran_text`, with the repository's expected database/port guard: 65 passed, 15 failed, 2,219 deselected. Observed failures were in WhatsApp/account transport and pipeline-review tests outside this branch. No base-branch comparison was run, so introduction status is untriaged; root should compare them during the combined A+B+C run. The disposable cluster was stopped and its temporary data directory removed. No other database or service was used.

## Limits and handoff

- The first synthetic 55-item request exposed a provider-facing Decimal `pattern` containing unsupported lookaround. The old classifier mislabeled the stderr warning about temporary `CODEX_HOME` PATH aliases as `cli_home_missing`; no credentials or raw diagnostics were exposed. Regression tests now cover both fixes.
- After the schema fix, a 12,063-byte synthetic request reached generation but exceeded the original 45-second timeout. Two bounded native-schema retries used the 48,254-byte synthetic fixture and reviewed 180-second cap: `xhigh` ended as `cli_timeout` at 180.077 seconds, and `high` ended as `cli_timeout` at 180.119 seconds. Auth-file metadata stayed unchanged on each. The schema was no longer rejected as invalid, but neither attempt produced a validated result, so 55-item capacity at this payload scale remains unverified. Further live retries were stopped; do not infer a per-request SLA.
- AGY audio remains separate from text selection. The current audio transcript carries no language-confidence value, so `transcript_review_reason` keeps supplier voice messages in human review; no AGY voice confidence was fabricated and the review gate was not bypassed.
- Native city admission still needs the persistence/scheduler increment: atomic complete-snapshot loading, per-item fencing, shared capacity locking, cursor/checkpoint persistence, and enqueueing existing work. Production activation remains for root review.
- No production database, `.env`, business document, outbound provider, authentication state, or running service was changed.

## Commits

- City policy: `36a4c29df5900ed640017c258d67fcd99e4222da`
- Structured text provider and synthetic probe: `d0ac6f58559175a4f1b0e77270bccc9141795a32`
