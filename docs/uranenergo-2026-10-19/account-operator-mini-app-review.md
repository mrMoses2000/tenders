# WhatsApp account operator and held-research draft scheduling — review summary

The authenticated Mini App lets an owner inspect account pin, hold, circuit, provider, and reservation readback; explicitly pin a verified WhatsApp identity; and review and attest an exact snapshot of legacy identity-unbound reservations. It masks the pinned JID, displays hold and circuit separately, requires exact-snapshot confirmation, and does not expose hold/circuit release.

During active native research, a tick can now enqueue the existing coalesced draft-preparation job once a persisted exact, confirm, or lead offer exists and the WhatsApp and auto-preparation settings are enabled. This lets the native workflow create the conversation needed for owner pinning while the account remains held or unpinned. It does not create reminders, reacquire dialogue slots, approve a draft, or send a message. The existing approval, pin, account-health, hold/circuit, recipient, and budget checks at physical delivery remain in place.

The routes use signed Telegram Mini App authentication and the middleware-resolved owner identity; writes require the configured HTTPS origin and exact request fields. Pin and quarantine requests keep payload-bound idempotency keys across retries. Transactional account/case/actor checks remain authoritative. Attestation adds immutable evidence without rewriting reservations or changing budget exposure, hold, or circuit. The isolated integration source includes the previously reviewed additive migration 045; the UI and scheduler follow-up add no schema change. Migration 045 has not been applied in production.

## Verification

On isolated source SHA `22a1312` plus this documentation update, the connected workflow and account identity suites passed **46/46** on verified loopback `127.0.0.1:55450/test_uran_integrated`; the guarded Mini App PostgreSQL API test passed **1/1** on `127.0.0.1:55451/test_uran_account_operator`; and five held-research tick cases passed on `55450`. The connected suite covers the native owner flow and confirms a source-bound skip of the first item preserves the original second item UUID through plan retry and offer persistence. Ruff, Python compileall, Node syntax, and diff checks passed. Pytest reported dependency deprecation and existing aiohttp AppKey warnings.

Tests used synthetic records, generated documents, and stubbed provider boundaries. No production database, live provider call, service restart, deployment, or outbound message was involved. Earlier broad tests had 15 failures that were reproduced on clean baseline `5f2ba1f`; the full broad set was not rerun after this merge, so those failures are not newly attributed or cleared here.

## Remaining review gates

The production Taraz run is currently blocked by a search-profile loader conflict and the fix is being prepared on a separate activation branch. Do not package this integration source until that exact fix is reviewed and merged. The current checkpoint does not implement city 046, four-hour reporting, partial-quantity purchase comparison, or production quarantine/pin/hold actions. This is an isolated integration result, not a deployment or end-to-end production claim.
