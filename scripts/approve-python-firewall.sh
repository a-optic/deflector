#!/bin/bash
# Re-approve the interpreter Deflector runs under in the macOS Application
# Firewall (ALF). Must run as root; socketfilterfw refuses otherwise.
#
# WHY THIS EXISTS
# ---------------
# ALF pins its allow-list entries to a RESOLVED BINARY PATH. Homebrew installs
# python into a versioned Cellar directory, so every `brew upgrade python@3.14`
# produces a new path that ALF has never seen and therefore silently drops
# inbound connections to.
#
# The failure mode is genuinely awful to diagnose, which is the real reason
# this script exists rather than a calendar reminder:
#   - the TCP handshake still completes (the kernel does that before ALF drops
#     the payload), so the client sees a healthy ESTABLISHED socket
#   - not one byte is ever returned, and the server-side application NEVER
#     RUNS -- no access log line, no request trace, no upstream call
#   - loopback is unaffected, so every local test passes and the service looks
#     perfectly healthy from the machine it runs on
#   - the client just hangs until its own timeout fires
# Observed in production as ~5-minute client timeouts with zero server-side
# evidence, after an unattended python 3.14.5 -> 3.14.7 upgrade.
#
# NOTE ON THE ALTERNATIVE: creating the venv with `python -m venv --copies`
# gives a stable binary path, but on a Homebrew framework build that copy still
# links against .../Cellar/python@3.14/<version>/Frameworks/... -- so the first
# `brew cleanup` that removes the old version breaks the interpreter outright.
# Do not "fix" this that way.

set -euo pipefail

FW=/usr/libexec/ApplicationFirewall/socketfilterfw
# Default derived from this script's own location rather than a hardcoded home
# directory: the checkout path is operator-specific and this file is published.
# Resolves correctly under launchd too, where $0 is the absolute path given in
# ProgramArguments. Pass an explicit interpreter as $1 to override.
REPO_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
VENV_PY="${1:-$REPO_ROOT/.venv/bin/python3}"

if [[ $EUID -ne 0 ]]; then
  echo "must run as root (socketfilterfw requires it)" >&2
  exit 1
fi

if [[ ! -e "$VENV_PY" ]]; then
  echo "interpreter not found: $VENV_PY" >&2
  exit 1
fi

# Follow the venv symlink chain to the real Mach-O behind it.
REAL=$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$VENV_PY")

# Refuse to approve anything outside Homebrew's python Cellar.
#
# The venv resolves through /opt/homebrew/opt/python@3.14, and that directory
# (/opt/homebrew/opt, 775 group:admin) is writable by ANY admin-group account
# on this machine -- not just the owner. Without this guard, such an account
# could repoint that symlink at an attacker-controlled tree and the next
# trigger would have ROOT firewall-approve their binary for inbound LAN
# traffic, silently bypassing the interactive "allow incoming connections?"
# consent prompt that is the entire point of ALF.
#
# HONEST LIMIT OF THIS GUARD: /opt/homebrew/Cellar is itself 775 group:admin,
# so an admin-group attacker can create or replace directories under it and
# craft a path that satisfies this check. This is defense in depth, not a
# boundary. The underlying exposure is that the whole Homebrew prefix is
# admin-writable, which already lets such an attacker tamper with the
# interpreter this service runs under -- with or without this script. The
# guard is still worth having: it costs nothing and blocks the trivial
# "repoint the symlink at /tmp" version outright. A codesign check was
# considered and REJECTED as security theatre here: Homebrew's python is
# ad-hoc signed (Signature=adhoc, TeamIdentifier not set), so there is no
# authority to pin and an attacker can ad-hoc sign their own build just as
# easily.
case "$REAL" in
  /opt/homebrew/Cellar/python@*) ;;
  *)
    echo "refusing: resolved interpreter is outside the Homebrew python Cellar" >&2
    echo "  resolved: $REAL" >&2
    exit 1
    ;;
esac

# CAREFUL: realpath is NOT the identity ALF uses on a Homebrew framework build.
# It lands on .../Versions/<X.Y>/bin/python<X.Y>, a ~34KB stub, but the kernel
# actually execs the framework's app bundle -- `ps -o comm=` on a live process
# reports .../Versions/<X.Y>/Resources/Python.app/Contents/MacOS/Python, and the
# existing ALF entries are .app bundle paths. Approving the stub path looks
# like it worked and changes nothing. Prefer the bundle when the layout has
# one, and fall back to the resolved binary for non-framework builds.
VERSIONS_DIR="${REAL%/bin/*}"
APP_BUNDLE="$VERSIONS_DIR/Resources/Python.app"
# Test the executable INSIDE the bundle, not just that the directory exists:
# a brew bottle extraction can create the .app directory before the Mach-O
# inside it is written, and approving a half-populated bundle would look
# like success while registering something the kernel never execs.
if [[ -x "$APP_BUNDLE/Contents/MacOS/Python" ]]; then
  TARGET="$APP_BUNDLE"
else
  TARGET="$REAL"
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) approving python for inbound connections"
echo "  interpreter : $VENV_PY"
echo "  resolves to : $REAL"
echo "  registering : $TARGET"

# Do NOT swallow socketfilterfw's own output -- its confirmation/error line is
# the only diagnostic that survives into the log for a later post-mortem.
"$FW" --add "$TARGET"        2>&1 | sed 's/^/  add       : /'
"$FW" --unblockapp "$TARGET" 2>&1 | sed 's/^/  unblock   : /'

# Verify against the allow list rather than trusting exit codes or
# --getappblocked. Observed during development: --getappblocked reports
# "permitted" for a binary that is NOT in the list at all (it appears to
# describe default policy, not membership), so it cannot distinguish "we
# registered it" from "we did nothing". Listing membership can.
if "$FW" --listapps 2>/dev/null | grep -qF -- "$TARGET"; then
  echo "  verified  : present in the firewall allow list"
else
  echo "  FAILED    : not present in the allow list after --add" >&2
  exit 1
fi
