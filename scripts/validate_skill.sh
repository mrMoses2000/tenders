#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SKILL_DIR="$ROOT/skills/tender-procurement-notion"
SKILL_MD="$SKILL_DIR/SKILL.md"
OPENAI_YAML="$SKILL_DIR/agents/openai.yaml"

test -f "$SKILL_MD"
test -f "$OPENAI_YAML"

first_line=$(sed -n '1p' "$SKILL_MD")
test "$first_line" = "---"

grep -q '^name: tender-procurement-notion$' "$SKILL_MD"
grep -q '^description: .\+' "$SKILL_MD"
grep -q '^interface:$' "$OPENAI_YAML"
grep -q '^  display_name: ".\+"$' "$OPENAI_YAML"
grep -q '^  short_description: ".\+"$' "$OPENAI_YAML"
grep -Fq 'default_prompt: "Use $tender-procurement-notion ' "$OPENAI_YAML"

echo "Skill metadata checks passed."
