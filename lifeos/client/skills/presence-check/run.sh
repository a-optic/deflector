#!/usr/bin/env bash
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

MODEL="lfm2.5:latest"
SENSITIVITY="private"

MEM=$(lifeos_fetch_recent 24 "daily-brief")
COUNT=$(printf '%s' "$MEM" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
  lifeos_log presence-check '{"skill":"presence-check","decision":"skip-empty"}'
  exit 0
fi

# TELOS is the source of truth for the bright lines this skill scores against.
# They used to be hardcoded into the prompt below, which had two costs: the copy
# drifted silently whenever TELOS changed, and it put the operator's actual
# non-negotiables into a file that is published. Reading them at runtime fixes
# both. lifeos_read_telos returns non-zero when no TELOS is found and `set -e`
# is on, so this aborts rather than scoring against nothing -- see that helper's
# docstring for the five-week ungrounded-run this behaviour exists to prevent.
TELOS=$(lifeos_read_telos)

PROMPT="You are auditing today's memories for TELOS non-negotiable drift.

Check the bright lines written under the '## Non-negotiables' heading of the TELOS
document below, and the early-warning patterns under '## Failure modes'. Use ONLY
what is written there -- do not invent or assume a bright line that is not stated.
Where a failure mode describes a sustained pattern, treat more than two consecutive
days of evidence for it as an ALERT.

Output ONE of these forms:

If all bright lines HELD:
HELD.

If ANY at-risk or violated:
AT-RISK: <one sentence naming which bright line + evidence [id]>.

or

VIOLATED: <one sentence, cite [id]>.

Rules:
- Only ONE line output.
- No preamble. No explanation.
- Cite [abc123] first 6 chars if referring to specific memory.

=== TELOS ===
$TELOS

=== Today's memories ==="

OUT=$(lifeos_call "$MODEL" "$SENSITIVITY" "$PROMPT" "$MEM")
[ -z "$OUT" ] && { lifeos_log presence-check '{"skill":"presence-check","decision":"empty-output"}'; exit 3; }

# Only write to Open Brain if NOT held (avoid daily noise)
if [[ "$OUT" == HELD* ]]; then
  lifeos_log presence-check "$(lifeos_kv_json skill presence-check decision held count "$COUNT")"
  printf '\n=== presence-check ===\n%s\n' "$OUT"
  exit 0
fi

lifeos_write "presence-check" '["presence-check","daily","private","alert","auto"]' "$OUT" '{}' > /dev/null
lifeos_log presence-check "$(lifeos_kv_json skill presence-check decision alert count "$COUNT" output "$OUT")"

printf '\n=== presence-check ALERT ===\n%s\n' "$OUT"
