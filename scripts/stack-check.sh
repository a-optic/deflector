#!/usr/bin/env bash
# Stack integration check — every consumer reaches inference and shared memory,
# and the privacy gate acts on all of them.
#
# The four consumers (LifeOS skills, Hermes, OpenJarvis, Pi) are PEERS, not a
# hierarchy: each calls the Deflector on :11500 independently. LifeOS is the
# orchestrator in the sense that it owns TELOS/Cortex/Skills and supplies intent
# and verification criteria -- not in the sense that its calls pass through the
# other two harnesses. lifeos_call() curls the Deflector directly.
AGENTSTOP=${AGENTSTOP:-http://127.0.0.1:11500}
BRAIN=${BRAIN:-http://127.0.0.1:8000}
FAILED=0
pass(){ printf "  \033[32mPASS\033[0m  %-32s %s\n" "$1" "$2"; }
fail(){ printf "  \033[31mFAIL\033[0m  %-32s %s\n" "$1" "$2"; FAILED=$((FAILED+1)); }

echo "── shared substrate ──"
curl -sf -m 8 "$BRAIN/health" >/dev/null 2>&1 && pass "Open Brain" "$BRAIN" || fail "Open Brain" "$BRAIN unreachable"
curl -sf -m 8 "$AGENTSTOP/api/tags" >/dev/null 2>&1 && pass "Deflector" "$AGENTSTOP" || fail "Deflector" "$AGENTSTOP unreachable"
N=$(curl -sf -m 8 "$BRAIN/stats" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin)["total"])' 2>/dev/null)
[ -n "$N" ] && pass "memory corpus" "$N memories" || fail "memory corpus" "unreadable"

echo "── each consumer reaches inference ──"
N0=$(date +%s%N)
R=$(curl -sf -m 120 "$AGENTSTOP/api/generate" -d "{\"model\":\"qwen3.6:35b-a3b\",\"prompt\":\"run $N0 - reply with exactly: OK\",\"stream\":false}" 2>/dev/null \
    | python3 -c 'import json,sys;print(json.load(sys.stdin).get("response","")[:14])' 2>/dev/null)
[ -n "$R" ] && pass "LifeOS (direct HTTP)" "$R" || fail "LifeOS (direct HTTP)" "no answer"

# `hermes ask` was removed; the subcommand is `chat -q` as of v0.18.2.
R=$(hermes chat -q "run $(date +%s%N) - reply with exactly: OK" 2>/dev/null | tail -1 | cut -c1-24)
[ -n "$R" ] && pass "Hermes harness" "$R" || fail "Hermes harness" "no answer"

R=$(jarvis ask "run $(date +%s%N) - reply with exactly: OK" 2>/dev/null | tail -1 | cut -c1-24)
[ -n "$R" ] && pass "OpenJarvis harness" "$R" || fail "OpenJarvis harness" "no answer"

echo "── privacy gate ──"
# A private key bound for cloud must not REACH the cloud. It does not 403: the
# Phase 1 secret fallback serves it locally instead, so the caller still gets an
# answer and the secret never leaves the box. Assert the ROUTE, not the status.
LOG=$(ls -t "$HOME/.agentstop/logs/routing-"*.jsonl 2>/dev/null | head -1)
BEFORE=$(wc -l < "$LOG" 2>/dev/null || echo 0)
# Unique per run: identical prompts trip the deflector's repeat-guard, which
# intercepts before the privacy gate and returns a CONFIRM instead of routing.
NONCE=$(date +%s%N)
curl -s -o /dev/null -m 60 "$AGENTSTOP/v1/chat/completions" -H 'Content-Type: application/json' \
  -d "{\"model\":\"nemotron-3-super:cloud\",\"messages\":[{\"role\":\"user\",\"content\":\"run $NONCE -----BEGIN OPENSSH PRIVATE KEY-----\"}],\"stream\":false}" 2>/dev/null
D=$(tail -n +$((BEFORE+1)) "$LOG" 2>/dev/null | python3 -c '
import json,sys
for l in sys.stdin:
    d=json.loads(l)
    if "secret" in str(d.get("detail","")): print(d.get("reason"),"->",d.get("routed")); break' 2>/dev/null)
[ -n "$D" ] && pass "secret diverted off cloud" "$D" || fail "secret diverted off cloud" "no diversion logged"

echo
[ $FAILED -eq 0 ] && echo "  all checks passed" || echo "  $FAILED check(s) failed"
exit $FAILED
