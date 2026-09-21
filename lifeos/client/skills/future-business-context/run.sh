#!/usr/bin/env bash
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

SENSITIVITY="private"
PREF="quality"

INPUT="${1:-}"
if [ -z "$INPUT" ] && [ ! -t 0 ]; then INPUT=$(cat); fi
if [ -z "$INPUT" ]; then
  echo "usage: future-business-context.run.sh <text>  OR  echo <text> | run.sh" >&2
  exit 2
fi

TELOS=$(lifeos_read_telos)

# Search prior business-tagged memories for context
PRIOR=$(lifeos_fetch_search "future business positioning consulting" 15 | python3 -c "
import sys, json
d = json.load(sys.stdin)
if isinstance(d, dict) and 'results' in d: d = d['results']
if not isinstance(d, list): d = []
json.dump(d, sys.stdout)
")

PROMPT="You are helping frame a future-business question against my TELOS.

TELOS current constraint: CLARITY. Every response must ask 'does this concentrate the bet or fragment it?' Fragmenting ideas → log, deprioritize. Concentrating ideas → develop.

Format (exact, no preamble):

## The question
- Restate in one sentence.

## Concentrate or fragment?
- Concentrate | Fragment | Ambiguous. One-sentence reason grounded in TELOS current bet.

## TELOS alignment
- Which values/horizons/non-negotiables this touches. Cite the exact line if possible.

## Considerations
- 3-5 bullets. Apply the lenses named under TELOS ## Decision heuristics, and check
  the result against ## Non-negotiables. Include a reversibility check.

## Recommendation
- ONE action. Concrete. Reversible bias unless stated otherwise.

Rules: terse operator, blunt friend when needed, no fluff, no preamble.

=== TELOS ===
$TELOS

=== Prior business-tagged memories ===
$PRIOR

=== The input ===
$INPUT"

MODEL=$(lifeos_pick_tier_c "$PROMPT" "$PREF")
OUT=$(lifeos_call "$MODEL" "$SENSITIVITY" "$PROMPT" "")
[ -z "$OUT" ] && { lifeos_log future-business-context '{"skill":"future-business-context","decision":"empty-output"}'; exit 3; }

# Write the input + framing as a paired memory
COMBINED="Q: $INPUT

$OUT"
lifeos_write "future-business-context" '["future-business","private","framing","auto"]' "$COMBINED" '{}' > /dev/null
lifeos_log future-business-context '{"skill":"future-business-context","decision":"wrote"}'

printf '\n=== future-business-context ===\n%s\n' "$OUT"
