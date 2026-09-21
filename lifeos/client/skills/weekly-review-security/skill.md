---
name: weekly-review-security
sensitivity: private
cadence: "0 20 * * 0"
default_model: pi-qwen3.6-128k
never_escalates: true
telos_grounded: false
inputs:
  - source: open-brain
    query: security/homelab/private memories last 7 days
outputs:
  - target: open-brain
    tags: [review, weekly, security, private]
---

# weekly-review-security

Sun 20:00. Reviews security + homelab + private-tagged memories. NEVER
escalates. Tier C local only. No TELOS injection (adds unnecessary PII to
security context).
