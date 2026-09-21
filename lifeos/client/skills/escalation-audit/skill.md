---
name: escalation-audit
sensitivity: private
cadence: "0 21 * * 0"
default_model: pi-qwen3.6-128k
never_escalates: true
telos_grounded: false
inputs:
  - source: file
    path: ~/.agentstop/logs/lifeos-escalations.jsonl
outputs:
  - target: open-brain
    tags: [audit, weekly, escalations, private]
---

# escalation-audit

Sun 21:00. Meta-skill. Audits the AgentStop escalation log for the week.
Flags drift in sensitivity tagging, patterns of prefilter refusal, cloud escalation rate.
