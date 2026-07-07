# Product and messaging architecture

Date: 2026-07-07

## Product boundary

This is not only a helper for NIS school tenders. The product is a procurement request operating system for Kazakhstan organizations.

Supported customer types:

- Nazarbayev Intellectual Schools and other schools;
- colleges and universities;
- clinics and medical organizations;
- offices and private companies;
- public-sector bodies;
- contractors and one-off project customers.

The common unit is not "school". The common unit is:

```text
customer organization -> request/application -> line item -> technical requirement -> supplier candidates -> validated purchase option -> delivery/acceptance status
```

## Are we building a unique product?

Practically, yes.

Existing open-source tender systems usually solve another problem: publishing a tender, accepting supplier bids, or running an ERP purchasing workflow. Our workflow is different:

- reconstruct messy specs from DOCX/PDF/photos/Notion;
- search real Kazakhstan supply across online and offline-ish sources;
- compare offers against hard technical requirements;
- track price ceilings, stock, logistics and document readiness;
- keep Notion as the operational source of truth;
- prepare supplier/customer communication with evidence.

The product moat is not a single algorithm. It is the combined workflow: Notion schema, category-specific validation, supplier memory, local-market search patterns, evidence discipline, and human approval checkpoints.

## WhatsApp integration options

### Option A: official WhatsApp Business Platform / Cloud API

Best for production.

Pros:

- official Meta-supported API;
- webhooks for inbound messages and statuses;
- clearer production compliance path;
- better fit for business-initiated supplier/customer communication.

Cons:

- setup is slower: business account, phone number, app, templates, permissions;
- outbound conversations may require approved message templates depending on timing and conversation type;
- stricter policy and rate-limit discipline.

Recommended use:

- long-lived procurement assistant;
- customer/supplier outreach where account reliability matters;
- auditable outgoing messages from the system.

### Option B: Baileys-style WhatsApp bridge from `shermos-bot`

Good for a pilot or internal operator workflow, not ideal as the final production dependency.

Observed reusable pieces in `/Users/mosesvasilenko/shermos-bot`:

- `whatsapp-bridge/` Node service;
- Redis-backed Baileys auth state;
- two-number routing pattern: client and manager bridges;
- inbound HTTP forwarding into Python API;
- Redis spool for inbound retries;
- shared-secret bridge authentication;
- operator runbook for pairing/re-pairing;
- systemd service model for Ubuntu.

Risks:

- Baileys is not the official WhatsApp Business API;
- linked-device sessions can break and require re-pairing;
- account policy risk is higher than the official API;
- production reliability depends on bridge health and phone/session state.

Recommended use:

- fast proof of concept;
- internal manager notifications;
- manual/approved supplier outreach while the official API is not ready.

## Telegram procurement agent

The pattern in `/Users/mosesvasilenko/Rudolf_music_site/services/telegram-bot` is a strong starting point:

- Telegram webhook server;
- allowlist/self-auth model;
- voice/photo/text handling;
- Codex CLI child process;
- confirmation buttons before changes;
- git diff guardrails;
- bounded editable files;
- service can run persistently on Ubuntu.

For procurement, adapt the pattern instead of copying it as-is.

Target Telegram flow:

1. User sends a request, photo, voice note, or Notion path.
2. Bot creates a procurement task and returns an acknowledgement.
3. Worker runs sourcing/validation asynchronously.
4. Bot sends summarized findings:
   - top supplier candidates;
   - blockers;
   - draft WhatsApp/call scripts;
   - Notion update summary.
5. For any outbound supplier/customer message, bot asks for explicit approval.
6. Only after approval does the messaging service send the text.

## Ubuntu deployment options

### Simple persistent bot

Use one Node or Python service under systemd.

Good when:

- low traffic;
- one operator;
- jobs can run one at a time;
- Notion + search + message draft is enough.

Limitations:

- long searches can block if not carefully queued;
- restart can lose in-memory state;
- harder to retry failed jobs.

### Production-grade worker architecture

Use separate deployment units:

- `telegram-bot` or `whatsapp-ingress`: receives messages/webhooks;
- `api`: Notion/task API and health endpoints;
- `worker`: runs procurement research jobs;
- `postgres`: durable task state, candidates, supplier memory, audit log;
- `redis`: queues, locks, rate limits, short-lived state;
- optional `browser-runner`: Playwright/Chrome job executor;
- optional `messaging-bridge`: WhatsApp provider adapter.

Recommended for real work because procurement searches are slow, stateful, and need retries.

## Messaging safety model

Default mode: draft only.

Allowed without extra approval:

- compose supplier questions;
- compose customer clarification messages;
- save draft in Notion;
- show message preview in Telegram;
- rank who to contact first.

Requires explicit approval in the same conversation:

- send WhatsApp/Telegram/SMS/email;
- ask a supplier for stock/price;
- ask a customer for clarification;
- reserve goods;
- confirm delivery;
- share personal/payment data;
- place an order.

Recommended approval record:

- approver user id;
- timestamp;
- recipient;
- exact message body;
- related request and line item;
- provider message id after send.

## Phased implementation

### Phase 1: Telegram control bot

- Reuse the Rudolf pattern: webhook, allowlist, voice/photo/text, confirmation buttons.
- No automatic outbound supplier messages.
- Bot can start jobs and report results.

### Phase 2: Notion task queue

- Add procurement task state:
  - `new`, `researching`, `needs_review`, `approved_to_contact`, `contacted`, `answered`, `blocked`, `done`.
- Store evidence and candidate snapshots.

### Phase 3: approved outbound messaging

- Start with WhatsApp bridge from `shermos-bot` if speed matters.
- Keep provider-neutral interfaces:
  - `send_message(provider, recipient, body, idempotency_key)`;
  - `receive_message(provider, sender, body, attachments, raw_event)`.
- Store every outbound and inbound message.

### Phase 4: official WhatsApp API migration

- Replace Baileys bridge with WhatsApp Cloud API/BSP adapter.
- Keep the same internal message model.
- Add templates and provider-specific delivery statuses.

### Phase 5: autonomous follow-up with limits

Only after enough safety data:

- rate-limited supplier follow-ups;
- no promises without approval;
- no orders/payments;
- automatic escalation when supplier answer conflicts with spec.

## Decision

Build the procurement assistant as a channel-agnostic agent with Telegram as the operator interface and WhatsApp as an approved communication channel.

Do not let the first version autonomously message suppliers. Make it draft, ask approval, send, record, and reconcile replies.

## External references to re-check before implementation

- Meta WhatsApp Business Platform / Cloud API documentation.
- Meta WhatsApp message template documentation.
- Telegram Bot API `setWebhook` documentation.
- Current Baileys documentation for the exact pinned major version, if using the Shermos bridge as a pilot.
