# WhatsApp account operator controls — review summary

The authenticated Mini App now lets an owner inspect account pin, hold, circuit, provider, and reservation readback; explicitly pin a verified WhatsApp identity; and inspect and attest an exact snapshot of pre-043 identity-unbound unknown reservations. The UI masks the pinned JID, shows hold and circuit separately, requires explicit snapshot confirmation, and does not offer hold/circuit release actions.

Runtime routes reuse signed Telegram Mini App authentication and pass the middleware-resolved owner identity to the existing operator service boundary. Writes require the configured HTTPS origin and exact request fields. Pin and quarantine requests retain payload-bound idempotency keys across retries. Transactional account/case/actor checks remain authoritative; the attestation adds immutable evidence without rewriting reservations or changing budget visibility, hold, or circuit state. No new migration is introduced by this wiring commit; it builds on migration 045 already present in the base.

Verification on the isolated server checkout and disposable PostgreSQL service at 127.0.0.1:55451: 47 selected API, stage UI, identity, and account-control tests passed. Ruff, Python compileall, Node syntax check, and git diff --check passed. Pytest reported three existing aiohttp AppKey warnings. The test pin endpoint uses a stubbed synthetic identity response; no provider call, production database, service restart, or deployment was performed.

Production activation still requires integration-owner review and the separately authorized release process. This branch is a review checkpoint, not a deployment claim.
