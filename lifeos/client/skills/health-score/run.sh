#!/usr/bin/env bash
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

MODEL="glm-4.7-flash"
SENSITIVITY="personal"

MEM=$(lifeos_fetch_recent 168 "daily-brief" | python3 -c "
import sys, json
data = json.load(sys.stdin)
KEEP = {'health','sleep','cardio','weight','hrv','workout','run','lift','yoga','apple-health'}
kept = [m for m in data if set(m.get('tags') or []) & KEEP]
json.dump(kept, sys.stdout)
")
COUNT=$(printf '%s' "$MEM" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

# Also load prior score if any (last 14 days) for 2-week trend
PRIOR=$(lifeos_fetch_recent 336 "" | python3 -c "
import sys, json
data = json.load(sys.stdin)
kept = [m for m in data if m.get('source') == 'health-score']
json.dump(kept[:2], sys.stdout)  # up to 2 most recent priors
")

TELOS=$(lifeos_read_telos)

if [ "$COUNT" -eq 0 ]; then
  # Still produce a zero-signal placeholder so the trend has continuity
  OUT="## Composite score
0/10 — no health-tagged memories logged this week.

## Sleep / Cardio / Weight
- Sleep: no data
- Cardio: no data
- Weight: no data

## TELOS non-negotiable status
AT-RISK — the TELOS body floor cannot be evaluated; nothing logged means either a tracking gap or an actual gap.

## Recommendation
Log this week's actuals manually, or ship apple-health importer."
  lifeos_write "health-score" '["health","weekly-score","personal","auto","zero-input"]' "$OUT" '{}' > /dev/null
  lifeos_log health-score '{"skill":"health-score","decision":"zero-input-placeholder"}'
  printf '\n=== health-score (count=0) ===\n%s\n' "$OUT"
  exit 0
fi

PROMPT="You are computing a weekly health composite score against TELOS non-negotiables.

Read the body-floor bright lines from the ## Non-negotiables heading of the TELOS
document below. Use ONLY the thresholds written there -- do not assume defaults, and
do not invent a metric TELOS does not name.

Format (exact, no preamble):

## Composite score
- N/10 with one-sentence rationale.

## Sleep / Cardio / Weight
- Sleep: <hours avg>, vs the TELOS sleep floor → HELD | AT-RISK | VIOLATED
- Cardio: <sessions this week>, vs the TELOS cardio floor → HELD | AT-RISK | VIOLATED
- Weight/body: <trend>, [id]

## TELOS non-negotiable status
- ONE line: HELD | AT-RISK | VIOLATED for body floor overall.

## 2-week trend
- Compare against prior scores if provided. Flag 2-consecutive-week drop as INCIDENT.

## Recommendation
- ONE concrete action for next week.

Rules: terse, cite [abc123] first 6 chars, no invention. If a metric missing, say 'no data' — do not fabricate.

=== TELOS ===
$TELOS

=== Prior health-score memories (up to 2) ===
$PRIOR

=== This week's health memories ==="

OUT=$(lifeos_call "$MODEL" "$SENSITIVITY" "$PROMPT" "$MEM")
[ -z "$OUT" ] && { lifeos_log health-score '{"skill":"health-score","decision":"empty-output"}'; exit 3; }

lifeos_write "health-score" '["health","weekly-score","personal","auto"]' "$OUT" \
  "$(lifeos_kv_json input_count "$COUNT" model "$MODEL")" > /dev/null

lifeos_log health-score "$(lifeos_kv_json skill health-score decision wrote count "$COUNT")"

printf '\n=== health-score (count=%d) ===\n%s\n' "$COUNT" "$OUT"
