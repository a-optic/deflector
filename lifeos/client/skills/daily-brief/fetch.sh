#!/usr/bin/env bash
# Fetch memories from the brain created in the last 24 hours.
# Writes a JSON array to stdout. Exit 0 always; empty array on API error.
#
# Delegates to the centralized lifeos_fetch_recent (see ../_lib/call.sh),
# which paginates OB1's /functions/v1/list and flattens metadata fields
# back to the top level for downstream compatibility.

set -eo pipefail

SKILL_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
source "$SKILL_DIR/../_lib/call.sh"

lifeos_fetch_recent 24 "daily-brief" || echo "[]"
