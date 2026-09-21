#!/usr/bin/env bash
# ensure-open-brain.sh — guarantee Open Brain's containers are actually up.
#
# ~/ai-stack/open-brain/docker-compose.yml already sets `restart:
# unless-stopped` on all three services (openbrain-api, openbrain-dashboard,
# openbrain-postgres), so Docker's own engine should bring them back whenever
# dockerd (inside Colima's Lima VM) restarts. Colima itself is a LaunchDaemon
# and comes up on boot on its own.
#
# What neither of those covers: Colima's LaunchDaemon reporting "started"
# doesn't mean the Lima VM and the dockerd inside it are actually ready yet --
# there's a real startup lag, and this box's Colima VM hadn't been through a
# full reboot cycle in 7+ weeks as of 2026-09-15, so that race was untested.
# This script is a verify-and-heal pass: it waits for the docker daemon to
# actually answer, then idempotently runs `docker compose up -d` (a no-op for
# anything already running and healthy), then waits for Open Brain's own
# /health endpoint specifically -- "container running" and "API answering"
# are not the same thing when postgres might still be catching up.
#
# Explicitly targets Colima's docker socket via DOCKER_HOST, not whatever
# the interactive shell's default happens to be (this box's .zshrc defaults
# DOCKER_HOST to a different runtime, Socktainer -- Open Brain has always
# been documented as running under Colima, and this script should not
# silently follow the shell default to the wrong daemon).
#
# Idempotent and safe to run by hand any time: scripts/ensure-open-brain.sh

set -euo pipefail

BRAIN_DIR="$HOME/ai-stack/open-brain"
DOCKER_COMPOSE_BIN="/opt/homebrew/bin/docker-compose"
DOCKER_SOCK="$HOME/.colima/default/docker.sock"
export DOCKER_HOST="unix://${DOCKER_SOCK}"

LOG="$HOME/.agentstop/logs/open-brain-ensure.log"
MAX_WAIT_S=180
POLL_S=5

mkdir -p "$(dirname "$LOG")"
log() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"
}

log "ensure-open-brain: starting (DOCKER_HOST=$DOCKER_HOST)"

# 1. Wait for the docker daemon inside the VM to actually answer -- Colima's
#    own daemon being "up" does not mean this is ready yet, and the socket
#    FILE not existing yet is just an earlier point on that same timeline,
#    not a different failure -- both must retry the same way. (This used to
#    be a separate, non-retrying check that hard-failed if the socket wasn't
#    there yet; on 2026-09-15 that fired for real, at boot, before Colima's
#    own dependency chain had finished -- see git history for the actual
#    root cause that day, an unlinked `docker` brew keg blocking `colima
#    start` entirely. Deleted the separate check; docker-compose failing to
#    connect covers both cases identically.) `ps` (unlike `version`) actually
#    round-trips to the daemon, so it's the real gate.
waited=0
until "$DOCKER_COMPOSE_BIN" --project-directory "$BRAIN_DIR" ps >/dev/null 2>&1; do
  if [ "$waited" -ge "$MAX_WAIT_S" ]; then
    log "ensure-open-brain: FAILED -- daemon at $DOCKER_HOST not responding after ${MAX_WAIT_S}s"
    exit 1
  fi
  sleep "$POLL_S"
  waited=$((waited + POLL_S))
done
log "ensure-open-brain: daemon responding after ${waited}s"

# 2. Idempotently ensure the compose project is up. No-op for anything
#    already running and healthy; heals anything that didn't come back
#    on its own restart policy.
if ! "$DOCKER_COMPOSE_BIN" --project-directory "$BRAIN_DIR" up -d >>"$LOG" 2>&1; then
  log "ensure-open-brain: FAILED -- docker compose up -d exited non-zero, see log above"
  exit 1
fi

# 3. Wait for the actual health endpoint, not just "container running" --
#    openbrain-api depends_on postgres:condition:service_healthy, so a slow
#    postgres on a cold VM boot can leave api up but not yet serving.
waited=0
until curl -sf -m 3 http://127.0.0.1:8000/health >/dev/null 2>&1; do
  if [ "$waited" -ge "$MAX_WAIT_S" ]; then
    log "ensure-open-brain: FAILED -- /health not responding after ${MAX_WAIT_S}s (containers may be up but unhealthy -- check: docker-compose --project-directory $BRAIN_DIR ps)"
    exit 1
  fi
  sleep "$POLL_S"
  waited=$((waited + POLL_S))
done

log "ensure-open-brain: OK -- healthy after ${waited}s total wait"
exit 0
