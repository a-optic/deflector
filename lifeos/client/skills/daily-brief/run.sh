#!/usr/bin/env bash
# daily-brief runner.
#
# 1. Fetch last 24h memories from the brain.
# 2. Pick tier B (default) or tier D (escalate) based on count.
# 3. Call AgentStop with the picked model + X-LifeOS-Sensitivity: mixed.
#    AgentStop enforces prefilter; blocks routed to tier C fallback automatically.
# 4. Strip </think>...</think> preamble from response (safe passthrough if absent).
# 5. Write result back to the brain tagged [brief, daily].
#
# Env: BRAIN_URL, BRAIN_ANON_KEY, AGENTSTOP (default http://127.0.0.1:11500),
#      LOG_DIR (default ~/.agentstop/logs) -- see ../_lib/call.sh for defaults.

set -eo pipefail

SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

DEFAULT_MODEL="glm-4.7-flash"
ESCALATE_MODEL="lifeos-cloud-reason"
THRESHOLD=50
SENSITIVITY="mixed"

TS=$(date +%Y%m%d-%H%M%S)
RUN_LOG="$LOG_DIR/daily-brief.jsonl"

log() {
  python3 -c "import json,sys; print(json.dumps({'ts': __import__('time').time(), **json.loads(sys.argv[1])}))" "$1" >> "$RUN_LOG"
}

# 1. Fetch
MEMORIES_JSON=$("$SKILL_DIR/fetch.sh")
COUNT=$(printf '%s' "$MEMORIES_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -eq 0 ]; then
  log "{\"skill\":\"daily-brief\",\"decision\":\"skip-empty\",\"count\":0}"
  echo "daily-brief: no memories in last 24h, skipping" >&2
  exit 0
fi

# 2. Tier select
MODEL=$(printf '%s' "$MEMORIES_JSON" \
  | "$SKILL_DIR/escalation-check.sh" "$DEFAULT_MODEL" "$ESCALATE_MODEL" "$THRESHOLD")

log "{\"skill\":\"daily-brief\",\"decision\":\"selected-model\",\"model\":\"$MODEL\",\"count\":$COUNT}"

# 3. Build prompt + call AgentStop
PROMPT=$(python3 - "$SKILL_DIR/skill.md" <<'PY'
import sys, re, pathlib
raw = pathlib.Path(sys.argv[1]).read_text()
# Extract the prompt template block after "## Prompt template" heading.
m = re.search(r"## Prompt template.*?```(.*?)```", raw, flags=re.DOTALL)
sys.stdout.write(m.group(1).strip() if m else "")
PY
)

BODY=$(python3 - "$MODEL" "$PROMPT" "$MEMORIES_JSON" <<'PY'
import sys, json
model, prompt, memories = sys.argv[1], sys.argv[2], sys.argv[3]
body = {
    "model": model,
    "stream": False,
    "messages": [
        {"role": "user", "content": f"{prompt}\n{memories}"}
    ],
    "options": {"temperature": 0.3, "num_ctx": 8192},
}
json.dump(body, sys.stdout)
PY
)

# Retry once with warm on empty content (Ollama cold-load race on /v1/chat).
RAW_CONTENT=""
for attempt in 1 2; do
  RESPONSE=$(curl -s -m 300 -X POST "$AGENTSTOP/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "X-LifeOS-Sensitivity: $SENSITIVITY" \
    -d "$BODY")
  RAW_CONTENT=$(printf '%s' "$RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'error' in d:
    print('ERROR:', d['error'], file=sys.stderr)
    sys.exit(2)
msg = d.get('choices',[{}])[0].get('message',{})
sys.stdout.write(msg.get('content','') or '')
")
  [ -n "$RAW_CONTENT" ] && break
  echo "daily-brief: empty response attempt $attempt, warming $MODEL" >&2
  curl -s -m 300 -X POST "$AGENTSTOP/api/generate" \
    -H "Content-Type: application/json" \
    -H "X-LifeOS-Sensitivity: $SENSITIVITY" \
    -d "$(python3 -c "import json,sys; json.dump({'model':sys.argv[1],'prompt':'ok','stream':False,'keep_alive':'1h'}, sys.stdout)" "$MODEL")" > /dev/null
done

# 4. Strip CoT preamble (portable — no cross-host module dependency).
# Drops everything up to and including the last `</think>`. Passthrough if absent.
CLEAN=$(python3 - "$RAW_CONTENT" <<'PY'
import sys
text = sys.argv[1]
tag = "</think>"
idx = text.rfind(tag)
print((text[idx + len(tag):] if idx != -1 else text).strip())
PY
)

if [ -z "$CLEAN" ]; then
  log "{\"skill\":\"daily-brief\",\"decision\":\"empty-output\",\"model\":\"$MODEL\"}"
  echo "daily-brief: model returned empty output" >&2
  exit 3
fi

# 5. Write back to the brain
WRITE_RESULT=$(lifeos_write "daily-brief" '["brief","daily","auto"]' "$CLEAN" \
  "$(lifeos_kv_json model "$MODEL" input_count "$COUNT")")

log "{\"skill\":\"daily-brief\",\"decision\":\"wrote-brief\",\"model\":\"$MODEL\",\"count\":$COUNT}"

# Also print to stdout so launchd log captures it.
printf '\n=== daily-brief %s (model=%s count=%d) ===\n%s\n' "$TS" "$MODEL" "$COUNT" "$CLEAN"
