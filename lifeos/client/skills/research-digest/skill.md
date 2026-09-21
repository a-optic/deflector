---
name: research-digest
sensitivity: public
cadence: "0 10 * * 6"
default_model: lifeos-cloud-reason
fallback_model: pi-qwen3.6-128k
telos_grounded: false
inputs:
  - source: open-brain
    query: research/learning tagged memories last 7 days
outputs:
  - target: open-brain
    tags: [digest, weekly, research, public]
---

# research-digest

Sat 10:00. Digests research/learning-tagged memories into publishable summary.
Cloud reason tier (nemotron-3-super:cloud). Public sensitivity.
