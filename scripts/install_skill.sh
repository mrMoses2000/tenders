#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/skills/tender-procurement-notion"
DEST="${CODEX_HOME:-$HOME/.codex}/skills/tender-procurement-notion"

mkdir -p "$DEST"
cp -R "$SRC"/. "$DEST"/

echo "Installed tender-procurement-notion to $DEST"
