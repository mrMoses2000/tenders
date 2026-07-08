# Tender Procurement Agent

This repository stores the working instructions, Codex skill, references, and helper scripts for Kazakhstan tender/procurement work with Notion.

The product is not limited to Nazarbayev Intellectual Schools. NIS requests are a frequent and well-known customer pattern, but the workflow must support any organization: schools, colleges, universities, clinics, offices, public-sector bodies, private companies, contractors, and one-off commercial requests.

## Layout

- `skills/tender-procurement-notion/` - installable Codex skill.
- `skills/tender-procurement-notion/references/` - detailed workflow references loaded only when needed.
- `skills/tender-procurement-notion/scripts/` - small deterministic helpers.
- `docs/` - human-readable audit, pipeline, and roadmap.
- root legacy `*.md` files - original project instructions used as source material.

Key docs:

- `docs/tender-agent-pipeline.md` - operational procurement pipeline.
- `docs/automation-roadmap.md` - next helper scripts and guardrails.
- `docs/github-repo-scout.md` - decision on external GitHub projects.
- `docs/product-and-messaging-architecture.md` - product boundary, WhatsApp/Telegram options, Ubuntu deployment model.
- `docs/telegram-whatsapp-implementation-plan.md` - phased implementation plan for the persistent Telegram/WhatsApp procurement agent.
- `docs/notion-maxim-structure.md` - actual Notion structure under `Максим` and required request/supplier relation workflow.

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
python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/tender-procurement-notion
```

Run a helper script smoke test:

```bash
python3 skills/tender-procurement-notion/scripts/check_candidates.py examples/candidates.csv --price-limit 2000 --required-qty 9
```

## Operating Principle

The agent may research, compare, draft, and update Notion. It must not send messages, reserve goods, place orders, or pay without explicit user approval in the same conversation.
