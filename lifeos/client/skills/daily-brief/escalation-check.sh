#!/usr/bin/env bash
# Reads memory JSON array from stdin. Echoes the model name to use.
# Args: <default_model> <escalation_model> <threshold>

set -eo pipefail

DEFAULT="${1:?default model required}"
ESCALATE="${2:?escalation model required}"
THRESHOLD="${3:?threshold required}"

COUNT=$(python3 -c "import sys, json; print(len(json.load(sys.stdin)))")

if [ "$COUNT" -gt "$THRESHOLD" ]; then
  echo "$ESCALATE"
else
  echo "$DEFAULT"
fi
