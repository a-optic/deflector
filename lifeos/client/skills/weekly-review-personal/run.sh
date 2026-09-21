#!/usr/bin/env bash
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

MODEL="lifeos-cloud-long"
SENSITIVITY="personal"

# Fetch last 7 days, drop private-tagged
MEM=$(lifeos_fetch_recent 168 "daily-brief" | python3 -c "
import sys, json
data = json.load(sys.stdin)
BLOCK = {'private','homelab','future-business','security','client'}
filtered = [m for m in data if not (set(m.get('tags') or []) & BLOCK)]
json.dump(filtered, sys.stdout)
")
COUNT=$(printf '%s' "$MEM" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
  lifeos_log weekly-review-personal '{"skill":"weekly-review-personal","decision":"skip-empty"}'
  echo "no personal memories in last 7 days, skipping" >&2
  exit 0
fi

TELOS=$(lifeos_read_telos)

PROMPT="You are producing a weekly personal review. Ground it against my TELOS (below).

Format (exact, no preamble):

## Alignment score (this week)
- Score 1-10 per horizon area: 90-day bets, non-negotiables, failure modes. Cite memory IDs.

## Wins
- 3-5 concrete wins that moved a horizon forward. Cite IDs.

## Recurring frictions
- 3-5 patterns. Explain mechanism, not just symptom. Cite IDs.

## Non-negotiable check
- For EACH bright line in TELOS, state: HELD | AT-RISK | VIOLATED with evidence.

## Focus for next week
- ONE concrete recommendation with a compounding rationale (10-year lens).

Rules:
- Voice: terse operator, blunt friend when pattern worth naming, no fluff.
- Cite memory IDs like [abc123] using first 6 chars.
- Do not invent facts absent from log/TELOS.
- No apologies, no preamble, start at ## Alignment score.

=== TELOS ===
$TELOS

=== Memories (last 7 days, personal only) ==="

OUT=$(lifeos_call "$MODEL" "$SENSITIVITY" "$PROMPT" "$MEM")
if [ -z "$OUT" ]; then
  lifeos_log weekly-review-personal '{"skill":"weekly-review-personal","decision":"empty-output"}'
  exit 3
fi

lifeos_write "weekly-review-personal" '["review","weekly","personal","auto"]' "$OUT" \
  "$(lifeos_kv_json input_count "$COUNT" model "$MODEL")" > /dev/null

lifeos_log weekly-review-personal "$(lifeos_kv_json skill weekly-review-personal decision wrote count "$COUNT")"

printf '\n=== weekly-review-personal (count=%d) ===\n%s\n' "$COUNT" "$OUT"
