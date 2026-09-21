#!/usr/bin/env bash
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

SENSITIVITY="private"
PREF="quality"

MEM=$(lifeos_fetch_recent 168 "daily-brief" | python3 -c "
import sys, json
data = json.load(sys.stdin)
KEEP = {'homelab','ops','pfsense','ollama','synology','truenas','studio','network','install','config'}
kept = [m for m in data if set(m.get('tags') or []) & KEEP]
json.dump(kept, sys.stdout)
")
COUNT=$(printf '%s' "$MEM" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
  lifeos_log homelab-drift-log '{"skill":"homelab-drift-log","decision":"skip-empty"}'
  exit 0
fi

PROMPT="You are producing a homelab drift log. Local only. Focus on state changes and unresolved items.

Format (exact, no preamble):

## Config changes this week
- Bullet each change with [id].

## Unresolved issues
- Items still open. Cite [id].

## Deferred/backlog rolling forward
- Items that appeared in prior weeks still not resolved. Cite [id] if inferrable.

## Recommended next action
- ONE concrete step to reduce operational surface or resolve highest-risk drift.

Rules: terse operator, no preamble, cite [abc123] first 6 chars, no invention.

=== Memories (last 7 days, homelab/ops) ==="

MODEL=$(lifeos_pick_tier_c "$PROMPT$MEM" "$PREF")
OUT=$(lifeos_call "$MODEL" "$SENSITIVITY" "$PROMPT" "$MEM")
[ -z "$OUT" ] && { lifeos_log homelab-drift-log '{"skill":"homelab-drift-log","decision":"empty-output"}'; exit 3; }

lifeos_write "homelab-drift-log" '["drift-log","weekly","homelab","private","auto"]' "$OUT" \
  "$(lifeos_kv_json input_count "$COUNT" model "$MODEL")" > /dev/null

lifeos_log homelab-drift-log "$(lifeos_kv_json skill homelab-drift-log decision wrote count "$COUNT")"

printf '\n=== homelab-drift-log (count=%d) ===\n%s\n' "$COUNT" "$OUT"
