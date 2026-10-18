# WAHA account identity binding — implementation strategy and review

Status: source implementation for review; not deployed. No live API, DB, authentication, account hold, or outbound operation was performed.

## Contract and affected path

`WhatsAppOutboxWorker.run_once` owns provider access. The existing exact consumed approval, supplier-safe text, contact provenance, opt-out, quiet hours, conversation order, case policy and account gates remain in force. A session name such as `default` identifies a WAHA session, not its physical sending account.

The adapter reads `GET /api/sessions/{session}/me` and accepts only the associated `id` in the numeric `@c.us` JID form. It never pins `pushName`. A null result means that a usable authenticated identity is unavailable. The official [WAHA sessions documentation](https://github.com/devlikeapro/waha-docs/blob/main/content/docs/how-to/sessions/index.md) documents this endpoint and also exposes `me.id` in session details. Context7 was unavailable in this tool session; the official source was read directly. All implementation tests use fake transport.

The owner pin helper verifies the active actor owns every active case using the session, fetches `/me` outside SQL, requires the exact owner-confirmed JID and a fresh observation, rechecks authorization, and locks the account before changing its monotonic pin version. It never clears a hold or circuit. It refuses changes while any action remains reserved, including an expired lease awaiting reconciliation. Initial pinning may coexist with historical unknown NULL rows; these remain held and unresolved. This patch adds the repository helper; an authenticated operator UI/CLI for invoking it remains to be wired.

For a new action:

1. Validate the existing approval and delivery/account gates and the client session.
2. Load the pinned JID/version. Fetch `/me` outside a SQL transaction, before `new_message_id` and before any physical action reservation.
3. On an unavailable, null, malformed, 429/5xx or timed-out identity, append `account_identity_check_failed` and defer without a request ID, reservation or POST. On a different valid JID, append `account_identity_mismatch` and defer the queued action. Neither denial drains the queue or changes holds/circuits.
4. Allocate the provider request ID. In one `reserve_outbound` transaction, revalidate session, exact pin/version, existing authorization and delivery gates, bounded observation age (0–30 seconds), account budget, new-contact limit, pacing, concurrency and outbox lease. Store pinned JID/version plus observed JID/time with the reservation and request ID atomically.
5. POST outside SQL. Before journal insertion, prove the exact action's reservation JID/version/timestamp and persisted request ID/payload. Commit durable acceptance before projecting conversation evidence.

Account identity, its owner and audit observations stay in private database records; the account state projection exposes pin presence/version without the JID. Public fixtures contain synthetic identities only. The affected files are the account repository, outbox worker, WAHA adapter, acceptance journal, migration 043, their three test files, and this document. No configuration, provider wiring or intake model contracts change.

## Compatibility and failure handling

Migration 043 adds nullable identity columns with no historical backfill. Pin absence uses version zero in controls; old reservation/journal identity tuples stay all NULL. Every bound tuple field is explicitly non-null in CHECK constraints (PostgreSQL accepts a NULL CHECK result otherwise). All partial tuples, malformed JIDs and zero bound versions are rejected. A bound reservation requires the observed JID to equal its pinned JID. Migrations 001–042 remain unchanged.

An existing accepted journal with all NULL identity is already acceptance proof: replay projects it without `/me`, history fetch or POST under the current account. An unknown NULL action never queries current-account history or reposts. Bound unknown actions can query only after `/me` matches their recorded sender; missing/mismatched evidence leaves them unknown. Crash after reservation is uncertain dispatch, with no separate not-dispatched state. Unknown actions retain their identity, consume budget and open/retain the circuit.

Pin, reserve, acceptance validation and outcome updates lock account before reservation. Terminal/expired lease transitions clear the entire existing worker/generation/expiry tuple together; retaining a generation with a NULL worker/expiry violated migration 038's CHECK and rolled back acceptance in real PostgreSQL tests.

External session relinking between `/me` and POST cannot be atomic across separate HTTP operations. The observation and pin version reduce risk and provide evidence, but cannot eliminate that TOCTOU interval. The same limitation applies between identity verification and a history fetch. Database migrations require normal worker/connection recycling; the migration replay test explicitly expires old prepared-statement caches.

## Verification evidence

Real tests migrate disposable PostgreSQL schemas, seed pre-043 accepted and unknown actions, apply 043, and assert NULL identities remain NULL before and after worker replay. Worker tests execute native approval/delivery/account/reservation/journal/projection SQL with fake WAHA transport: identity 503/429/timeout then successful retry and exactly one POST; missing/malformed/null/mismatched identity; stale/future observations; pin/session/lease races; active-reservation pin exclusion; exact acceptance tuple rejection; crash/unknown identity preservation; and process restart after projection failure without a duplicate POST. The complete partial-NULL matrices are tested for controls, reservations and journal. Quiet hours, opt-out, exact approval, session, hold/circuit, total budget and new-contact limits have worker-level regression coverage.

These component tests do not prove a live WAHA account, resolve historical sends, or establish an end-to-end procurement pipeline. Exact commands, counts, commit SHAs and remaining integration work are recorded in the server recovery result document.
