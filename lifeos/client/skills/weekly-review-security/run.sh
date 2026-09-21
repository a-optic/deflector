#!/usr/bin/env bash
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

SENSITIVITY="private"
PREF="quality"

MEM=$(lifeos_fetch_recent 168 "daily-brief" | python3 -c "
import sys, json
data = json.load(sys.stdin)
KEEP = {'security','homelab','private','ops','incident','pentest','offensive','vuln','audit'}
kept = [m for m in data if set(m.get('tags') or []) & KEEP]
json.dump(kept, sys.stdout)
")
COUNT=$(printf '%s' "$MEM" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
  lifeos_log weekly-review-security '{"skill":"weekly-review-security","decision":"skip-empty"}'
  exit 0
fi

PROMPT="You are producing a weekly SECURITY + HOMELAB review. Local-only, never leaves LAN.

Format (exact, no preamble):

## Posture summary
- 2-3 sentences. Trending better/worse/flat.

## New surface / config changes
- Every config change, new service, new peer. Cite IDs.

## Incidents / anomalies
- Anything worth investigating. Cite IDs.

## Drift indicators
- Unaddressed items rolling forward >1 week. Cite IDs.

## Recommended actions (ordered by risk reduction)
- Numbered. Each: action + rationale.

Rules:
- Terse operator voice. No preamble.
- Cite memory IDs like [abc123] (first 6 chars).
- Do not invent.

=== Memories (last 7 days, security/homelab/private only) ==="

MODEL=$(lifeos_pick_tier_c "$PROMPT$MEM" "$PREF")
OUT=$(lifeos_call "$MODEL" "$SENSITIVITY" "$PROMPT" "$MEM")
[ -z "$OUT" ] && { lifeos_log weekly-review-security '{"skill":"weekly-review-security","decision":"empty-output"}'; exit 3; }

lifeos_write "weekly-review-security" '["review","weekly","security","private","auto"]' "$OUT" \
  "$(lifeos_kv_json input_count "$COUNT" model "$MODEL")" > /dev/null

lifeos_log weekly-review-security "$(lifeos_kv_json skill weekly-review-security decision wrote count "$COUNT")"

printf '\n=== weekly-review-security (count=%d) ===\n%s\n' "$COUNT" "$OUT"
