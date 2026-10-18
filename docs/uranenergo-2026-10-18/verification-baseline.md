# Verification baseline and source gap — 2026-10-18

## Local source checkout baseline

In the existing dirty local checkout, the dirty source checkout was on branch `codex/pipeline-v3-event-core` at `b4acafe55c36fb5c2e06d0886baec2d6595e4c62`. The working tree held broad uncommitted/untracked code changes, tests, migrations and generated artifacts. This was a local verification of that working tree, not of GitHub `main`, a clean release branch or the production server.

Before running the non-database suite, the environment check reported `TEST_POSTGRES_DSN`, `DATABASE_URL` and `POSTGRES_DSN` all absent. `tests/conftest.py` returns without opening a connection when `TEST_POSTGRES_DSN` is unset. Tests that require that explicit variable skip themselves. The command also deselected tests marked `postgres` and `external`:

```sh
.venv/bin/python -m pytest -q -m 'not postgres and not external'
```

Result: **1075 passed, 10 skipped, 78 deselected, 5 warnings, 4 subtests passed in 32.07s**. The output contained only five PyMuPDF/SWIG deprecation warnings. This command did not connect to PostgreSQL or call external services.

The focused local Codex Chrome failure-trace file was then run separately:

```sh
.venv/bin/python -m pytest -q tests/test_codex_failure_traces.py
```

Result: **11 passed in 3.48s**. The test exercises a pinned Node executable path and checks that a secret sentinel is absent from the child environment. These results apply to the local dirty checkout at the time of each run; they do not prove server behavior or that the corresponding uncommitted code is present in a clean GitHub worktree.

No full PostgreSQL integration suite or `scripts/run_full_checks.sh` was run because no disposable PostgreSQL target was configured. No production database was used.

## GitHub base compatibility

The clean docs branch starts at public `origin/main@7ea41c0`. A read-only tree check found that this base lacks these newer modules and migrations which exist in the local dirty source state:

- `src/procurement_bot/providers/codex_chrome.py`
- `src/procurement_bot/configured_extractor.py`
- `src/procurement_bot/autonomous_pipeline.py`
- `src/procurement_bot/manual_quotes.py`
- `migrations/036_autonomous_pipeline.sql`
- `migrations/037_whatsapp_send_acceptance_journal.sql`
- `migrations/042_pipeline_reviews.sql`

Therefore the docs branch is not a valid base for a standalone pipeline deployment or for applying an isolated provider patch that depends on absent files. Build future code commits on an explicitly reviewed code base and include the exact required modules/migrations in the same curated patch. First compare pre-change hashes with the server's live checkout; do not assume remote `main`, the local dirty branch or the server checkout are equivalent.

The exact full pytest stdout was not retained as a repository artifact. The counts and command above are the sanitized result recorded from the tool output. The private source baseline is stored in a protected local backup outside the repository; it contains only selected `src/`, `tests/`, `migrations/`, `pyproject.toml` files and a SHA-256 manifest, not `.env`, generated outputs, screenshots or database artifacts.

## Isolated sanitized publication-copy candidate

A separate worktree was populated from an explicit source/test/migration/packaging/UI allowlist and sanitized there; the dirty source checkout was not edited by this process. The candidate maps contact-number and UUID examples consistently, generalizes machine-specific paths and owner handles, and replaces stored consent text with a synthetic non-authorization example. It is a source checkpoint for review, not a production release or a self-contained server deployment. Root review is required before it is committed.

The clean-copy test command used the candidate `src/` directory via `PYTHONPATH`, with `TEST_POSTGRES_DSN`, `DATABASE_URL` and `POSTGRES_DSN` unset:

```sh
env -u TEST_POSTGRES_DSN -u DATABASE_URL -u POSTGRES_DSN \
  PYTHONPATH="$PWD/src" /path/to/existing-venv/bin/python \
  -m pytest -q -m 'not postgres and not external'
```

Result after publication-copy sanitization: **1,018 passed, 10 skipped, 77 deselected, 5 warnings, 4 subtests passed**. The imported `procurement_bot` resolved from the isolated worktree. This still skips all tests marked `postgres` or `external` and does not establish production readiness.

The candidate excludes customer-specific reconciliation fixture/test data, the webhook configuration helper/test/service that imports an excluded operator-control script, and the backup-fetch helper/test containing an operator host/path. Uran-specific launch and in-progress case-import operator tests/scripts are also out of scope. These exclusions leave the associated operational capabilities unverified in the candidate. No PostgreSQL integration, live server check, deployment, WAHA send, or four-hour run was performed.
