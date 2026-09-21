#!/usr/bin/env bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# ollama-mem.sh — show or free resident models across BOTH ollama servers.
#
# WHY THIS EXISTS
# ---------------
# Inference is split across two independent ollama servers -- main (:11434,
# routing.main_models) and tasks (:11435, everything else, including
# routing.jarvis_models). They share one pool of physical RAM and have no
# knowledge of each other, so neither can account for what the other has
# resident. Nothing in either process reports the combined figure that actually
# matters, and `ollama ps` only ever shows the one server OLLAMA_HOST points at
# (default :11434) -- so the tasks server's footprint is invisible by default,
# which is exactly the half that gets forgotten.
#
# That blind spot produced a real outage on 2026-09-20. laguna-s-2.1:nvfp4
# (68.4GB, on main) and qwen3-coder:30b (25.6GB, on tasks) were both pinned by
# OLLAMA_KEEP_ALIVE=24h. When tasks then loaded
# orcarouter/Qwen3.8-27B-Uncensored:mlx-8bit (33.8GB) the total reached ~128GB
# on a 137GB box. Ollama's fit estimator is optimistic on unified memory: it
# loaded the third model rather than evicting, swap grew 4GB -> 12GB and hit
# 97% full, and prefill on large prompts stopped finishing. Requests produced
# ZERO bytes, so upstream.local_read_timeout_s (270s) was what surfaced it --
# 104 requests died at ttfb=270.0 before anyone looked at memory. Small prompts
# still worked the whole time (a 33.8GB cold load answered in 19.9s), which is
# what made it read as an intermittent model bug rather than RAM exhaustion.
#
# `keep_alive` is an IDLE timer, not a job timeout -- it resets on every
# request, so it never truncates a long job, and a high value only means a
# finished model squats. Lower it rather than raising it, and use `free` here
# when you deliberately need room for something big.
#
#   ollama-mem.sh                 what is resident on each port, + swap
#   ollama-mem.sh free <model>    unload one model (substring ok, both ports)
#   ollama-mem.sh free-main       unload everything on main
#   ollama-mem.sh free-tasks      unload everything on tasks
#   ollama-mem.sh free-all        unload everything everywhere
#
# Freeing is non-destructive: it drops the model from RAM, not from disk, and
# the next request reloads it (~20s for a 33GB model).

set -uo pipefail

MAIN=${MAIN:-http://127.0.0.1:11434}
TASKS=${TASKS:-http://127.0.0.1:11435}
SERVERS=("main|$MAIN" "tasks|$TASKS")

# Model names resident on one server, one per line. Empty if it is not up.
resident() {
  curl -s --max-time 5 "$1/api/ps" 2>/dev/null | python3 -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: sys.exit()
for m in d.get('models', []): print(m['name'])
" 2>/dev/null
}

# keep_alive:0 is ollama's documented 'unload now'. Worth a generous timeout:
# the call returns only once the runner has actually released the weights.
unload() {
  if curl -s --max-time 60 "$2/api/generate" \
       -d "{\"model\":\"$3\",\"keep_alive\":0}" >/dev/null 2>&1; then
    printf '  freed  %-45s (%s)\n' "$3" "$1"
  else
    printf '  FAILED %-45s (%s)\n' "$3" "$1"
    return 1
  fi
}

show() {
  local grand=0 line size
  for entry in "${SERVERS[@]}"; do
    local name=${entry%%|*} url=${entry#*|}
    printf '── %-5s %s ──\n' "$name" "$url"
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      size=${line##*|}
      case $size in ''|*[!0-9]*) ;; *) grand=$((grand + size)) ;; esac
      printf '%s\n' "${line%|*}"
    done <<< "$(curl -s --max-time 5 "$url/api/ps" 2>/dev/null | python3 -c "
import sys, json
try: d = json.load(sys.stdin)
except Exception: print('  (not responding)|'); sys.exit()
ms = d.get('models', [])
if not ms: print('  (none resident)|'); sys.exit()
for m in ms:
    # expires_at is keep_alive's deadline -- a date far in the future is the
    # squatter signature the header above describes.
    exp = (m.get('expires_at') or '').replace('T', ' ')[:16]
    print(f\"  {m['name']:45} {m['size_vram']/1e9:6.1f} GB  ctx={m.get('context_length')}  until {exp}|{m['size_vram']}\")
" 2>/dev/null)"
  done

  local phys; phys=$(sysctl -n hw.memsize)
  echo
  printf 'resident : %6.1f GB of %.1f GB physical (%.0f%%)\n' \
    "$(bc -l <<< "$grand/1000000000")" \
    "$(bc -l <<< "$phys/1000000000")" \
    "$(bc -l <<< "100*$grand/$phys")"
  printf 'swap     : %s\n' "$(sysctl -n vm.swapusage)"
}

free_servers() {
  local rc=0
  for entry in "$@"; do
    local name=${entry%%|*} url=${entry#*|} m
    while IFS= read -r m; do
      [ -n "$m" ] && { unload "$name" "$url" "$m" || rc=1; }
    done <<< "$(resident "$url")"
  done
  echo
  show
  return $rc
}

case "${1:-show}" in
  show) show ;;
  free)
    want=${2:-}
    if [ -z "$want" ]; then
      echo "usage: ${0##*/} free <model>" >&2; exit 1
    fi
    hit=0 rc=0
    for entry in "${SERVERS[@]}"; do
      name=${entry%%|*} url=${entry#*|}
      while IFS= read -r m; do
        [ -z "$m" ] && continue
        case $m in *"$want"*) hit=1; unload "$name" "$url" "$m" || rc=1 ;; esac
      done <<< "$(resident "$url")"
    done
    if [ "$hit" -eq 0 ]; then
      echo "  no resident model matching '$want'" >&2
      echo; show; exit 1
    fi
    echo; show; exit $rc ;;
  free-main)  free_servers "main|$MAIN" ;;
  free-tasks) free_servers "tasks|$TASKS" ;;
  free-all)   free_servers "${SERVERS[@]}" ;;
  # Both derive from the header rather than line numbers, which silently rot
  # the moment the WHY block above grows a paragraph.
  -h|--help|help)
    awk 'NR>=6 && /^#/ {sub(/^# ?/, ""); print; next} NR>=6 {exit}' "$0" ;;
  *)
    echo "unknown command: $1" >&2
    grep '^#   ollama-mem\.sh' "$0" | sed 's/^# //' >&2
    exit 1 ;;
esac
