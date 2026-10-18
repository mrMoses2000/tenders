#!/bin/sh
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

if [ -z "${TEST_POSTGRES_DSN:-}" ]; then
  echo "TEST_POSTGRES_DSN is required; full checks never fall back to a configured application database." >&2
  exit 2
fi
if [ -z "${TEST_POSTGRES_EXPECTED_DATABASE:-}" ] || [ -z "${TEST_POSTGRES_EXPECTED_PORT:-}" ]; then
  echo "Set TEST_POSTGRES_EXPECTED_DATABASE and TEST_POSTGRES_EXPECTED_PORT to pin the disposable database." >&2
  exit 2
fi

FAILED=0
run_check() {
  label=$1
  shift
  printf '\n== %s ==\n' "$label"
  if "$@"; then
    printf 'PASS: %s\n' "$label"
  else
    status=$?
    printf 'FAIL (%s): %s\n' "$status" "$label" >&2
    FAILED=1
  fi
}

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
run_check "Python compileall" python -m compileall -q src tests scripts
run_check "Full pytest suite" python -m pytest -q
run_check "Ruff" ruff check src tests scripts setup.py
run_check "Mini App JavaScript syntax" node --check mini_app/app.js

TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/procurement-full-checks.XXXXXX") || exit 1
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM
run_check "Build wheel" python scripts/build_wheel_isolated.py "$TMP_DIR/wheel"
WHEEL=$(find "$TMP_DIR/wheel" -name '*.whl' -print -quit)
if [ -n "$WHEEL" ]; then
  run_check "Installed wheel resource smoke" python scripts/verify_wheel_install.py "$WHEEL"
else
  echo "FAIL: Build wheel produced no wheel artifact." >&2
  FAILED=1
fi
run_check "Git diff whitespace check" git diff --check

if [ "$FAILED" -ne 0 ]; then
  echo "One or more full checks failed." >&2
  exit 1
fi
echo "All full project checks passed."
