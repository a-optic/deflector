#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# End-to-end proof that encrypted capture works AND that the server cannot read
# what it just wrote. Run this ON THE CLIENT, from Terminal at the Mac's own
# screen -- reading the key needs the unlocked login keychain of a GUI session.
#
#   ./capture-selftest.sh [model]
#
# It sends one request carrying a unique marker, then proves four things in
# order: the blob exists, the server cannot decrypt it, you can, and the
# plaintext contains your marker. Anything less than all four is not a pass --
# a decrypt that works proves nothing on its own if the server could do it too.

set -euo pipefail

# Site-specific addresses live in a local, uncommitted conf -- never in here.
#   ~/.agentstop/capture-selftest.conf
#     SERVER_HOST=192.0.2.10        # Deflector's address
#     SERVER_SSH=user@192.0.2.10    # ssh login on that host
CONF="${DEFLECTOR_SELFTEST_CONF:-$HOME/.agentstop/capture-selftest.conf}"
# shellcheck source=/dev/null
[[ -r "$CONF" ]] && . "$CONF"

SERVER_HOST="${DEFLECTOR_SERVER:-${SERVER_HOST:-}}"
SERVER_PORT="${DEFLECTOR_PORT:-${SERVER_PORT:-11500}}"
SERVER_SSH="${DEFLECTOR_SSH:-${SERVER_SSH:-}}"
CERT="${DEFLECTOR_CAPTURE_CERT:-$HOME/.agentstop/keys/pi.crt}"

# Prefer this client's enrolled fingerprint over the legacy "1": the fingerprint
# follows the key, so the test keeps working after a DHCP lease change, which is
# exactly the failure the enrolment work removed.
CLIENT_JSON="${DEFLECTOR_CLIENT_JSON:-$HOME/.agentstop/client.json}"
CAPTURE_VALUE="1"
if [[ -r "$CLIENT_JSON" ]]; then
  FP=$(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('fingerprint',''))" \
        "$CLIENT_JSON" 2>/dev/null || true)
  [[ -n "$FP" ]] && CAPTURE_VALUE="$FP"
fi

if [[ -z "$SERVER_HOST" || -z "$SERVER_SSH" ]]; then
  cat >&2 <<USAGE
server address not configured.

Create $CONF with:

  SERVER_HOST=<deflector host or IP>
  SERVER_SSH=<user>@<deflector host or IP>

or set DEFLECTOR_SERVER and DEFLECTOR_SSH in the environment.
USAGE
  exit 2
fi
DECRYPT="${DEFLECTOR_DECRYPT:-$HOME/.agentstop/bin/decrypt-capture.sh}"
MODEL="${1:-lfm2.5:latest}"

MARK="SELFTEST-$(date +%s)-$RANDOM"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/dfl-selftest.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
fail() { echo; echo "RESULT: FAIL -- $*" >&2; exit 1; }

echo "Deflector capture self-test"
echo "  this Mac (client) : $(ipconfig getifaddr en0 2>/dev/null || echo '?')"
echo "  server            : $SERVER_SSH  (:$SERVER_PORT)"
echo "  marker            : $MARK"
echo "  capture id        : $CAPTURE_VALUE"
echo

# 1 -------------------------------------------------------------------------
echo "1/6  Sending a request with the capture header..."
START=$(date +%s)
CODE=$(curl -s --max-time 120 -o "$TMP/resp.json" -w '%{http_code}' \
  -X POST "http://$SERVER_HOST:$SERVER_PORT/v1/chat/completions" \
  -H 'Content-Type: application/json' -H "X-Deflector-Capture: $CAPTURE_VALUE" \
  -d "{\"model\":\"$MODEL\",\"stream\":false,\"max_tokens\":16,
       \"messages\":[{\"role\":\"user\",\"content\":\"Say OK. Marker $MARK\"}]}") \
  || fail "request failed -- is Deflector up on $SERVER_HOST:$SERVER_PORT?"
[[ "$CODE" == "200" ]] || fail "server returned HTTP $CODE"
echo "     HTTP 200"

# 2 -------------------------------------------------------------------------
# Newest blob written since we started. `ls -t` on purpose: `find -newermt` is
# not portable here (bfs shadows find on some setups and rejects it).
echo "2/6  Locating the blob on the server..."
BLOB=$(ssh -o BatchMode=yes "$SERVER_SSH" \
  "ls -t ~/.agentstop/logs/capture/*/*.cms 2>/dev/null | head -1") \
  || fail "cannot ssh to $SERVER_SSH"
[[ -n "$BLOB" ]] || fail "no capture blob on the server -- is your client IP in config.yaml recipients?"
BLOB_TS=$(ssh -o BatchMode=yes "$SERVER_SSH" "stat -f %m '$BLOB'")
[[ "$BLOB_TS" -ge "$START" ]] || fail "newest blob predates this request -- capture did not fire"
echo "     $BLOB"
ssh -o BatchMode=yes "$SERVER_SSH" "ls -l '$BLOB' | awk '{print \"     \"\$1, \$5\" bytes\"}'"

# 3 -------------------------------------------------------------------------
echo "3/6  Proving the SERVER cannot read it..."
# Anchored to a real PEM header line, with the closing dashes. A loose
# '-----BEGIN .*PRIVATE KEY' matches this very script (it contains the pattern),
# so installing these scripts under ~/.agentstop on the server made the check
# report a key that was never there.
NKEYS=$(ssh -o BatchMode=yes "$SERVER_SSH" \
  "grep -rl -- '^-----BEGIN [A-Z ]*PRIVATE KEY-----\$' ~/.agentstop 2>/dev/null | wc -l | tr -d ' '")
echo "     private keys anywhere under ~/.agentstop on the server: $NKEYS"
[[ "$NKEYS" == "0" ]] || fail "the server is holding private key material -- that breaks the model"

SRVOUT=$(ssh -o BatchMode=yes "$SERVER_SSH" \
  "/opt/homebrew/bin/openssl cms -decrypt -inform DER -in '$BLOB' 2>&1 | head -1" || true)
echo "     server attempt -> ${SRVOUT:-<no output>}"
case "$SRVOUT" in
  *"No recipient certificate or key"*|*"unable to load"*|*"no recipient"*|*error*|*Error*) ;;
  *) fail "the SERVER decrypted its own capture -- confidentiality is broken" ;;
esac

# -inform DER is required. Without it openssl assumes S/MIME, finds nothing,
# and prints NOTHING while exiting 0 -- so this check silently passed while
# proving absolutely nothing. Found exactly that way.
echo "     recipients the blob is addressed to:"
SERIALS=$(ssh -o BatchMode=yes "$SERVER_SSH" \
  "/opt/homebrew/bin/openssl cms -cmsout -noout -print -inform DER -in '$BLOB' 2>/dev/null \
   | awk '/serialNumber:/{print \$2}'")
[[ -n "$SERIALS" ]] || fail "could not read recipients from the blob"
echo "$SERIALS" | sed 's/^/       serial /'
NRECIP=$(echo "$SERIALS" | grep -c .)
MINE=$(openssl x509 -in "$CERT" -noout -serial | cut -d= -f2)
MINE_DEC=$(/usr/bin/python3 -c "print(int('$MINE',16))")
if echo "$SERIALS" | grep -qx "$MINE_DEC"; then
  echo "       ^ one of these is YOUR certificate ($NRECIP recipient(s) total)"
else
  fail "the blob is NOT addressed to your certificate -- the server has a stale .crt"
fi

# 4 -------------------------------------------------------------------------
echo "4/6  Decrypting HERE, with the key from your Keychain..."
scp -q "$SERVER_SSH:$BLOB" "$TMP/blob.cms" || fail "could not copy the blob down"
DEFLECTOR_CAPTURE_CERT="$CERT" "$DECRYPT" "$TMP/blob.cms" > "$TMP/plain.json" 2>"$TMP/err" \
  || { sed 's/^/     /' "$TMP/err" >&2; fail "decrypt failed (are you in a GUI session?)"; }
echo "     OK -- $(wc -c < "$TMP/plain.json" | tr -d ' ') bytes of plaintext"

# 5 -------------------------------------------------------------------------
echo "5/6  Checking the plaintext is really yours..."
/usr/bin/python3 - "$TMP/plain.json" "$MARK" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); mark = sys.argv[2]
if mark not in json.dumps(d):
    print(f"     marker {mark} NOT present"); raise SystemExit(1)
print(f"     marker found in the decrypted capture")
print(f"     client   : {d.get('client')}")
print(f"     model    : {d.get('model')}")
print(f"     trace_id : {d.get('trace_id')}")
for k in ("request_in", "request_upstream", "response_out"):
    v = d.get(k)
    print(f"     {k:<16}: {'present' if v else 'MISSING'} ({len(v or '')} chars)")
PY
[[ ${PIPESTATUS[0]:-0} -eq 0 ]] || fail "marker missing -- decrypted someone else's blob?"
TRACE=$(/usr/bin/python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('trace_id',''))" "$TMP/plain.json")

# 6 -------------------------------------------------------------------------
echo "6/6  Server-side trace for this request..."
ssh -o BatchMode=yes "$SERVER_SSH" \
  "cd ~/ai-stack/agentstop-mw && ./.venv/bin/python scripts/deflector-logs.py --id '$TRACE' 2>/dev/null | sed 's/^/     /'" || true

cat <<EOF

RESULT: PASS
  The server wrote a capture it cannot read.
  You read it, and it contains your marker.
EOF
