#!/usr/bin/env bash
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

MODEL="lifeos-cloud-reason"
SENSITIVITY="public"

MEM=$(lifeos_fetch_recent 168 "daily-brief" | python3 -c "
import sys, json
data = json.load(sys.stdin)
KEEP = {'research','learning','reading','paper','book','video','study','anki'}
kept = [m for m in data if set(m.get('tags') or []) & KEEP]
json.dump(kept, sys.stdout)
")
COUNT=$(printf '%s' "$MEM" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
  lifeos_log research-digest '{"skill":"research-digest","decision":"skip-empty"}'
  exit 0
fi

PROMPT="You are producing a weekly research digest. Public-safe synthesis for possible publication.

Format (exact, no preamble):

## What I learned this week
- 3-6 items. Each: 1-line takeaway + why it matters + [id citation].

## Connections
- Cross-item patterns worth noting. Cite IDs.

## Open questions
- What I don't yet understand or want to explore next.

## Publishable snippet
- ONE 2-3 sentence paragraph shareable as-is (Twitter/blog note).

Rules:
- Terse. No preamble. No hedging.
- Cite [abc123] first 6 chars.
- Do not invent facts.

=== Memories (last 7 days, research/learning only) ==="

OUT=$(lifeos_call "$MODEL" "$SENSITIVITY" "$PROMPT" "$MEM")
[ -z "$OUT" ] && { lifeos_log research-digest '{"skill":"research-digest","decision":"empty-output"}'; exit 3; }

lifeos_write "research-digest" '["digest","weekly","research","public","auto"]' "$OUT" \
  "$(lifeos_kv_json input_count "$COUNT" model "$MODEL")" > /dev/null

lifeos_log research-digest "$(lifeos_kv_json skill research-digest decision wrote count "$COUNT")"

printf '\n=== research-digest (count=%d) ===\n%s\n' "$COUNT" "$OUT"
