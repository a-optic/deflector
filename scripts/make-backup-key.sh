#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Generate a passphrase-protected BACKUP recipient key for Deflector capture.
#
#   ./make-backup-key.sh [--bitwarden|--print] [--cert-out PATH]
#
# This is NOT an export of your primary key -- nothing can export that, by
# design. It is a second, independent key pair. Its certificate goes in
# `capture.backup_recipients`, after which every NEW capture is encrypted to
# both keys, and losing the primary Keychain item stops being fatal.
#
# ORDER MATTERS. Store the key somewhere safe and shred the local copy BEFORE
# adding the certificate to the server config. Until it is a configured
# recipient the key can decrypt nothing, so the window while it sits on disk is
# the one moment it is worthless. Do it the other way round and you have a
# live decryption key lying in a temp directory.
#
# WHY THIS REFUSES TO RUN ON LibreSSL
# -----------------------------------
# macOS ships LibreSSL, which writes passphrase-protected keys in the
# traditional PEM format (`DEK-Info:`). That format derives the key with MD5 at
# ONE iteration -- effectively no work factor, so the passphrase falls to
# offline guessing at enormous speed. Real OpenSSL writes PKCS#8 with PBKDF2;
# this script pins 600,000 iterations of HMAC-SHA256.
#
# Verified: LibreSSL 3.3.6 can READ the resulting key, so recovery still works
# on a stock macOS client. It just must not be the thing that WRITES it.

set -euo pipefail

ITER=600000
BITS=4096
MODE=copy
CERT_OUT=""
BW_ITEM_NAME="${DEFLECTOR_BW_ITEM:-Deflector capture backup key}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --print)     MODE=print; shift ;;
    --bitwarden) MODE=bitwarden; shift ;;
    --cert-out) CERT_OUT="${2:-}"; shift 2 ;;
    -h|--help)  sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# Find real OpenSSL rather than assuming Homebrew's default prefix. Hardcoding
# /opt/homebrew cost real time here: a Mac with brew on an external volume was
# wrongly declared incapable of generating a key it could generate perfectly
# well. Search PATH too, and report everything tried.
# An explicit DEFLECTOR_OPENSSL is authoritative: if you name a binary and it
# is not OpenSSL, that is an error, not an invitation to quietly use a
# different one you did not ask for.
if [[ -n "${DEFLECTOR_OPENSSL:-}" ]]; then
  CANDIDATES=("$DEFLECTOR_OPENSSL")
else
  # Every openssl on PATH, not just the first: /usr/bin/openssl (LibreSSL)
  # normally shadows the real one, so stopping at the first hit would reject a
  # machine that has both.
  CANDIDATES=()
  while IFS= read -r d; do
    [[ -n "$d" && -x "$d/openssl" ]] && CANDIDATES+=("$d/openssl")
  done < <(printf '%s' "$PATH" | tr ':' '\n')
  CANDIDATES+=(/opt/homebrew/bin/openssl /usr/local/bin/openssl /opt/local/bin/openssl)
fi

TRIED=()
OPENSSL=""
for cand in "${CANDIDATES[@]}"; do
  [[ -n "$cand" && -x "$cand" ]] || continue
  ver=$("$cand" version 2>/dev/null || true)
  TRIED+=("$cand ($ver)")
  if [[ "$ver" == OpenSSL* ]]; then OPENSSL="$cand"; break; fi
done

if [[ -z "$OPENSSL" ]]; then
  {
    echo "no real OpenSSL found. Tried:"
    printf '  %s\n' "${TRIED[@]:-<nothing executable>}"
    cat <<'EOF'

Apple ships LibreSSL, whose `genrsa -aes256` writes the traditional PEM format
-- key derived with MD5 at ONE iteration, which is not meaningful protection
for a passphrase. Real OpenSSL writes PKCS#8 with PBKDF2.

  brew install openssl

If brew lives somewhere non-standard, point at it directly:
  DEFLECTOR_OPENSSL=/path/to/bin/openssl ./make-backup-key.sh ...
EOF
  } >&2
  exit 1
fi

CERT_OUT="${CERT_OUT:-$HOME/.agentstop/keys/backup.crt}"
mkdir -p "$(dirname "$CERT_OUT")"

# 0700 workspace, shredded on any exit path. `rm -P` overwrites before
# unlinking; on an SSD that is best-effort, not a guarantee -- which is exactly
# why the key must reach its real home and be removed promptly.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/dfl-backup.XXXXXX")
chmod 700 "$WORK"
cleanup() { [[ -f "$WORK/k.plain" ]] && rm -P "$WORK/k.plain" 2>/dev/null
            [[ -f "$WORK/k.enc"   ]] && rm -P "$WORK/k.enc"   2>/dev/null
            rm -rf "$WORK"; }
trap cleanup EXIT

# --- passphrase, read once, never on argv and never in the environment -------
echo "Choose a passphrase for the backup key."
echo "Store it SEPARATELY from the key itself -- if both live in the same vault,"
echo "one compromise gets both and the passphrase adds nothing."
echo
read -rsp "Passphrase: " P1; echo
read -rsp "Again     : " P2; echo
[[ "$P1" == "$P2" ]] || { echo "passphrases do not match" >&2; exit 1; }
[[ ${#P1} -ge 12 ]] || { echo "use at least 12 characters" >&2; exit 1; }
unset P2
pf() { printf '%s' "$P1"; }   # fed to openssl over fd 3

echo
echo "Generating a ${BITS}-bit key (this takes a moment)..."
"$OPENSSL" genrsa -out "$WORK/k.plain" "$BITS" 2>/dev/null

echo "Wrapping it: PKCS#8, AES-256-CBC, PBKDF2-HMAC-SHA256 x $ITER..."
"$OPENSSL" pkcs8 -topk8 -v2 aes-256-cbc -v2prf hmacWithSHA256 -iter "$ITER" \
  -in "$WORK/k.plain" -out "$WORK/k.enc" -passout fd:3 3< <(pf)
rm -P "$WORK/k.plain"

"$OPENSSL" req -x509 -new -key "$WORK/k.enc" -passin fd:3 3< <(pf) \
  -out "$CERT_OUT" -days 3650 -subj "/CN=deflector-capture-backup" 2>/dev/null
chmod 644 "$CERT_OUT"

# --- prove it works BEFORE you rely on it ------------------------------------
# A backup you have not restored from is not a backup. This catches a wrong
# passphrase or a key/cert mismatch now, rather than during a real recovery.
echo "Verifying the key and passphrase actually decrypt..."
printf 'deflector-backup-selfcheck' > "$WORK/probe.txt"
"$OPENSSL" cms -encrypt -binary -aes-256-cbc -outform DER \
  -in "$WORK/probe.txt" -out "$WORK/probe.cms" "$CERT_OUT"
OUT=$("$OPENSSL" cms -decrypt -inform DER -in "$WORK/probe.cms" \
        -recip "$CERT_OUT" -inkey "$WORK/k.enc" -passin fd:3 3< <(pf) 2>/dev/null || true)
[[ "$OUT" == "deflector-backup-selfcheck" ]] || {
  echo "VERIFICATION FAILED -- not writing anything. Nothing was stored." >&2
  rm -f "$CERT_OUT"; exit 1; }
echo "  OK: round trip succeeded."

ACTUAL_ITER=$("$OPENSSL" asn1parse -in "$WORK/k.enc" 2>/dev/null \
  | awk '/INTEGER/{v=$NF} /hmacWithSHA256/{print v; exit}' | sed 's/://')
echo "  KDF recorded in the key: PBKDF2-HMAC-SHA256, $((16#${ACTUAL_ITER:-0})) iterations"
echo

# --- hand it over ------------------------------------------------------------

# Store in Bitwarden without the key ever appearing in a process listing.
# `bw create` reads the encoded item from STDIN, and `bw encode` reads from
# stdin too, so the whole pipeline avoids argv -- where anyone running `ps`
# could have read it. The master password is typed at bw's own prompt and the
# session key stays in the environment, never on a command line either.
bw_store() {
  local keyfile="$1"

  command -v bw >/dev/null || {
    echo "the Bitwarden CLI is not installed:  brew install bitwarden-cli" >&2
    return 1; }

  local state
  state=$(bw status 2>/dev/null | /usr/bin/python3 -c \
            'import json,sys;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)
  case "$state" in
    unauthenticated|"")
      echo "not logged in to Bitwarden. Run:  bw login" >&2; return 1 ;;
    locked)
      echo "Unlocking your vault (Bitwarden will prompt for your master password)..."
      BW_SESSION=$(bw unlock --raw) || { echo "unlock failed" >&2; return 1; }
      export BW_SESSION ;;
  esac

  # Refuse to silently create a second copy -- two items with one name is how
  # you end up restoring the wrong key.
  local existing
  existing=$(bw list items --search "$BW_ITEM_NAME" 2>/dev/null | /usr/bin/python3 -c \
    'import json,sys
try: items=json.load(sys.stdin)
except Exception: items=[]
print(len([i for i in items if i.get("name")=="'"$BW_ITEM_NAME"'"]))' 2>/dev/null || echo 0)
  if [[ "${existing:-0}" != "0" ]]; then
    echo "Bitwarden already has an item named '$BW_ITEM_NAME'." >&2
    echo "Rename or delete it first, or set DEFLECTOR_BW_ITEM to a different name." >&2
    return 1
  fi

  local certfp
  certfp=$("$OPENSSL" x509 -in "$CERT_OUT" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)

  local created id
  created=$(/usr/bin/python3 - "$keyfile" "$BW_ITEM_NAME" "$certfp" <<'PYJSON' | bw encode | bw create item
import json, sys, datetime
key, name, fp = open(sys.argv[1]).read(), sys.argv[2], sys.argv[3]
print(json.dumps({
    "type": 2, "name": name, "favorite": False, "notes": key,
    "secureNote": {"type": 0},
    "fields": [
        {"name": "cert_sha256", "value": fp, "type": 0},
        {"name": "created", "value": datetime.date.today().isoformat(), "type": 0},
        {"name": "purpose", "value": "Deflector capture backup recipient. "
                                     "Passphrase stored separately.", "type": 0},
    ],
}))
PYJSON
) || { echo "Bitwarden rejected the item" >&2; return 1; }

  id=$(printf '%s' "$created" | /usr/bin/python3 -c \
        'import json,sys;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
  [[ -n "$id" ]] || { echo "could not read the new item id back" >&2; return 1; }

  # Verify by reading it back. A backup you have not restored from is not a
  # backup, and this is the cheapest possible restore test.
  local roundtrip
  roundtrip=$(bw get item "$id" 2>/dev/null | /usr/bin/python3 -c \
                'import json,sys;print(json.load(sys.stdin).get("notes",""),end="")')
  if [[ "$roundtrip" != "$(cat "$keyfile")" ]]; then
    echo "VERIFICATION FAILED: what Bitwarden stored does not match the key." >&2
    echo "Item id $id -- delete it and retry. Nothing else was changed." >&2
    return 1
  fi

  echo "  stored in Bitwarden as '$BW_ITEM_NAME' and read back byte-identical."
  echo "  item id: $id"
  return 0
}

if [[ "$MODE" == "bitwarden" ]]; then
  if ! bw_store "$WORK/k.enc"; then
    echo >&2
    echo "Nothing was saved to Bitwarden. The certificate is still at:" >&2
    echo "  $CERT_OUT" >&2
    echo "Re-run once the problem above is fixed, or use --print to store it by hand." >&2
    exit 1
  fi
  cat <<EOF

Store the PASSPHRASE somewhere other than that same Bitwarden item -- if one
compromise yields both, the passphrase has added nothing.
EOF
elif [[ "$MODE" == "copy" ]]; then
  pbcopy < "$WORK/k.enc"
  if [[ "$(pbpaste)" != "$(cat "$WORK/k.enc")" ]]; then
    echo "could not put the key on the clipboard -- re-run with --print" >&2; exit 1
  fi
  cat <<EOF
The encrypted private key ($(wc -c < "$WORK/k.enc" | tr -d ' ') chars) is on your clipboard.

  NOTE: if Handoff / Universal Clipboard is on, your clipboard syncs to your
  other Apple devices. Turn it off first, or use --print instead.

Paste it into Bitwarden now:
  New item -> Secure Note
  Name : Deflector capture backup key
  Notes: paste (Cmd-V)
Then clear the clipboard:  pbcopy </dev/null

Store the PASSPHRASE somewhere else -- not in that same note.
EOF
else
  echo "----- copy everything between the lines into your password manager -----"
  cat "$WORK/k.enc"
  echo "----------------------------------------------------------------------"
  echo
  echo "NOTE: this is now in your terminal scrollback. Clear it when done"
  echo "(Terminal: Edit > Clear Scrollback, or Cmd-K)."
fi

cat <<EOF

Certificate written to: $CERT_OUT   (public -- this is the half the server needs)

NEXT, in this order:
  1. Confirm the key is saved and the passphrase is recorded separately.
  2. Let this script exit -- the local key copy is shredded automatically.
  3. Add the certificate to the SERVER's config.yaml:
         capture:
           backup_recipients:
             - ~/.agentstop/keys/backup.crt
     and restart Deflector.
  4. Run a recovery drill: make one new capture and decrypt it with the BACKUP
     key alone, pulling it back out of your password manager. A backup you have
     never restored from is not a backup.

Existing captures are NOT re-encrypted -- backup_recipients applies to new
blobs only.
EOF
