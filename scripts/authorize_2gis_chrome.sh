#!/usr/bin/env bash
set -euo pipefail

if [[ ! -t 0 || ! -t 1 ]]; then
  echo 'Run this script in an interactive terminal on the server.' >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
codex_bin="${CODEX_2GIS_EXECUTABLE:-/home/moses/.local/bin/codex}"
if [[ ! -x "$codex_bin" ]]; then
  echo "Codex executable is missing: $codex_bin" >&2
  exit 2
fi

export PATH="$(dirname "$codex_bin"):$PATH"
cd "$repo_root"
exec "$codex_bin" \
  -m gpt-6-luna \
  -c model_reasoning_effort=high \
  -c approval_policy=on-request \
  -C "$repo_root" \
  -s read-only \
  'Use $2gis-chrome-research and the installed Chrome plugin. In my existing Chrome profile, open https://2gis.kz/almaty read-only. If Chrome requests permission for this site, present the request and wait for me to decide. Do not bypass a denial, solve a CAPTCHA, submit a form, or contact a supplier. Report whether the page became accessible and close only tabs you created.'
