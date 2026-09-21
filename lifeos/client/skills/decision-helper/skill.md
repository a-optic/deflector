---
name: decision-helper
sensitivity: private
cadence: on-demand
default_model: pi-qwen3.6-128k
never_escalates: true
telos_grounded: true
inputs:
  - source: stdin
    query: the decision to frame
  - source: telos
outputs:
  - target: open-brain
    tags: [decision, private]
---

# decision-helper

On-demand. Applies TELOS decision heuristics to any framed decision.

Heuristics are NOT duplicated here. They are read at runtime from the
`## Decision heuristics` and `## Values` headings of your TELOS and injected into the
prompt. Whatever you write there is what this skill applies -- so it cannot drift from
the document it claims to be grounded in, and this file stays free of the personal
content TELOS holds.

Invoke: `echo "framed decision" | pi -s decision-helper` or with arg.
