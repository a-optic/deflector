---
name: health-score
sensitivity: personal
cadence: "0 17 * * 0"
default_model: glm-4.7-flash
fallback_model: pi-qwen3.6-128k
telos_grounded: true
inputs:
  - source: open-brain
    query: health/sleep/cardio/weight tagged memories last 7 days
  - source: telos
outputs:
  - target: open-brain
    tags: [health, weekly-score, personal]
---

# health-score

Sun 17:00. Composite weekly health score against whatever body-floor bright lines
your TELOS states under `## Non-negotiables` -- the thresholds are read at runtime, not
hardcoded here. Flags 2-week consecutive drops as incident. Feeds
weekly-review-personal (which runs at 18:00).

Currently reads from `health`-tagged memories (self-logged or apple-health
importer once wired). Handles zero-input gracefully.
