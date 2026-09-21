---
name: weekly-review-personal
sensitivity: personal
cadence: "0 18 * * 0"
default_model: lifeos-cloud-long
fallback_model: pi-qwen3.6-128k
telos_grounded: true
inputs:
  - source: open-brain
    query: memories from last 7 days
  - source: telos
outputs:
  - target: open-brain
    tags: [review, weekly, personal]
---

# weekly-review-personal

Sun 18:00. 7-day personal review grounded in TELOS. Scores week against
non-negotiables + horizons + failure modes.

Escalates: default tier D long-ctx (minimax-m3:cloud) for synthesis breadth.
Prefilter blocks any private-tagged content — those force fallback to tier C
local (pi-qwen3.6-128k).

Prompt injects TELOS as system context. Sensitivity `personal` forbidden by
Telos policy from cloud → **override**: this skill drops any memory tagged
`private` or `homelab` or `future-business` before the cloud call. Weekly
review skill's synthesis is over personal-life memories only.
