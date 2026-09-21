#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Decrypt a Deflector capture blob. RUN THIS ON THE CLIENT MACHINE -- the one
# holding the private key. The server cannot decrypt these files; that is the
# entire point of the design.
#
# By default the private key is read from the macOS Keychain and handed to
# openssl through a pipe, so it exists only in memory and never as a file on
# disk. Set up once with scripts/import-capture-key.sh.
#
#   ./decrypt-capture.sh <blob.cms>                    # key from Keychain
#   ./decrypt-capture.sh <blob.cms> --key-file b.key   # e.g. a backup key
#   ./decrypt-capture.sh <blob.cms> | python3 -m json.tool
#
# WHY BASE64 (do not "simplify" this away)
# ----------------------------------------
# `security find-generic-password -w` returns the stored value HEX-ENCODED when
# it is not a plain printable string -- and a PEM contains newlines, so it
# always trips that. Verified: a 3272-byte key came back as 6543 bytes of hex.
# Storing base64 (one printable line, no newlines) makes the round trip
# byte-exact and deterministic instead of depending on that heuristic.
#
# WHY A PIPE
# ----------
# `-inkey <(...)` passes a /dev/fd pipe. openssl parses PEM sequentially, so
# this works (verified) and keeps the key off disk entirely. A temp file would
# be simpler and strictly worse.

set -euo pipefail

# "User interaction is not allowed" has two distinct causes with different
# fixes, so ask launchd which session this is instead of asserting one.
# Unlocking works from a non-GUI session too -- the unlock lives in securityd,
# not in the calling process -- so do not hard-fail on session type alone.
keychain_hint() {
  local mgr; mgr=$(launchctl managername 2>/dev/null || echo unknown)
  echo >&2
  if [[ "$mgr" == "Aqua" ]]; then
    cat >&2 <<'HINT'
This IS a GUI session, so the login keychain is locked rather than
unreachable. Unlock it and re-run:

  security unlock-keychain ~/Library/Keychains/login.keychain-db

It prompts for your login password on this terminal; nothing is passed on the
command line.
HINT
  else
    cat >&2 <<HINT
This is a '$mgr' session, not a GUI (Aqua) one. ssh sessions, launchd jobs,
and tools that shell out on your behalf (Claude Code's '!' prefix, for one)
all land here even on this same Mac -- the login keychain is locked and there
is no way to show the unlock dialog.

Simplest fix: open Terminal.app while logged in at the Mac, and run it there.

Or unlock the keychain first, which also works from this session:

  security unlock-keychain ~/Library/Keychains/login.keychain-db
HINT
  fi
}

BLOB="${1:-}"
shift || true

KEY_FILE=""
CERT="${DEFLECTOR_CAPTURE_CERT:-$HOME/.agentstop/keys/pi.crt}"
KEYCHAIN_SERVICE="${DEFLECTOR_KEY_SERVICE:-deflector-capture-key}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key-file) KEY_FILE="${2:-}"; shift 2 ;;
    --cert)     CERT="${2:-}";     shift 2 ;;
    --service)  KEYCHAIN_SERVICE="${2:-}"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$BLOB" ]]; then
  cat >&2 <<'USAGE'
usage: decrypt-capture.sh <blob.cms> [--key-file PATH] [--cert PATH] [--service NAME]

  default: private key is read from the macOS Keychain
  --key-file: read the key from a file instead (e.g. an offline backup key)
USAGE
  exit 2
fi
[[ -r "$BLOB" ]] || { echo "cannot read blob: $BLOB" >&2; exit 1; }
[[ -r "$CERT" ]] || { echo "cannot read certificate: $CERT" >&2; exit 1; }

# -recip is REQUIRED on the client's LibreSSL (it will not locate the recipient
# from the key alone) and harmless on OpenSSL 3.x, so one command works on both.
if [[ -n "$KEY_FILE" ]]; then
  [[ -r "$KEY_FILE" ]] || { echo "cannot read key file: $KEY_FILE" >&2; exit 1; }
  exec openssl cms -decrypt -inform DER -in "$BLOB" -recip "$CERT" -inkey "$KEY_FILE"
fi

# Fail with something actionable rather than letting openssl report a confusing
# PEM parse error on empty input. "item not found" and "keychain locked" are
# different problems -- reporting the first for the second sends you looking
# for a missing item that is actually sitting right there.
ERR=$(mktemp "${TMPDIR:-/tmp}/dfl-decrypt.XXXXXX")
trap 'rm -f "$ERR"' EXIT
if ! RAW=$(security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w 2>"$ERR"); then
  if grep -q "User interaction is not allowed" "$ERR"; then
    echo "cannot read the Keychain (it is locked or unreachable):" >&2
    sed 's/^/  /' "$ERR" >&2
    keychain_hint
    exit 1
  fi
  cat >&2 <<USAGE
no Keychain item '$KEYCHAIN_SERVICE' for user '$USER'.

  set it up once:   scripts/import-capture-key.sh <your-private-key.pem>
  or decrypt with a key file instead:
                    $(basename "$0") "$BLOB" --key-file /path/to/key.pem
USAGE
  exit 1
fi

if ! printf '%s' "$RAW" | base64 -d 2>/dev/null | head -1 | grep -q -- "-----BEGIN"; then
  cat >&2 <<USAGE
Keychain item '$KEYCHAIN_SERVICE' did not decode to a PEM private key.

  It must be stored BASE64-ENCODED -- 'security -w' hex-encodes raw multi-line
  values, which silently corrupts a PEM. Re-import with:
                    scripts/import-capture-key.sh <your-private-key.pem>
USAGE
  exit 1
fi

openssl cms -decrypt -inform DER -in "$BLOB" -recip "$CERT" \
  -inkey <(printf '%s' "$RAW" | base64 -d)
