---
name: brain-capture
description: Categorizes and writes lifeOS insights to Open Brain at natural work boundaries — identity, events, knowledge, or work context.
sensitivity: mixed
cadence: on-boundary
default_model: glm-4.7-flash
escalation_model: lifeos-cloud-reason
claude_model: lifeos-claude
inputs:
  - source: lifeos-state
    paths: [.config/USER/TELOS/, .config/LIFEOS/.claude/]
outputs:
  - target: open-brain
    tags: [capture, lifestate|identity|knowledge]
---

# brain-capture

Triggers at natural boundaries — daily-brief completion, TELOS update, or when Alex says "capture this" during work. Before writing to Open Brain, it categorizes what's worth keeping permanently vs letting expire.

## Persistence Categories — Define What You Want to Keep

| Category | Purpose | Examples | Write to Brain? | Frequency |
|----------|---------|----------|-----------------|-----------|
| **identity-te** | Who you are — TELOS, MODELS, beliefs, worldview | All TELOS.md files, PRINCIPAL_TELOS.md, BELIEFS.md | ✅ Yes | Monthly re-sync only. Not after every change — these rarely shift meaningfully. |
| **lifestate-event** | Meaningful life events that alter conditions | Health changes (5→8 energy), relationship shifts, financial milestones, move/new job/grief | ✅ Yes. Only on meaningful deltas, not noise tracking. | After significant life event or daily-brief detects >2 day trend in health/energy |
| **knowledge** | Proven technical skills, patterns, research findings | Skills created, patterns verified useful, interview insights that generalize | ✅ Yes. Once verified as broadly useful, not raw notes. | One-time write after verification, not during drafting |
| **work-context** | Active project state — what's happening now | Work session outcomes, daily-brief threads, Pulse events | ✅ Yes. High frequency (per-session boundary), but auto-trimmed quarterly | Per session completion or named milestone |
| **personal-private** | Anything explicitly marked by Alex as private IP | Future business plans, unreleased product ideas, Deloitte-adjacent thinking | ❌ **NEVER to brain.** Write to encrypted local stash only. Brain is vector-searchable — privacy leakage risk. | N/A — never escalate |

## Trigger Conditions

Activate brain-capture at these natural boundaries:
1. **daily-brief completes** → "Capture today's state to brain?" with one-liner summary
2. **TELOS file modified** → Auto-detect meaningful change in /health, /finances, /relationships files; prompt update check
3. **Alex says "capture this"** → During any conversation or work session
4. **Weekly review ends** → Bulk sync: "Sync today's state to brain?" before closing the session

## Sensitivity Gating (runs BEFORE any write)

Pre-filter applied to every memory before POSTing to Open Brain:
- Private IPs in `10.10.0.0/16`, `192.168.0.0/16`, `172.16.0.0/12` → strip or refuse
- Internal hostnames (`*.<your-internal-domain>`, lab hostnames) → redact
- Credentials (JWT, PAT shapes, base64 > 20 chars) → refuse entirely
- Unreleased business IP → mark `private`, route to local encrypted stash instead

## Workflow (on trigger)

1. **Scan** — Read active TELOS files under `.config/USER/TELOS/` and `.config/LIFEOS/`.
2. **Categorize** — For each file or concept you want to persist:
   - What category does this belong to? (identity-te, lifestate-event, knowledge, work-context)
   - Is it worth keeping vs ephemeral noise?
   - Any private sensitivity that blocks brain storage?
3. **Chunk** — Limit each memory to 2-5 sentences of core insight. Not the whole file.
4. **Write** — POST to Open Brain with category tag + source origin:

```bash
curl -sX POST "$BRAIN_URL/functions/v1/capture" \
  -H "apikey: $BRAIN_ANON_KEY" \
  -H "Authorization: Bearer $BRAIN_ANON_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "<core insight in 2-3 sentences>",
    "metadata": {
      "source": "brain-capture",
      "category": "identity-te|lifestate-event|knowledge|work-context",
      "tags": ["<3-5 relevant tags>"]
    }
  }'
```

`BRAIN_URL` / `BRAIN_ANON_KEY` come from the OB1 `local-brain-no-mcp` deployment (see `_lib/call.sh`'s defaults, or export them if running standalone).

5. **Verify** — Confirm the memory was stored (check for `{"ok":true,...}` in the response).

## When NOT to write

- Vague or unfinished thoughts
- Purely procedural (checklists, todo items)
- Anything that could compromise privacy if the brain were ever accessed externally
- Duplicate of existing memory with no new insight

## Integration points

This skill is designed to run alongside `daily-brief`, not replace it. It acts as the **archival layer** — daily-brief surfaces today's insights, brain-capture decides what sticks long-term.
