---
name: presence-check
sensitivity: private
cadence: "30 21 * * *"
default_model: lfm2.5:latest
never_escalates: true
telos_grounded: true
inputs:
  - source: open-brain
    query: last 24h memories
  - source: telos
outputs:
  - target: open-brain
    tags: [presence-check, daily, private]
---

# presence-check

Daily 21:30. Scans the day's memories for drift against the bright lines your TELOS
states under `## Non-negotiables`, plus the early-warning patterns under
`## Failure modes`. Both are read at runtime; neither is written here. One-line alert
if drift, silent if held.

Local tier A (lfm2.5) — classification task, small model sufficient.
