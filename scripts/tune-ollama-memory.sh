#!/usr/bin/env bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# tune-ollama-memory.sh — cap resident model footprint on both ollama daemons.
#
# WHY THIS EXISTS
# ---------------
# Run this as root; it edits LaunchDaemon plists and restarts the jobs.
#
# On 2026-09-20 this box kernel-panicked. Both ollama servers ran with
# OLLAMA_KEEP_ALIVE=24h and permissive OLLAMA_MAX_LOADED_MODELS (2 on main, 3 on
# tasks), so finished models never released. Four models reached 129.0GB
# resident on a 137.4GB machine. What followed, in order: swap grew 4GB -> 12GB
# -> 14GB; Jetsam fired at 18:09 naming ollama as largestProcess at 119.9GB; the
# VM compressor hit "100% of segments limit (BAD) with 100 swapfiles"; userspace
# stalled hard enough that watchdogd missed check-ins for 94 seconds; the kernel
# watchdog panicked and the box rebooted at 19:10:32.
#
# The same exhaustion produced the milder symptom first: requests to a 33.8GB
# model returned ZERO bytes because prefill could not finish in swap, so all 104
# of them died at exactly upstream.local_read_timeout_s (ttfb=270.0), which Pi
# read as a provider fault and retried until the Deflector's repeat-guard 400'd
# it. Neither the timeout nor the guard was the bug; both were reporting this.
#
# WHAT IT CHANGES, AND WHY EACH
#   OLLAMA_KEEP_ALIVE      24h -> 1h  keep_alive is an IDLE timer, not a job
#                                     timeout: it resets on every request, so it
#                                     never truncates a long job. A high value
#                                     only means a finished model squats. A cold
#                                     reload of 33.8GB measured 19.9s.
#   OLLAMA_MAX_LOADED_MODELS  main 2->1, tasks 3->2
#                                     Not a cap on which models you may use --
#                                     ollama evicts least-recently-used to make
#                                     room, so all of them stay reachable. It is
#                                     the hard backstop for ollama's fit
#                                     estimator, which is optimistic on unified
#                                     memory: it was observed loading 33.8GB
#                                     alongside 68.4GB rather than evicting.
#   OLLAMA_NUM_PARALLEL    tasks 2->1 KV cache is reserved as context x parallel,
#                                     so 2 silently doubles it per loaded model.
#
# The two servers share one pool of RAM and cannot see each other's residency,
# so no setting makes every combination safe. With a giant on main, tasks holds
# exactly one mid-size model. Use ollama-mem.sh to see the combined figure and
# to free something on purpose.
#
#   sudo tune-ollama-memory.sh                  both servers
#   sudo tune-ollama-memory.sh main             :11434 only
#   sudo tune-ollama-memory.sh tasks            :11435 only
#   sudo tune-ollama-memory.sh --context 65536  also pin tasks' context length
#
# --context is opt-in because it is the one value here that trades capability
# for headroom. A reboot re-read the tasks plist and its context went 65536 ->
# 131072, doubling KV reservation per model; pass this to put it back. The
# script prints every current value before touching it, so check that line first.
#
# Every plist is backed up before it is edited and restored if validation fails.
# Rollback instructions print at the end.

set -uo pipefail

PB=/usr/libexec/PlistBuddy
LD=/Library/LaunchDaemons
BACKUP_DIR="$LD/.ollama-env-backups"
STAMP=$(date +%Y%m%d-%H%M%S)

CONTEXT=""
TARGETS=()
while [ $# -gt 0 ]; do
  case $1 in
    --context) CONTEXT=${2:?--context needs a value}; shift 2 ;;
    main|tasks) TARGETS+=("$1"); shift ;;
    -h|--help)
      awk 'NR>=6 && /^#/ {sub(/^# ?/, ""); print; next} NR>=6 {exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=(main tasks)

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root:  sudo ${0##*/} $*" >&2
  exit 1
fi

port_for() { case $1 in main) echo 11434 ;; tasks) echo 11435 ;; esac; }

# Settings per server. Deliberately not a hardcoded plist name: the daemon is
# found by the port it serves, so this survives a relabel and carries no
# site-specific identifiers.
settings_for() {
  case $1 in
    main)  echo "OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_KEEP_ALIVE=1h" ;;
    tasks) echo "OLLAMA_MAX_LOADED_MODELS=2 OLLAMA_KEEP_ALIVE=1h OLLAMA_NUM_PARALLEL=1${CONTEXT:+ OLLAMA_CONTEXT_LENGTH=$CONTEXT}" ;;
  esac
}

# The plist whose EnvironmentVariables bind OLLAMA_HOST to this port.
find_plist() {
  local port=$1 p host
  for p in "$LD"/*.plist; do
    [ -f "$p" ] || continue
    host=$($PB -c "Print :EnvironmentVariables:OLLAMA_HOST" "$p" 2>/dev/null) || continue
    case $host in *":$port") echo "$p"; return 0 ;; esac
  done
  return 1
}

mkdir -p "$BACKUP_DIR"
rc=0
touched=()

for t in "${TARGETS[@]}"; do
  port=$(port_for "$t")
  echo "══════════════════════════════════════════════════════════════"
  printf '%s (:%s)\n' "$t" "$port"

  if ! plist=$(find_plist "$port"); then
    echo "  SKIP: no LaunchDaemon binds OLLAMA_HOST to :$port"; rc=1; continue
  fi
  label=$($PB -c "Print :Label" "$plist" 2>/dev/null)
  if [ -z "$label" ]; then
    echo "  SKIP: ${plist##*/} has no :Label"; rc=1; continue
  fi
  echo "  plist : ${plist##*/}"
  echo "  label : $label"

  echo "  current:"
  for kv in $(settings_for "$t"); do
    k=${kv%%=*}
    cur=$($PB -c "Print :EnvironmentVariables:$k" "$plist" 2>/dev/null) || cur="<not set>"
    printf '    %-28s %s\n' "$k" "$cur"
  done

  bak="$BACKUP_DIR/${plist##*/}.$STAMP"
  if ! cp -p "$plist" "$bak"; then
    echo "  SKIP: backup failed"; rc=1; continue
  fi

  ok=1
  for kv in $(settings_for "$t"); do
    k=${kv%%=*}; v=${kv#*=}
    if ! $PB -c "Set :EnvironmentVariables:$k $v" "$plist" 2>/dev/null \
       && ! $PB -c "Add :EnvironmentVariables:$k string $v" "$plist" 2>/dev/null; then
      echo "  FAILED to set $k"; ok=0; break
    fi
  done
  if [ $ok -eq 1 ] && ! plutil -lint "$plist" >/dev/null 2>&1; then
    echo "  plutil validation FAILED"; ok=0
  fi
  if [ $ok -ne 1 ]; then
    cp -p "$bak" "$plist"
    echo "  RESTORED from backup; not restarting"; rc=1; continue
  fi

  echo "  new:"
  for kv in $(settings_for "$t"); do
    k=${kv%%=*}
    printf '    %-28s %s\n' "$k" "$($PB -c "Print :EnvironmentVariables:$k" "$plist" 2>/dev/null)"
  done

  # MUST be bootout+bootstrap, never `kickstart -k`. kickstart restarts the
  # RUNNING job from launchd's cached job definition, which does not include the
  # edit just written to disk -- the plist changes, the process restarts, and
  # nothing about its environment differs. That is how this box's 12:15 restart
  # on 2026-09-20 silently applied nothing, and it is not observable except by
  # diffing `ps eww` against the file. Only unloading and reloading re-reads it.
  echo "  reloading from disk ..."
  launchctl bootout "system/$label" 2>/dev/null
  # bootout returns before the job is gone; bootstrapping into a still-loaded
  # label fails with EALREADY, so wait for it to actually leave.
  for _ in $(seq 1 20); do
    launchctl print "system/$label" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if ! launchctl bootstrap system "$plist" 2>/dev/null; then
    echo "  BOOTSTRAP FAILED -- job may be unloaded; reload it with:" >&2
    echo "    launchctl bootstrap system '$plist'" >&2
    rc=1; continue
  fi

  for _ in $(seq 1 45); do
    curl -sf --max-time 2 "http://127.0.0.1:$port/api/tags" >/dev/null 2>&1 && break
    sleep 1
  done
  if curl -sf --max-time 2 "http://127.0.0.1:$port/api/tags" >/dev/null 2>&1; then
    echo "  :$port responding"
  else
    echo "  WARNING: :$port not responding after 45s"; rc=1
  fi
  touched+=("$label|$bak|$plist")
done

echo "══════════════════════════════════════════════════════════════"
echo "running environment now:"
for t in "${TARGETS[@]}"; do
  port=$(port_for "$t")
  pid=$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1)
  printf '── %s (:%s, pid %s) ──\n' "$t" "$port" "${pid:-none}"
  [ -n "${pid:-}" ] && ps eww -o command -p "$pid" | tr ' ' '\n' | grep -E '^OLLAMA' | sed 's/^/    /'
done

if [ ${#touched[@]} -gt 0 ]; then
  echo
  echo "rollback:"
  for e in "${touched[@]}"; do
    IFS='|' read -r label bak plist <<< "$e"
    # Same reload rule as above: restoring the file is not enough on its own.
    echo "  sudo cp '$bak' '$plist' && sudo launchctl bootout 'system/$label'; sudo launchctl bootstrap system '$plist'"
  done
fi
exit $rc
