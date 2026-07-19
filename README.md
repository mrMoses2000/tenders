# Tender Procurement Agent

This repository stores the working instructions, Codex skill, references, and helper scripts for Kazakhstan tender/procurement work with Notion.

The product is not limited to Nazarbayev Intellectual Schools. NIS requests are a frequent and well-known customer pattern, but the workflow must support any organization: schools, colleges, universities, clinics, offices, public-sector bodies, private companies, contractors, and one-off commercial requests.

## Layout

- `skills/tender-procurement-notion/` - installable Codex skill.
- `skills/tender-procurement-notion/references/` - detailed workflow references loaded only when needed.
- `skills/tender-procurement-notion/scripts/` - small deterministic helpers.
- `src/tender_agent/` - dependency-free deterministic core and safe public HTML extractor.
- `tests/` - unit tests and local HTML fixtures.
- `docs/` - human-readable audit, pipeline, and roadmap.
- root legacy `*.md` files - original project instructions used as source material.

Key docs:

- `docs/tender-agent-master-implementation-plan.md` - active master plan; update it before changing the architecture, workflow, skill, Notion schema, or implementation.
- `docs/pipeline-v3-architecture.md` - architecture and failure boundaries of the durable procurement core.
- `docs/pipeline-v3-implementation-report.md` - implemented contracts, tests, commands and remaining adapter work.
- `docs/tender-agent-pipeline.md` - operational procurement pipeline.
- `docs/automation-roadmap.md` - next helper scripts and guardrails.
- `docs/github-repo-scout.md` - decision on external GitHub projects.
- `docs/product-and-messaging-architecture.md` - product boundary, WhatsApp/Telegram options, Ubuntu deployment model.
- `docs/telegram-whatsapp-implementation-plan.md` - phased implementation plan for the persistent Telegram/WhatsApp procurement agent.
- `docs/notion-maxim-structure.md` - actual Notion structure under `Максим` and required request/supplier relation workflow.
- `docs/offers-database-field-guide.md` - complete human guide to every `Предложения` field.
- `docs/notion-cleanup-1.6-report.md` - verified reset report for `Караганда_1` and supplier legacy.

## Deterministic Core

Run all local checks:

```bash
./scripts/run_checks.sh
```

Examples:

```bash
PYTHONPATH=src python3 -m tender_agent landed-cost \
  --tender-price 850 --quantity 3 --offer-price 800 --logistics 1000

PYTHONPATH=src python3 -m tender_agent allocate-delivery \
  --total 5000 №8=1 №12=1 №15=1

PYTHONPATH=src python3 -m tender_agent extract-file \
  tests/fixtures/product_jsonld.html --source-url https://shop.example/item

PYTHONPATH=src python3 -m tender_agent source-plan \
  https://kaspi.kz/shop/p/test --html tests/fixtures/product_jsonld.html \
  --required product_name price confirmed_stock

PYTHONPATH=src python3 -m tender_agent compare-scenarios tests/fixtures/scenarios.json
PYTHONPATH=src python3 -m tender_agent trip-economics tests/fixtures/trip.json

PYTHONPATH=src python3 -m tender_agent snapshot-validate \
  tests/fixtures/karaganda_reconciliation.json
PYTHONPATH=src python3 -m tender_agent reconcile \
  tests/fixtures/karaganda_reconciliation.json
PYTHONPATH=src python3 -m tender_agent report \
  tests/fixtures/karaganda_reconciliation.json --format md

PYTHONPATH=src python3 -m tender_agent store-init var/tender-agent.sqlite
PYTHONPATH=src python3 -m tender_agent job-status var/tender-agent.sqlite

PYTHONPATH=src python3 -m tender_agent notion-plan \
  tests/fixtures/notion_current.json tests/fixtures/notion_desired.json
PYTHONPATH=src python3 -m tender_agent coverage tests/fixtures/coverage.json \
  --minimum-leads 2 --minimum-actionable 2
```

`extract-url` accepts only public HTTP(S) HTML pages, limits redirects, response size and timeout, and does not bypass login or anti-bot protection. A public stock signal is never converted into supplier-confirmed stock automatically.

Pipeline v3 stores source assertions as append-only facts, calculates line state on independent procurement/delivery/location/acceptance/replacement axes, and emits deterministic snapshots. Notion is a projection: `notion-plan` is always read-only, and runtime application must use the verified adapter contract from `tender_agent.notion_sync`. Five URLs are no longer treated as five actionable offers; `coverage` checks maturity, freshness, technical proof, confirmed price, contactability, local coverage and a reserve option.

## Install In Codex

Install or refresh the skill:

```bash
./scripts/install_skill.sh
```

The skill is copied to:

```text
~/.codex/skills/tender-procurement-notion
```

That makes it visible to Codex desktop and Codex CLI after the next skill discovery/restart.

## Validation

Validate the skill metadata:

```bash
./scripts/validate_skill.sh
```

The official `quick_validate.py` can also be used when `PyYAML` is installed.

Run a helper script smoke test:

```bash
python3 skills/tender-procurement-notion/scripts/check_candidates.py examples/candidates.csv --price-limit 2000 --required-qty 9
```

## Operating Principle

The agent may research, compare, draft, and update Notion. It must not send messages, reserve goods, place orders, or pay without explicit user approval in the same conversation.
