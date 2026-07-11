# Deterministic procurement core: architecture

Status: local pilot
Version: 0.1.0
Date: 2026-07-11

## Boundary

The local core performs deterministic transformations and public-page extraction. It does not authenticate to sites, write to Notion, send messages, reserve goods, order transport, or make payments.

```text
Photo / Notion row / user fact
          │
          ▼
   Agent interpretation
          │ structured objects
          ▼
┌─────────────────────────────┐
│ Deterministic local core    │
│ normalize → dedupe →        │
│ validate → calculate → pack │
└─────────────────────────────┘
          │ transport-neutral payload
          ▼
 Future reviewed Notion adapter
```

Public pages follow a separate route:

```text
Public URL
  │ URL/IP/content/size/time guards
  ▼
HTML fetcher → JSON-LD / OpenGraph parser → compact product signal
                                              │
                                              └─ stock_confirmed is always false
```

## Components

| Module | Responsibility |
|---|---|
| `models.py` | Immutable request, supplier, offer, validation and cost structures. |
| `normalization.py` | Text, phone, domain, unit and package-aware price normalization. |
| `dedupe.py` | Stable supplier keys and duplicate grouping. |
| `validation.py` | Hard-parameter comparison and independent technical/readiness states. |
| `economics.py` | Decimal landed cost and exact delivery allocation to tiyn. |
| `notion_payload.py` | Stable observation key and transport-neutral property payload. No write. |
| `extractor.py` | Guarded public HTML fetch and structured metadata extraction. |
| `adapters.py` | Stable source classification and extractor/Chrome/Computer Use/contact escalation. |
| `logistics.py` | Like-for-like route summaries and full-cost local/intercity ranking. |
| `trips.py` | Explicit offline market hypotheses, break-even economics and route sheets. |
| `cli.py` | Local smoke and operator commands. |

## Persistence

Version 0.1 has no mutable local persistence. Inputs and outputs are JSON-compatible values; fixtures are committed under `tests/fixtures`.

Future persistence belongs in a service database only when background tasks exist. Notion remains the operational business record, while Postgres would hold execution state, snapshots, retries and audit events.

## Idempotency

The observation key is derived from:

```text
request id + normalized supplier key + source + exact URL + checked date
```

The same input produces the same key and payload. A new date or materially different source produces a new observation instead of overwriting history.

## Retries and failure modes

- public fetch timeout: maximum 30 seconds;
- public fetch retries: 0–2, default 1;
- retries only for transport failures, 429 and transient 5xx responses;
- redirects: maximum 5, and every target is revalidated;
- response size: default 2 MB, hard maximum 10 MB;
- accepted content: HTML/XHTML only;
- malformed JSON-LD: skipped, then OpenGraph fallback;
- missing price or stock: remains unknown;
- one failed source must be reported as a partial result by the future orchestrator.

## Security

- only `http` and `https` URLs;
- credentials in URLs are rejected;
- localhost, private, loopback, link-local and special-purpose IPs are rejected;
- DNS results are checked before fetch and after redirects;
- no login, cookie import, CAPTCHA solving or anti-bot bypass;
- no secrets are required or stored by the core;
- HTML is parsed as data and never executed.

The DNS check reduces SSRF risk but is not a complete production isolation boundary. A future hosted fetcher should also use network egress policy and an isolated worker.

## Logging and audit

Version 0.1 is a library/CLI and emits only its result or an explicit error. The future worker must add structured fields:

- `task_id`;
- `request_id`;
- `line_item_id`;
- `supplier_key`;
- `offer_observation_key`;
- source URL and checked time;
- retry count and final error class.

Logs must never contain passwords, payment data, 2FA codes or private credentials.

## Verification

Run:

```bash
./scripts/run_checks.sh
```

Tests cover unit/package normalization, supplier duplicates, hard technical mismatches, unknown states, forecast economics, delivery allocation, idempotent payloads, JSON-LD/OpenGraph extraction, and private URL rejection.
