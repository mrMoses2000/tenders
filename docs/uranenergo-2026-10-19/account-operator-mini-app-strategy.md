# Account operator Mini App/API strategy — 2026-10-19

## Current entry point and flow

mini_app.py authenticates /api/* requests with signed Telegram Mini App initData, checks access grants, resolves an active users.id, and stores it in request[owner_id]. New handlers pass only that trusted identity into whatsapp_operator.py; request JSON never selects an actor. whatsapp_account.py owns the pin and legacy-unknown attestation transactions. The Mini App uses same-origin api()/apiWrite() fetch helpers with Authorization: tma ... and no cookies. The WAHA client is constructed only for an explicit pin action and its identity GET is read-only; no sends or provider mutations are introduced.

## Intended behavior

Add authenticated readback for owned account/session controls and exact legacy-unknown inventory; add explicit, idempotent pin and exact-inventory attestation commands. Pin readback masks the account JID. Inventory review binds the exact reservation IDs and canonical inventory SHA-256, with source reference and reason; the operator confirms the exact snapshot before submitting. Quarantined rows remain unchanged and budget-visible. The interface presents hold and circuit independently and offers no release controls. Only authenticated owners may act, and transactions recheck actor activity and case ownership after taking the account lock. Same-key retries return prior results; fresh intentional actions require a new key.

## Compatibility and persistence

Existing authentication, owner scoping, and account state remain authoritative. API additions are authenticated-only and same-origin for writes; strict field allowlists reject spoofed actor fields and unexpected data. No migration is planned in this wiring change; migration 045 and its immutable evidence model already exist. The inventory query must omit already-attested exact legacy rows to make a subsequent inventory actionable and avoid duplicate attestations. No existing reservation, outbox event, message, case, or hold/circuit state is rewritten by these endpoints.

## Files and scope

Runtime: mini_app.py routes/projections and narrowly scoped ownership recheck in whatsapp_account.py; existing whatsapp_operator.py service boundary is reused. UI: mini_app/index.html, mini_app/app.js, mini_app/app.css, with account-only DOM/state/functions separate from stage grouping and diagnostics. Tests: new Mini App route tests plus focused account-control tests using only the isolated PostgreSQL test service on port 55451. Documentation: this strategy and sanitized operator workflow notes.

## Concurrency, failures, and privacy

The account-control row is locked before the active actor and every affected case link, matching the pin/quarantine lock order. Ownership loss, actor deactivation, stale inventory, bad Origin, invalid schema, and provider failure abort without changing account state. Provider failure responses remain generic and no provider payload, API key, full account JID, reservation body, or raw message is returned or logged. Idempotency keys are generated once per explicit UI action and retained for retry. A pin never clears an operator hold or open circuit. The attestation is exact-snapshot-only; any new or partially identity-bound unknown remains blocking.

## Verification and limits

Run focused API/UI/account tests and the real PostgreSQL ownership/idempotency tests against the disposable port-55451 database, then repository lint and syntax checks. Tests may use a stubbed read-only identity response; no live WAHA call, production database, service restart, or deployment is authorized by this change. Passing tests validates the isolated code path only; production activation remains a separate review.
