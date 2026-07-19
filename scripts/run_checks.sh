#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m tender_agent extract-file \
  tests/fixtures/product_jsonld.html --source-url https://shop.example/p139 >/dev/null
PYTHONPATH=src python3 -m tender_agent landed-cost \
  --tender-price 850 --quantity 3 --offer-price 800 --logistics 1000 >/dev/null
PYTHONPATH=src python3 -m tender_agent allocate-delivery \
  --total 5000 №8=1 №12=1 №15=1 >/dev/null
PYTHONPATH=src python3 -m tender_agent source-plan \
  https://kaspi.kz/shop/p/test --html tests/fixtures/product_jsonld.html \
  --required product_name price confirmed_stock >/dev/null
PYTHONPATH=src python3 -m tender_agent compare-scenarios \
  tests/fixtures/scenarios.json >/dev/null
PYTHONPATH=src python3 -m tender_agent trip-economics \
  tests/fixtures/trip.json >/dev/null
PYTHONPATH=src python3 -m tender_agent snapshot-validate \
  tests/fixtures/karaganda_reconciliation.json >/dev/null
PYTHONPATH=src python3 -m tender_agent reconcile \
  tests/fixtures/karaganda_reconciliation.json >/dev/null
PYTHONPATH=src python3 -m tender_agent report \
  tests/fixtures/karaganda_reconciliation.json --format md >/dev/null
PYTHONPATH=src python3 -m tender_agent notion-plan \
  tests/fixtures/notion_current.json tests/fixtures/notion_desired.json >/dev/null
PYTHONPATH=src python3 -m tender_agent coverage \
  tests/fixtures/coverage.json --minimum-leads 2 --minimum-actionable 2 >/dev/null
STATE_DB=$(mktemp "${TMPDIR:-/tmp}/tender-agent-state.XXXXXX.sqlite")
trap 'rm -f "$STATE_DB"' EXIT HUP INT TERM
PYTHONPATH=src python3 -m tender_agent store-init "$STATE_DB" >/dev/null
PYTHONPATH=src python3 -m tender_agent job-status "$STATE_DB" >/dev/null
./scripts/validate_skill.sh

echo "All procurement core checks passed."
