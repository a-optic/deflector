#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Move a Deflector capture private key into the macOS Keychain, so it stops
# existing as a file on disk. Run once, on the CLIENT machine.
#
#   ./import-capture-key.sh ~/.agentstop/keys/pi.key         # write it for me
#   ./import-capture-key.sh --gui ~/.agentstop/keys/pi.key   # I'll paste it into
#                                                            # Keychain Access
#   ./import-capture-key.sh --verify ~/.agentstop/keys/pi.key # check, write nothing
#
# The key is stored BASE64-ENCODED. That is not decoration: `security -w`
# returns hex for any value that is not a plain printable string, and a PEM
# contains newlines -- so storing it raw round-trips as hex and silently
# corrupts the key. Verified: 3272 bytes in, 6543 bytes of hex out.
#
# Needs an UNLOCKED login keychain. A GUI (Aqua) session has one already; a
# LaunchDaemon, an ssh session, or a tool that shells out for you does not, and
# this fails with "User interaction is not allowed." The script tells you which
# case you are in and how to fix it.

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

SERVICE="${DEFLECTOR_KEY_SERVICE:-deflector-capture-key}"
MODE=write
KEY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gui)    MODE=gui;    shift ;;
    --verify) MODE=verify; shift ;;
    --service) SERVICE="${2:-}"; shift 2 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)  KEY="$1"; shift ;;
  esac
done

if [[ -z "$KEY" ]]; then
  cat >&2 <<USAGE
usage: $(basename "$0") [--gui|--verify] <private-key.pem> [--service NAME]

  (default)  write the key into the login Keychain and verify it
  --gui      copy the value to the clipboard and print what to type into
             Keychain Access -- for a Mac you drive by screen, not by shell
  --verify   check an already-stored item against the key file; writes nothing
USAGE
  exit 2
fi
# Distinguish "you have not generated a key yet" from "it exists but is
# unreadable". The first is the overwhelmingly common case -- it is step 1 of
# the setup -- and deserves the command rather than a bare errno.
if [[ ! -e "$KEY" ]]; then
  cat >&2 <<USAGE
no such file: $KEY

Generate the key pair first (step 1 of the capture setup), then re-run this:

  mkdir -p "$(dirname "$KEY")" && chmod 700 "$(dirname "$KEY")"
  openssl req -x509 -newkey rsa:4096 -nodes \\
    -keyout "$KEY" -out "${KEY%.*}.crt" \\
    -days 3650 -subj "/CN=deflector-capture"

Keep the .crt: it is public, and the SERVER needs it to encrypt.
USAGE
  exit 1
fi
[[ -r "$KEY" ]] || { echo "cannot read key (permissions?): $KEY" >&2; exit 1; }
head -1 "$KEY" | grep -q -- "-----BEGIN" || {
  echo "not a PEM private key: $KEY" >&2; exit 1; }

ENCODED=$(base64 < "$KEY" | tr -d '\n')

# Read it back and compare against the file. Used by every mode: the GUI path
# has no write step to check, so this IS its safety net before `rm -P`.
verify_round_trip() {
  security find-generic-password -a "$USER" -s "$SERVICE" -w 2>/dev/null \
    | base64 -d 2>/dev/null | cmp -s - "$KEY"
}

if [[ "$MODE" == "verify" ]]; then
  if verify_round_trip; then
    echo "OK: Keychain item '$SERVICE' matches $KEY byte-for-byte."
    echo "Safe to delete the file now:  rm -P \"$KEY\""
    exit 0
  fi
  # "absent", "locked" and "stored but wrong" all fail the compare, and the
  # remedies differ. Saying MISMATCH for a typo'd item name would send you
  # re-pasting a key that was never the problem.
  VERR=$(mktemp "${TMPDIR:-/tmp}/dfl-verify.XXXXXX")
  trap 'rm -f "$VERR"' EXIT
  if ! security find-generic-password -a "$USER" -s "$SERVICE" -w >/dev/null 2>"$VERR"; then
    if grep -q "User interaction is not allowed" "$VERR"; then
      echo "cannot read the Keychain (it is locked or unreachable):" >&2
      sed 's/^/  /' "$VERR" >&2
      keychain_hint
    else
      echo "no Keychain item '$SERVICE' for account '$USER'." >&2
      echo >&2
      echo "Nothing was stored, or the name does not match. In Keychain Access the" >&2
      echo "'Keychain Item Name' must be exactly '$SERVICE' and the" >&2
      echo "'Account Name' exactly '$USER'." >&2
    fi
    echo >&2; echo "Do NOT delete the key file." >&2
    exit 1
  fi
  echo "MISMATCH: item '$SERVICE' exists but does not match $KEY." >&2
  echo >&2
  echo "Most likely the pasted value was truncated, or the key was stored raw" >&2
  echo "instead of base64-encoded. Re-do the import; --gui re-copies the value." >&2
  echo "Do NOT delete the key file." >&2
  exit 1
fi

if [[ "$MODE" == "gui" ]]; then
  # pbcopy talks to the pasteboard of *this* session. Over ssh that is not the
  # one the logged-in user pastes into, and it fails silently -- you would find
  # out by pasting stale clipboard contents into your Keychain. Read it back.
  printf '%s' "$ENCODED" | pbcopy 2>/dev/null || true
  if [[ "$(pbpaste 2>/dev/null)" != "$ENCODED" ]]; then
    cat >&2 <<EOF
could not put the key on the clipboard.

pbcopy writes to the pasteboard of the session it runs in. Over ssh that is
not the pasteboard of the logged-in desktop, so there would be nothing to
paste. Run this from a Terminal on the Mac's own screen (or through Screen
Sharing), or use the non-GUI import instead:

  $0 "$KEY"
EOF
    exit 1
  fi
  cat <<EOF
The base64-encoded key (${#ENCODED} chars) is on your clipboard.

Nothing has been written yet -- do this in Keychain Access:

  1. Open it. On current macOS it is NOT in Utilities and Spotlight will not
     find it; it lives in /System/Library/CoreServices/Applications. Easiest:

       open -b com.apple.keychainaccess

  2. Pick the "login" keychain in the sidebar, then:
       File -> New Password Item...

  3. Fill in EXACTLY these three fields:
       Keychain Item Name:  $SERVICE
       Account Name:        $USER
       Password:            press Cmd-V (already on your clipboard)

  4. Click Add. Then clear the clipboard so the key does not linger there:
       pbcopy </dev/null

  5. Verify before you delete anything:
       $0 --verify "$KEY"
       rm -P "$KEY"

The first decrypt will show a dialog asking whether 'security' may use the
item (exact wording varies by macOS version). That is expected: an item made
in Keychain Access has no ACL entry for /usr/bin/security, unlike one this
script writes. Click Always Allow.

Full click-by-click guide: docs/macos-client-capture-setup.md
EOF
  exit 0
fi

# -U updates in place if the item already exists, so re-running is safe.
# -T /usr/bin/security scopes the ACL to the one tool that reads it back,
# rather than leaving it readable by anything without a prompt.
if ! security add-generic-password -U -a "$USER" -s "$SERVICE" \
       -T /usr/bin/security -w "$ENCODED" 2>/tmp/dfl-import.$$; then
  echo "failed to write to the Keychain:" >&2
  sed 's/^/  /' /tmp/dfl-import.$$ >&2
  grep -q "User interaction is not allowed" /tmp/dfl-import.$$ && keychain_hint
  rm -f /tmp/dfl-import.$$
  exit 1
fi
rm -f /tmp/dfl-import.$$

# Verify the round trip NOW rather than discovering corruption the day you
# actually need to read a capture.
if ! verify_round_trip; then
  echo "round-trip verification FAILED -- the stored value does not match the key." >&2
  echo "The key file has been left in place. Do not delete it." >&2
  exit 1
fi

cat <<EOF
stored '$SERVICE' in the login Keychain, verified byte-identical.

The private key is now in the Keychain. Delete the file so it stops existing
on disk -- that is the whole point:

  rm -P "$KEY"

Keep the .crt: it is public, and the SERVER needs it to encrypt.
Keep your offline BACKUP key somewhere safe and out of the Keychain -- if this
Keychain item is lost, every capture encrypted to this key alone becomes
permanently unreadable.
EOF
