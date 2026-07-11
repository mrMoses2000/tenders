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
./scripts/validate_skill.sh

echo "All deterministic core checks passed."
