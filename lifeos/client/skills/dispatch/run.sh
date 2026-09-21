#!/usr/bin/env bash
# dispatch — route a request to the harness that suits it.
#
#   run.sh <sensitivity> <prompt...>
#   run.sh --explain <sensitivity> <prompt...>   # print the choice, run nothing
#
# See SKILL.md. `private` never reaches the cloud-capable harness.
set -eo pipefail
SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"
export SKILL_NAME=dispatch

EXPLAIN=0
[ "${1:-}" = "--explain" ] && { EXPLAIN=1; shift; }

SENSITIVITY="${1:-}"; shift || true
PROMPT="$*"

if [ -z "$SENSITIVITY" ] || [ -z "$PROMPT" ]; then
  echo "usage: run.sh [--explain] <private|personal|public> <prompt...>" >&2
  exit 2
fi

case "$SENSITIVITY" in
  private|personal|public|mixed) ;;
  *) echo "dispatch: unknown sensitivity '$SENSITIVITY' -- treating as private" >&2
     SENSITIVITY=private ;;
esac

if [ "$EXPLAIN" = "1" ]; then
  lifeos_pick_harness "$SENSITIVITY" "$PROMPT"
  exit 0
fi

lifeos_dispatch "$SENSITIVITY" "$PROMPT"
