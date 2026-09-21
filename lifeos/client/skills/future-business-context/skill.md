---
name: future-business-context
sensitivity: private
cadence: on-demand
default_model: pi-qwen3.6-128k
never_escalates: true
telos_grounded: true
inputs:
  - source: stdin
    query: business idea, positioning question, or decision to frame
  - source: telos
outputs:
  - target: open-brain
    tags: [future-business, private]
---

# future-business-context

On-demand. Frames a business idea/positioning question/decision against TELOS
current-constraint (clarity → concentrate the bet, not fragment). Local-only,
never escalates.

Invoke: `echo "the idea or question" | pi -s future-business-context` or run
run.sh with prompt on stdin.
