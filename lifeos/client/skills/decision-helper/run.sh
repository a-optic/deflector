#!/usr/bin/env bash
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

SENSITIVITY="private"
PREF="quality"

INPUT="${1:-}"
if [ -z "$INPUT" ] && [ ! -t 0 ]; then INPUT=$(cat); fi
if [ -z "$INPUT" ]; then
  echo "usage: decision-helper.run.sh <text>  OR  echo <text> | run.sh" >&2
  exit 2
fi

TELOS=$(lifeos_read_telos)

PROMPT="You are helping me apply TELOS decision heuristics to a decision I've framed.

Heuristics: apply what is written under the ## Decision heuristics heading of the
TELOS document below, and break ties using the ranked list under ## Values. Use ONLY
what is written there -- do not supply heuristics of your own. Where a choice is
irreversible, prefer a pause over a default yes unless TELOS states otherwise.

Format (exact, no preamble):

## The decision
- Restate in one sentence.

## Heuristic checks
- One block per heuristic listed under TELOS '## Decision heuristics'. Name the
  heuristic, then give PASSES / CONFLICTS / NEUTRAL and a one-sentence reason.
  Use only the heuristics written there -- do not add checks of your own.

## Reversibility
- REVERSIBLE / IRREVERSIBLE / PARTIAL. If irreversible, name what's locked in.

## TELOS alignment
- Which values/horizons/non-negotiables touched. Quote the relevant TELOS line.

## Recommendation
- STRONG YES / YES / PAUSE / NO / PAUSE + CONSULT. One-sentence rationale.
- If PAUSE + CONSULT: name who TELOS implies should be consulted, and what
  specifically to discuss with them. If TELOS names no one, say so rather than
  guessing.

Rules: terse operator, blunt friend, no hedging, no preamble.

=== TELOS ===
$TELOS

=== The decision ===
$INPUT"

MODEL=$(lifeos_pick_tier_c "$PROMPT" "$PREF")
OUT=$(lifeos_call "$MODEL" "$SENSITIVITY" "$PROMPT" "")
[ -z "$OUT" ] && { lifeos_log decision-helper '{"skill":"decision-helper","decision":"empty-output"}'; exit 3; }

COMBINED="Q: $INPUT

$OUT"
lifeos_write "decision-helper" '["decision","private","framing","auto"]' "$COMBINED" '{}' > /dev/null
lifeos_log decision-helper '{"skill":"decision-helper","decision":"wrote"}'

printf '\n=== decision-helper ===\n%s\n' "$OUT"
