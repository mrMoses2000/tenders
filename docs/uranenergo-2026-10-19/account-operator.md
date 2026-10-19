# Account pin and legacy unknown quarantine

This checkpoint adds two owner-authenticated account-control operations while keeping provider sends and application wiring unchanged.

A pin command can carry an operator idempotency key. The same session/key and matching actor, expected self JID, and reason return the committed pin version without another WAHA identity read. A different key represents an intentional new pin version. The existing function call form without a key remains supported. Pinning does not clear the account hold or circuit.

The pin operation locks the account-control row, then locks the active actor row and every active case/conversation using the session in stable case/conversation order. It rechecks that the actor remains active and owns every case before changing the pin. Ownership transfer or actor deactivation that commits before those locks causes the operation to fail. Updates that start after the row locks wait until the pin transaction ends.

Migration 045 records idempotency keys and creates immutable legacy-quarantine headers and exact reservation mappings. An inventory can include only unknown reservations that predate migration 043 and have all four identity-binding fields NULL. The operator read path returns the exact sorted reservation IDs, a SHA-256 over the canonical inventory, and age metadata. Attestation requires that full ID set, digest, a safe evidence reference, a reason, and an active owner. A changed inventory, partial list, mismatched digest, or reused key for another command fails closed.

Attestation writes only immutable audit records and mappings. It never edits reservation status, identity fields, history, or provider data. Only mapped rows are excluded from unresolved send, hold-release, and circuit-recovery predicates. Unknown rows remain included in message/contact budgets. A fresh clear provider observation is still required for circuit recovery; hold release remains a separate explicit owner action. Readback reports total unknowns, quarantined unknowns, oldest quarantined age, and unresolved count without exposing reservation IDs or account JIDs.

The module src/procurement_bot/whatsapp_operator.py provides narrow handlers that require an AuthenticatedOperatorContext supplied by trusted application middleware. The HTTP/Mini App surface is intentionally not wired by this checkpoint. The integration owner must connect the handler to the established authenticated operator entrypoint and construct the context from server-side identity, never a request-body actor ID.

Verification for this checkpoint is recorded in the branch handoff after the isolated PostgreSQL suite completes. All generated test records use synthetic IDs and localhost-only disposable PostgreSQL.
