#!/usr/bin/env bash
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

SENSITIVITY="private"
PREF="quality"

LOG="$HOME/.agentstop/logs/lifeos-escalations.jsonl"
if [ ! -f "$LOG" ]; then
  lifeos_log escalation-audit '{"skill":"escalation-audit","decision":"no-log"}'
  exit 0
fi

# Filter to last 7 days
RECENT=$(python3 - "$LOG" <<'PY'
import sys, json, time
cutoff = time.time() - 7*86400
out = []
with open(sys.argv[1]) as f:
    for line in f:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("ts",0) >= cutoff:
            out.append(rec)
json.dump(out, sys.stdout)
PY
)
COUNT=$(printf '%s' "$RECENT" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
  lifeos_log escalation-audit '{"skill":"escalation-audit","decision":"skip-empty"}'
  exit 0
fi

PROMPT="You are auditing the AgentStop LifeOS escalation log for the last 7 days.

Each record: {ts, skill_model, decision, sensitivity, ...}. Decisions: escalated | refused-private | refused-prefilter.

Format (exact, no preamble):

## Volume
- Total records, split by decision.

## Refusal patterns
- Which prefilter categories (ip_private, cred_*, hostname_lab) fired most. Concerning?

## Skill escalation rates
- Per skill: escalation vs refusal counts. Any skill escalating too often (leak risk) or too rarely (missing signal)?

## Anomalies
- Any decision that looks wrong given sensitivity + hit categories?

## Recommended tuning
- Regex additions, sensitivity re-tagging, or skill config changes.

Rules: terse operator, no preamble, no invention.

=== Log records (last 7 days) ==="

MODEL=$(lifeos_pick_tier_c "$PROMPT$RECENT" "$PREF")
OUT=$(lifeos_call "$MODEL" "$SENSITIVITY" "$PROMPT" "$RECENT")
[ -z "$OUT" ] && { lifeos_log escalation-audit '{"skill":"escalation-audit","decision":"empty-output"}'; exit 3; }

lifeos_write "escalation-audit" '["audit","weekly","escalations","private","auto"]' "$OUT" \
  "$(lifeos_kv_json input_count "$COUNT" model "$MODEL")" > /dev/null

lifeos_log escalation-audit "$(lifeos_kv_json skill escalation-audit decision wrote count "$COUNT")"

printf '\n=== escalation-audit (count=%d) ===\n%s\n' "$COUNT" "$OUT"
