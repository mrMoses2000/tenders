# Owner autonomy review follow-up strategy

## Current entry point and persisted flow

Telegram document intake enters worker._process_intake and cases.persist_intake_result, which binds a default-off owner profile and immutable case/parse snapshot. worker._prepare_research builds and persists the native plan, then owner_autonomy.autostart_snapshotted_research binds the snapshot to a run, consumes the normal approval helpers, starts the existing workflow and enqueues its native execute job. guard_profile_research_work checks revocation at pipeline, parallel task and execute job boundaries. The Mini App exposes owner-scoped read-only provenance and consent profile endpoints.

Owner sourcing evidence currently enters record_sourcing_disposition; the repository accepts only a source_artifacts row joined to a Telegram attachment/message, verifies case and owner, then records an immutable evidence-only disposition. It does not alter accounting or research quantities. This cannot represent an image supplied outside Telegram.

## Intended behavior

Allow one immutable intake snapshot to authorize multiple exact native runs/plans, each independently bound to its run and plan hash. Add a distinct operator-attestation evidence source for a source reference plus verified SHA-256, bound to the authenticated case owner and exact case/item, with the owner assertion and reviewed rationale retained immutably. Retain the existing Telegram artifact route. Both evidence routes stay outside accounting; exact item disposition remains an explicit owner/reviewer action and ambiguous/candidate mappings are never inferred. The workflow continues to use existing research/approval/executor boundaries and respects revocation at native job boundaries.

## Minimal affected files and compatibility

- migrations/044_owner_research_autonomy.sql: change authorization uniqueness from snapshot-only to exact (snapshot_id, research_run_id) and preserve one authorization per run; add a typed operator evidence record/source discriminant without making Telegram artifact identifiers synthetic or nullable-ambiguous; add explicit cross-field NOT NULL/CHECK invariants.
- src/procurement_bot/owner_autonomy.py: persist and validate the two evidence provenance forms through one owner/case/item gate; hash canonical evidence facts and reject replay drift. Keep evidence recording separate from plan quantity application.
- src/procurement_bot/mini_app.py: add an authenticated narrow write endpoint only if it can bind a user-provided operator reference/hash and exact item to the signed owner; do not accept case owner IDs from the request body. Existing Telegram source path remains compatible.
- tests/test_owner_autonomy_postgres.py: verify multiple exact run bindings per snapshot, Telegram and operator evidence acceptance, cross-owner/case rejection, invalid/partial provenance rejection, replay drift, no accounting writes, and existing manual/owner behavior.
- docs/uranenergo-2026-10-19/owner-autonomy-implementation.md: state that sequential city scheduling, four-hour reporting and applying sourcing dispositions to search quantities remain pending unless implemented in this slice.

No live database/config/service/provider/outbound state is touched. Provider network access stays in the existing executor. No arbitrary historical rows, images, case IDs, item mappings, purchase rows, or Telegram attachments are imported. A report checkpoint remains informational; work continues under native retry and queue policy. Verification targets the repository isolated test_uran_owner PostgreSQL cluster and focused native tests.

## Review gates

Before reporting completion, inspect original item-index validation from plan persistence through execution; run the focused PostgreSQL test module and relevant lint/syntax checks; verify the exact public GitHub branch/commit read-only. Do not claim full PDF/DOCX-to-MiniApp delivery, sequential city coverage, four-hour reporting, photo-backed quantity reduction, or production readiness unless that path is implemented and evidenced.
