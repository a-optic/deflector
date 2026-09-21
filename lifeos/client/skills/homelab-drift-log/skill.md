---
name: homelab-drift-log
sensitivity: private
cadence: "0 8 * * 1"
default_model: pi-qwen3.6-128k
never_escalates: true
inputs:
  - source: open-brain
    query: homelab-tagged memories last 7 days
outputs:
  - target: open-brain
    tags: [drift-log, weekly, homelab, private]
---

# homelab-drift-log

Mon 08:00. Homelab-only. Never escalates. Pure local. Tracks config drift,
unresolved issues, planned changes.
