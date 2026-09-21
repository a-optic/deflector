#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Install the Deflector capture client on THIS Mac.
#
#   ./install-client.sh [--server <host>]
#
# Copies the client tools into ~/.agentstop/bin, puts them on your PATH, and
# tells you the one command left to run. Idempotent -- safe to re-run to
# upgrade after pulling new changes.
#
# This exists because "install the client and it works" was not true: the
# tools had to be copied by hand and `agentstop` was not on anyone's PATH.

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEST="$HOME/.agentstop"
SERVER=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) SERVER="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# Client tools only. Nothing here belongs on the server, and the server's own
# scripts are deliberately not copied to clients.
TOOLS=(agentstop decrypt-capture.sh import-capture-key.sh
       make-backup-key.sh capture-selftest.sh)

mkdir -p "$DEST/bin" "$DEST/keys"
chmod 700 "$DEST/keys"

missing=()
for t in "${TOOLS[@]}"; do
  if [[ -f "$HERE/$t" ]]; then
    install -m 755 "$HERE/$t" "$DEST/bin/$t"
  else
    missing+=("$t")
  fi
done
if (( ${#missing[@]} )); then
  echo "missing from $HERE: ${missing[*]}" >&2
  echo "run this from the repo's scripts/ directory" >&2
  exit 1
fi
echo "installed ${#TOOLS[@]} tools into $DEST/bin"

# --- PATH ---------------------------------------------------------------------
# Written to the login file for the shell actually in use. A symlink into a
# Homebrew bin would be fewer steps, but that prefix can sit on an external
# volume -- and a command that vanishes when a disk unmounts is worse than an
# extra line here.
case "$(basename "${SHELL:-/bin/zsh}")" in
  zsh)  RC="$HOME/.zprofile" ;;
  bash) RC="$HOME/.bash_profile" ;;
  *)    RC="" ;;
esac

if [[ -z "$RC" ]]; then
  echo "unrecognised shell ${SHELL:-}; add this to your login file yourself:"
  echo '  export PATH="$HOME/.agentstop/bin:$PATH"'
elif grep -qF '.agentstop/bin' "$RC" 2>/dev/null; then
  echo "PATH already configured in $RC"
else
  printf '\n# Deflector capture client\nexport PATH="$HOME/.agentstop/bin:$PATH"\n' >> "$RC"
  echo "added $DEST/bin to your PATH in $RC"
  echo "  (open a new terminal, or run:  source $RC)"
fi

# --- what is still needed -----------------------------------------------------
have_real_openssl=no
while IFS= read -r d; do
  [[ -n "$d" && -x "$d/openssl" ]] || continue
  [[ "$("$d/openssl" version 2>/dev/null)" == OpenSSL* ]] && { have_real_openssl=yes; break; }
done < <(printf '%s' "$PATH" | tr ':' '\n')

echo
echo "Next:"
if [[ -n "$SERVER" ]]; then
  echo "  agentstop enroll --server $SERVER"
else
  echo "  agentstop enroll --server <deflector-host>"
fi
echo "  agentstop status"
echo
echo "Run those at this Mac's own screen -- writing to the login Keychain needs"
echo "a GUI session, and an ssh session cannot do it."

if [[ "$have_real_openssl" != yes ]]; then
  echo
  echo "Note: only Apple's LibreSSL is on your PATH. Reading captures works fine,"
  echo "but creating a BACKUP key needs real OpenSSL:  brew install openssl"
fi
command -v bw >/dev/null || {
  echo
  echo "Optional, to store the backup key automatically:  brew install bitwarden-cli"; }
