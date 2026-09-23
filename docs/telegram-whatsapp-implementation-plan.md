# Telegram and WhatsApp implementation plan

Date: 2026-07-08

## Goal

Build a persistent procurement agent that can work with requests through Telegram and, after explicit approval, communicate with suppliers/customers through WhatsApp.

The product is client-agnostic: NIS is a frequent customer type, but the same flow must support schools, colleges, clinics, offices, public-sector bodies, private companies, contractors, and other organizations.

## Scope boundary

The agent may:

- accept new procurement requests;
- read photos, PDFs, DOCX, XLSX and Notion paths;
- create procurement tasks;
- research supplier candidates;
- update Notion;
- draft supplier/customer questions;
- show message previews;
- send approved messages;
- reconcile replies into Notion.

The agent must not do automatically:

- place orders;
- reserve goods;
- promise delivery;
- send payment or personal data;
- approve substitutions;
- mark a line as fully solved without evidence.

## Recommended architecture

```text
Telegram operator
    |
    v
telegram-bot service
    |
    v
procurement-api
    |
    +--> Postgres: tasks, line items, candidates, messages, approvals, audit
    +--> Redis: queues, locks, retry state, rate limits
    |
    v
procurement-worker
    |
    +--> Notion
    +--> Browser/search tools
    +--> supplier source extractors
    |
    v
messaging-adapter
    |
    +--> WhatsApp Cloud API, production path
    +--> Baileys bridge, pilot/internal path
```

## Reuse from existing projects

### `/Users/mosesvasilenko/Rudolf_music_site`

Reuse concepts from `services/telegram-bot`:

- Telegram webhook server;
- allowlist/self-auth;
- text, voice and photo intake;
- confirmation buttons;
- Codex/agent execution as a child process or queued job;
- concise operator responses;
- bounded action surface.

Do not copy the site-editing assumptions. Procurement needs task queues, Notion updates, supplier candidates and message audit, not git diff approval for website files.

### `/Users/mosesvasilenko/shermos-bot`

Reuse concepts from `whatsapp-bridge` and WhatsApp docs:

- bridge service split from business logic;
- Redis-backed session/state;
- inbound HTTP forwarding;
- shared-secret authentication between bridge and API;
- durable spool/retry for inbound messages;
- systemd runbooks;
- dual-number routing if separating operator/customer channels is useful.

Use Baileys only as a pilot or internal adapter. Keep the internal interface provider-neutral so official WhatsApp Cloud API can replace it later.

## Core data model

Minimum durable tables or Notion-backed equivalents:

- `customers`
  - organization name, type, city, addresses, contacts, notes.
- `requests`
  - customer, source, title, city, delivery site, status, deadline.
- `line_items`
  - request, item number, name, technical spec, quantity, unit, budget price, hard gates, status.
- `supplier_candidates`
  - line item, supplier, URL, price, quantity, city, delivery, match status, evidence, checked_at.
- `messages`
  - provider, direction, recipient, body, status, related request/line item, provider message id.
- `approvals`
  - action type, exact message/action, approver id, timestamp, idempotency key.
- `audit_events`
  - who/what/when/source for every automated or approved action.

## Status model

Request status:

- `new`
- `researching`
- `needs_review`
- `waiting_supplier`
- `waiting_customer`
- `ready_to_buy`
- `blocked`
- `done`

Line item status:

- `not_started`
- `searching`
- `exact_candidate_found`
- `needs_confirmation`
- `approved_to_contact`
- `contacted`
- `answered`
- `blocked`
- `closed`

Candidate status:

- `exact`
- `likely`
- `needs_confirmation`
- `over_budget`
- `wrong_spec`
- `unavailable`
- `stale`

## Event flow

### New request from Telegram

1. Operator sends text/photo/file/voice or a Notion path.
2. Telegram bot authenticates sender.
3. Bot creates a procurement task.
4. API stores task and enqueues worker job.
5. Worker parses input, reads Notion if needed, creates/updates request and line items.
6. Worker searches, validates candidates, updates Notion.
7. Bot sends a compact summary with top candidates and blockers.

### Approved WhatsApp outreach

1. Worker drafts message to supplier/customer.
2. Bot shows exact preview:
   - recipient;
   - related line item;
   - message body;
   - risk note.
3. Operator presses approve or cancel.
4. API stores approval with idempotency key.
5. Messaging adapter sends the message.
6. Provider response is stored in `messages`.
7. Inbound reply is attached to the correct request/line item.
8. Worker updates candidate status and Notion evidence.

## Safety rules

- Default mode is draft-only.
- Every outgoing supplier/customer message needs explicit approval in the same conversation.
- Store the exact approved text before sending.
- Use idempotency keys for outbound sends.
- Rate-limit supplier outreach.
- Never send payment data, private credentials, 2FA codes or card data.
- Never order, reserve or confirm delivery without a separate explicit approval.
- If a supplier answer conflicts with the technical spec, mark the line as `needs_review`, not solved.

## Reliability requirements

- Telegram webhook must acknowledge quickly and process work asynchronously.
- Long procurement jobs must run in worker queues, not inside the webhook request.
- Use Redis locks to avoid two workers updating the same request at the same time.
- Store all source evidence before claiming a candidate is validated.
- Retry temporary web/search/Notion failures with bounded attempts.
- Keep dead-letter records for failed jobs.
- Add health endpoints for bot, API, worker, queue, Notion connectivity and messaging adapter.
- Log with request id, line item id and provider message id.

## Deployment on Ubuntu

Recommended systemd units:

- `tenders-telegram-bot.service`
- `tenders-api.service`
- `tenders-worker.service`
- `tenders-whatsapp-adapter.service`
- `tenders-browser-runner.service` if browser automation is separated
- `postgres` and `redis` through Docker Compose or managed services

Required secrets:

- Telegram bot token;
- Telegram webhook secret;
- Notion token;
- OpenAI/Codex/Gemini credentials depending on runner;
- WhatsApp provider credentials;
- bridge shared secret;
- Postgres password;
- Redis URL/password.

Do not commit secrets. Keep `.env` out of git.

## Phased implementation

### Phase 0: repository preparation

- Keep current skill and docs as source of truth.
- Add `.env.example`.
- Add service skeleton folders.
- Decide Python vs Node for the first bot service.

Recommended choice: Node/TypeScript for Telegram bot if reusing Rudolf patterns; Python for procurement worker if Notion/docs parsing helpers are faster there.

### Phase 1: Telegram MVP

- Telegram webhook.
- Operator allowlist.
- `/start`, `/status`, `/tasks`.
- Accept text, photo and document.
- Create task record.
- Return task id.
- No WhatsApp sending.

Success criterion: operator can send a request and see a created task with status.

### Phase 2: Worker and Notion integration

- Queue worker.
- Parse simple text/photo/file inputs.
- Read/update Notion request rows.
- Write candidate summaries back to Notion.
- Return summary to Telegram.

Success criterion: a procurement request can be researched and reflected in Notion without manual copy/paste.

### Phase 3: Message drafting and approval

- Generate supplier/customer message drafts.
- Show preview in Telegram.
- Add approve/cancel buttons.
- Store approval records.
- Still no automatic sending until approval is explicit.

Success criterion: every outgoing text is auditable before sending.

### Phase 4: WhatsApp pilot adapter

- Start with the Shermos-style bridge if speed matters.
- Implement provider-neutral interface:
  - `send_message(provider, recipient, body, idempotency_key)`;
  - `receive_message(provider, sender, body, attachments, raw_event)`.
- Store inbound/outbound messages.
- Attach replies to request/line item.

Success criterion: approved supplier message can be sent and reply can be reconciled.

### Phase 5: Official WhatsApp Cloud API

- Add official provider adapter.
- Add templates if business-initiated outreach requires them.
- Track delivery/read/error statuses.
- Keep Baileys only as fallback or internal pilot.

Success criterion: production WhatsApp communication no longer depends on a linked-device session.

### Phase 6: Controlled autonomy

Only after stable audit data:

- automatic follow-up reminders;
- supplier answer classification;
- suggested substitutions with hard-gate warnings;
- no ordering/payment/reservation without approval.

Success criterion: agent reduces routine communication while keeping human approval for commercial commitments.

## MVP decision

Start a new agent dialog for procurement requests now, using the current `tender-procurement-notion` skill and Notion workflow.

Do not wait for Telegram/WhatsApp automation to exist. The automation plan is for persistent operation; current agent-assisted request work can continue immediately.
