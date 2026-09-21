#!/usr/bin/env bash
# Shared LifeOS skill helpers. Source with:
#   source "$(dirname "$0")/../_lib/call.sh"
#
# Env expected: BRAIN_URL, AGENTSTOP, LOG_DIR. BRAIN_ANON_KEY is accepted but
# unused -- kept so existing plists keep working unchanged.
#
# BRAIN_URL points at Open Brain's REST API on :8000 (2026-09-10). This reverses
# the Phase D cutover to OB1: the 11-container Supabase stack was a large
# operational surface for a ~50-row corpus, and OB1 held nothing Open Brain did
# not already have -- of its 46 rows, 38 were byte-identical and the 8 unique
# ones were all cutover smoke-test probes.
#
# API differences handled below: OB1 was POST /functions/v1/{list,search,capture}
# behind a Kong gateway with an apikey header; Open Brain is GET /memories,
# POST /memories/search, POST /memories, unauthenticated on the LAN.

: "${BRAIN_URL:=http://127.0.0.1:8000}"
: "${AGENTSTOP:=http://127.0.0.1:11500}"
: "${LOG_DIR:=$HOME/.agentstop/logs}"
mkdir -p "$LOG_DIR"

# Open Brain needs no auth on the LAN. Kept as a no-op so every call site and
# every plist keeps working without edits; delete once nothing references it.
_brain_require_key() { return 0; }

# lifeos_log <run-log-basename> <json-object-string>
lifeos_log() {
  local base="$1" payload="$2"
  python3 - "$payload" >> "$LOG_DIR/$base.jsonl" <<'PY'
import json, sys, time
try:
    obj = json.loads(sys.argv[1])
except Exception:
    obj = {"raw": sys.argv[1]}
obj["ts"] = time.time()
print(json.dumps(obj))
PY
}

# lifeos_kv_json <k1> <v1> <k2> <v2> ...
# Emits JSON object from alternating key/value args. Values coerced to int if numeric.
lifeos_kv_json() {
  python3 - "$@" <<'PY'
import json, sys
args = sys.argv[1:]
out = {}
for i in range(0, len(args), 2):
    k = args[i]
    v = args[i+1] if i+1 < len(args) else ""
    try:
        v_cast = int(v)
    except ValueError:
        v_cast = v
    out[k] = v_cast
sys.stdout.write(json.dumps(out))
PY
}

# lifeos_call <model> <sensitivity> <prompt> <memories-json>
# Prints cleaned response to stdout. Returns non-zero on failure.
lifeos_call() {
  local model="$1" sensitivity="$2" prompt="$3" mem="$4"
  local body resp raw
  body=$(python3 - "$model" "$prompt" "$mem" <<'PY'
import sys, json
model, prompt, mem = sys.argv[1], sys.argv[2], sys.argv[3]
json.dump({
    "model": model,
    "stream": False,
    "messages": [{"role": "user", "content": f"{prompt}\n{mem}"}],
    "options": {"temperature": 0.3, "num_ctx": 16384},
}, sys.stdout)
PY
)
  # Ollama's /v1/chat/completions can return empty content on cold-load.
  # Retry once after a warm attempt if first call comes back empty.
  local attempt raw=""
  for attempt in 1 2; do
    resp=$(curl -s -m 600 -X POST "$AGENTSTOP/v1/chat/completions" \
      -H "Content-Type: application/json" \
      -H "X-LifeOS-Sensitivity: $sensitivity" \
      -d "$body")
    raw=$(printf '%s' "$resp" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'error' in d:
    print('ERROR:', d['error'], file=sys.stderr); sys.exit(2)
sys.stdout.write(d.get('choices',[{}])[0].get('message',{}).get('content','') or '')
")
    [ -n "$raw" ] && break
    # Empty content — likely cold-load race. Warm the model, then retry once.
    echo "lifeos_call: empty response attempt $attempt, warming $model" >&2
    # Heredoc, not `python3 -c` -- same trap documented on lifeos_fetch_search
    # below: bash brace-expands an unescaped {a,b} even inside the nested quotes
    # of "$(python3 -c "...")", splitting the JSON literal into separate words.
    # As `-c` this emitted `json.dump('keep_alive':'1h', sys.stdout)` and curl
    # then failed with "option : blank argument where content is expected", so
    # the warm-up never ran and the retry it exists to enable was wasted. It
    # fires only on the cold-load path, which is why it survived unnoticed --
    # and why it surfaced when keep_alive dropped from 24h to 1h (2026-09-20)
    # and cold loads became routine.
    local warm_body
    warm_body=$(python3 - "$model" <<'PY'
import sys, json
json.dump({"model": sys.argv[1], "prompt": "ok",
           "stream": False, "keep_alive": "1h"}, sys.stdout)
PY
)
    curl -s -m 300 -X POST "$AGENTSTOP/api/generate" \
      -H "Content-Type: application/json" \
      -H "X-LifeOS-Sensitivity: $sensitivity" \
      -d "$warm_body" > /dev/null
  done
  # Strip </think> preamble.
  python3 - "$raw" <<'PY'
import sys
t = sys.argv[1]; tag = "</think>"
i = t.rfind(tag)
print((t[i+len(tag):] if i != -1 else t).strip())
PY
}

# lifeos_fetch_recent <hours> [source-exclude]
# Prints JSON array of memories with created_at >= now-hours. Excludes given source.
# Open Brain's GET /memories has no server-side time filter, so we paginate
# newest-first and stop once a page's oldest row is past the cutoff.
#
# No field flattening needed: Open Brain returns source/tags/importance at the
# top level of each row already. OB1 buried them in a `metadata` JSONB and this
# helper used to lift them back out.
lifeos_fetch_recent() {
  local hours="$1" exclude="${2:-}"
  python3 - "$BRAIN_URL" "$hours" "$exclude" <<'PY'
import sys, json, urllib.request
from datetime import datetime, timezone, timedelta
base, hours, exclude = sys.argv[1], int(sys.argv[2]), sys.argv[3]
cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
out = []
for page in range(10):
    try:
        with urllib.request.urlopen(
                f"{base}/memories?limit=100&offset={page*100}", timeout=10) as r:
            batch = json.loads(r.read())
    except Exception:
        break
    if not batch:
        break
    stop = False
    for m in batch:
        ts = m.get("created_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < cutoff:
            stop = True
            continue
        if exclude and m.get("source") == exclude:
            continue
        out.append(m)
    if stop or len(batch) < 100:
        break
json.dump(out, sys.stdout, ensure_ascii=False)
PY
}

# lifeos_fetch_search <query> <limit>
lifeos_fetch_search() {
  local query="$1" limit="${2:-20}"
  # Open Brain's SearchRequest takes {query, limit}; OB1 took
  # {query, match_count, match_threshold}. Threshold is server-side here.
  # Heredoc, not `python3 -c`: bash brace-expands an unescaped {a,b} even inside
  # the nested quotes of "$(python3 -c "...")", splitting the JSON literal into
  # two words. That is a pre-existing bug -- the OB1 version had three keys and
  # expanded the same way -- so this helper has been emitting malformed JSON.
  local body
  body=$(python3 - "$query" "$limit" <<'PY'
import sys, json
json.dump({"query": sys.argv[1], "limit": int(sys.argv[2])}, sys.stdout)
PY
)
  curl -s -m 15 -X POST "$BRAIN_URL/memories/search" \
    -H "Content-Type: application/json" -d "$body"
}

# lifeos_write <source> <tags-json-array> <content-string> [metadata-json]
lifeos_write() {
  _brain_require_key || return 1
  local source="$1" tags="$2" content="$3"
  local meta="$4"
  [ -z "$meta" ] && meta='{}'
  local body
  body=$(python3 - "$source" "$tags" "$content" "$meta" <<'PY'
import sys, json
src, tags, content, meta = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], json.loads(sys.argv[4])
# Open Brain's MemoryCreate takes source/tags/importance as FIRST-CLASS fields;
# OB1 required them stuffed into a metadata blob. Anything else the caller
# passed still rides along in metadata.
json.dump({"content": content, "source": src, "tags": tags,
           "importance": 0.6, "metadata": meta}, sys.stdout)
PY
)
  curl -s -m 30 -X POST "$BRAIN_URL/memories" \
    -H "Content-Type: application/json" -d "$body"
}

# ---------------------------------------------------------------------------
# Harness dispatch
#
# lifeos_call() does one-shot inference: one prompt, one answer, no tools. For
# work that needs a tool loop there are two agent harnesses, both installed on
# THIS host and both pointed at the Studio's Deflector -- so the harness process
# is local, the GPU work lands on the Studio, and the privacy gate sits between.
#
#   OpenJarvis  local models only (lfm2.5 / qwen3.5), orchestrator loop
#   Hermes      cloud-capable: Ollama or Claude, richer toolsets and sessions
#
# WHY THE CLASSIFIER IS ALSO THE PRIVACY CONTROL. Neither harness can send
# X-LifeOS-Sensitivity, so work dispatched through one loses the declared-
# sensitivity refusal in the Deflector's lifeos_gate() -- the content prefilter
# (RFC1918, credential shapes) still fires, but the declared half does not.
# Routing `private` to OpenJarvis, whose config pins it to local models, means
# private work never reaches the cloud-capable harness at all. TELOS's own
# contract requires exactly this of any skill that injects it.
#
# Absolute paths on purpose: ~/.local/bin is not on the login PATH, let alone
# launchd's, and these run from LaunchAgents.
: "${JARVIS_BIN:=$HOME/.local/bin/jarvis}"
: "${HERMES_BIN:=$HOME/.local/bin/hermes}"

# lifeos_pick_harness <sensitivity> <prompt> → prints "jarvis" or "hermes"
#
# Sensitivity is a lookup, not a judgement -- skill frontmatter already declares
# it. Only effort needs a model, and tier A (lfm2.5) is what SYSTEM.md already
# designates for "routing gates, yes/no classification".
lifeos_pick_harness() {
  local sensitivity="$1" prompt="$2"
  # Anything private stays on the local-only harness. No model consulted: this
  # is policy, and a classifier that can be talked out of it is not a control.
  case "$sensitivity" in
    private) echo "jarvis"; return 0 ;;
  esac
  local verdict
  verdict=$(lifeos_call "lfm2.5:latest" "$sensitivity" \
    "Classify the effort this task needs. Answer with ONE word, nothing else.
HARD  - needs deep reasoning, long synthesis, or judgement a small local model would botch
LIGHT - routine, mechanical, extraction, formatting, or a short factual answer

Task:
$prompt" "" 2>/dev/null | tr -d "[:space:]" | tr "[:lower:]" "[:upper:]")
  case "$verdict" in
    *HARD*) echo "hermes" ;;
    *)      echo "jarvis" ;;   # unparseable verdict downgrades -- see TELOS
  esac                         # "when in doubt → downgrade one tier"
}

# lifeos_dispatch <sensitivity> <prompt> [harness]
#
# Runs the prompt on a harness and prints its answer. Falls back rather than
# failing: harness error → OpenJarvis → plain lifeos_call, so a skill always
# produces something.
lifeos_dispatch() {
  local sensitivity="$1" prompt="$2" harness="${3:-}"
  [ -z "$harness" ] && harness=$(lifeos_pick_harness "$sensitivity" "$prompt")

  local out=""
  if [ "$harness" = "hermes" ] && [ -x "$HERMES_BIN" ]; then
    out=$("$HERMES_BIN" chat -q "$prompt" 2>/dev/null) || out=""
  fi
  if [ -z "$out" ] && [ -x "$JARVIS_BIN" ]; then
    [ "$harness" = "hermes" ] && echo "lifeos_dispatch: hermes failed, retrying on jarvis" >&2
    harness="jarvis"
    out=$("$JARVIS_BIN" ask "$prompt" --no-stream 2>/dev/null | grep -v "^WARNING" ) || out=""
  fi
  if [ -z "$out" ]; then
    echo "lifeos_dispatch: both harnesses failed, falling back to one-shot inference" >&2
    harness="direct"
    out=$(lifeos_call "$(lifeos_pick_tier_c "$prompt")" "$sensitivity" "$prompt" "") || return 1
  fi
  lifeos_log dispatch "$(lifeos_kv_json skill "${SKILL_NAME:-unknown}" \
    harness "$harness" sensitivity "$sensitivity" chars "${#out}")" 2>/dev/null || true
  printf '%s' "$out"
}

# lifeos_read_telos → prints TELOS.md to stdout; NON-ZERO if there is no TELOS.
#
# The old version was `cat "$HOME/.pi/agent/TELOS.md" 2>/dev/null || echo ""`.
# Two faults, and together they hid a real outage for seven weeks:
#
#   1. One hardcoded path. TELOS lived at ~/.pi/agent/TELOS.md on the Studio and
#      at ~/.config/USER/TELOS/TELOS.md on the mini -- and the mini is where the
#      scheduled jobs actually run.
#   2. It failed SILENTLY. Skills received "" and carried on, so a TELOS-grounded
#      review ran ungrounded and nothing in the logs said so. The only reason this
#      surfaced at all is that weekly-review-personal is articulate enough to
#      write "Cannot score. TELOS not provided in input" into its own output
#      (2026-08-02), where it sat unread.
#
# Now: search candidates, and refuse loudly if none is found. Callers use
# `TELOS=$(lifeos_read_telos)` under `set -e`, so a non-zero return aborts the
# skill -- which is correct. A TELOS-grounded skill with no TELOS has nothing to
# ground against, and an ungrounded answer that looks grounded is worse than no
# answer at all.
#
# $LIFEOS_TELOS overrides for testing or a non-standard layout.
lifeos_read_telos() {
  local c
  for c in "${LIFEOS_TELOS:-}" \
           "$HOME/.pi/agent/TELOS.md" \
           "$HOME/.config/USER/TELOS/TELOS.md"; do
    [ -n "$c" ] && [ -s "$c" ] && { cat "$c"; return 0; }
  done
  echo "call.sh: no TELOS found (tried \$LIFEOS_TELOS, ~/.pi/agent/TELOS.md, ~/.config/USER/TELOS/TELOS.md)" >&2
  lifeos_log telos-missing "$(lifeos_kv_json skill "${SKILL_NAME:-unknown}" decision refused-no-telos host "$(hostname -s)")" 2>/dev/null || true
  return 1
}

# lifeos_free_gb → free RAM in GB (integer). Uses vm_stat, page-size aware.
#
# Kept as a public helper (skills may call it), but NOTE it no longer gates any
# model choice: lifeos_pick_tier_c used to select a 67GB model on `free_gb >=
# 70` and that branch was removed 2026-09-21 -- see the rationale there. Free
# RAM is a poor gate for a load decision on a box with two ollama servers that
# cannot see each other's residency; use scripts/ollama-mem.sh for the combined
# figure instead.
lifeos_free_gb() {
  local vm; vm=$(vm_stat 2>/dev/null)
  python3 - "$vm" <<'PY'
import re, sys
text = sys.argv[1]
page = 16384
free = 0
speculative = 0
for line in text.splitlines():
    m = re.match(r'Pages (free|speculative):\s+(\d+)', line)
    if m:
        val = int(m.group(2))
        if m.group(1) == 'free': free = val
        else: speculative = val
    m2 = re.search(r'page size of (\d+) bytes', line)
    if m2: page = int(m2.group(1))
print((free + speculative) * page // (1024**3))
PY
}

# lifeos_pick_tier_c <content-string> [preference]
# Preferences: quality (default) | speed | long-ctx
# Selects best on-disk tier C model given estimated token count + free RAM.
# Estimation: ~4 chars per token.
#
# Rules:
#   tokens <  8k, pref=speed         → qwen3.6:35b-a3b (fast MoE)
#   tokens < 64k                     → pi-qwen3.6-128k (dense, safe)
#   tokens >= 64k or pref=long-ctx   → pi-qwen3.6-128k (only option with full 128k ctx headroom)
#
# NO 66GB+ MODEL IS SELECTABLE HERE, deliberately. Until 2026-09-21 the
# `pref=quality` branch chose llama4:latest (67.4GB) whenever free RAM was
# >= 70GB. Two things were wrong with that. llama4 is demoted -- config.yaml's
# main_models keeps it routable but hides it from the dropdown, because its
# vision measured 0/3 and laguna beat it on the long-context retrieval it
# existed for (287s vs 383s at equal RAM) -- so this picked automatically a
# model the operator had decided against choosing manually. And it is a
# 67.4GB load triggered by a FREE-RAM test, which inverts the safety it looks
# like: the more headroom the box had, the more of it this would consume.
#
# That branch also got progressively more dangerous. It fires on `free_gb >=
# 70`, and the 2026-09-20 daemon retune (keep_alive 24h -> 1h,
# MAX_LOADED_MODELS 2 -> 1 on main) made that condition go from rare to
# common -- so a fix for memory exhaustion would have silently increased how
# often a 67GB model got loaded without anyone asking for one.
#
# pi-qwen3.6-128k (23.9GB) covers this case at a third of the footprint. Any
# genuinely large model is an explicit request now, never a fallback's guess.
#
# The cost of that simplicity -- selection no longer adapts to load at all -- and
# what an adaptive version would have to read instead (combined residency across
# BOTH ollama servers, not free RAM on one host) is written up in
# docs/stack/deferred-work.md. Read it before making this RAM-aware again.
lifeos_pick_tier_c() {
  local content="$1" pref="${2:-quality}"
  python3 - "$content" "$pref" <<'PY'
import sys
content, pref = sys.argv[1], sys.argv[2]
tokens = max(len(content) // 4, 1)
QWEN_MOE = "qwen3.6:35b-a3b"
QWEN_LONG = "pi-qwen3.6-128k"

if pref == "speed" and tokens < 8_000:
    print(QWEN_MOE)
else:
    print(QWEN_LONG)
PY
}
