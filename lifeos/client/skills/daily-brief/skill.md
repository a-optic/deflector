---
name: daily-brief
sensitivity: mixed
cadence: "0 7 * * *"
default_model: glm-4.7-flash
escalation_model: lifeos-cloud-reason
escalation_threshold: 50
claude_model: lifeos-claude
inputs:
  - source: open-brain
    query: memories from last 24h
outputs:
  - target: open-brain
    tags: [brief, daily]
---

# daily-brief

Runs 07:00 daily. Reads last 24h of Open Brain memories. Produces a compact
morning brief: top themes, unresolved threads, one recommended focus for today.

Tier logic:
- Default: `glm-4.7-flash` (tier B local, direct AgentStop call).
- Escalate to `lifeos-cloud-reason` (nemotron-3-super:cloud) if memories >
  `escalation_threshold` — larger context, better cross-item synthesis.
- Sensitivity `mixed` — the pre-filter in AgentStop blocks any memory with
  private IPs/credentials/lab hostnames from escalation, forcing local fallback.

Runner: `./run.sh` — orchestrates fetch → tier select → call → strip CoT →
write result back to Open Brain tagged `[brief, daily]`.

## Prompt template (fed to model)

```
You are producing a morning brief from the last 24 hours of my memory log.

Format (exact):

## Top of mind
- 2 to 4 dominant threads from yesterday. Cite memory IDs in brackets.

## Unresolved
- Every open loop the log implies. Cite IDs.

## Focus for today
- One concrete recommendation. Cite the memories driving it.

Rules:
- No preamble. Start at ## Top of mind.
- Cite memory IDs like [abc123] using the first 6 chars.
- Do not invent facts absent from the log.
- Terse. Sentences, not paragraphs.

Memories (JSON):
```
