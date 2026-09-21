---
name: dispatch
description: Routes a request to whichever harness (OpenJarvis or Hermes) fits it, instead of the one-shot lifeos_call every other skill uses. Called by other skills, not invoked directly.
sensitivity: mixed
cadence: on-demand
default_model: lfm2.5:latest
never_escalates: false
telos_grounded: false
inputs:
  - sensitivity: private|personal|public — declared by the CALLING skill, not inferred
  - prompt: the work to run
outputs:
  - the harness's answer on stdout
  - a dispatch record in ~/.agentstop/logs/dispatch.jsonl
---

# dispatch

Routes a request to the agent harness that suits it, instead of the one-shot
`lifeos_call` every other skill uses.

## Why this exists

`lifeos_call()` sends one prompt and reads one answer. No tools, no iteration.
Work that needs a tool loop had nowhere to go, even though two harnesses were
installed and already pointed at the Deflector.

| | runs on | models | shape |
|---|---|---|---|
| **OpenJarvis** | this host | local only (`lfm2.5`, `qwen3.5`) | orchestrator loop, audit + telemetry |
| **Hermes** | this host | Ollama **or** Claude | sessions, richer toolsets |

Both send inference to the Studio's Deflector, so the harness process is local,
the GPU work lands on the Studio, and the privacy gate sits between.

## The rule

```
private ───────────────────────► jarvis   (local models only)
routine / mechanical ──────────► jarvis
cloud-safe AND genuinely hard ─► hermes   (can reach Claude)
```

**Sensitivity is a lookup, not a judgement.** The calling skill's frontmatter
already declares it. Only *effort* is asked of a model, and that uses tier A
(`lfm2.5:latest`) — which `SYSTEM.md` already designates for "routing gates,
yes/no classification".

## This skill is a privacy control, not just a router

Neither harness can send `X-LifeOS-Sensitivity`, so anything dispatched through
one loses the declared-sensitivity refusal the Deflector's `lifeos_gate()`
applies. The content prefilter (RFC1918, credential shapes) still fires; the
declared half does not.

Routing `private` to OpenJarvis — whose config pins it to local models — means
private work never reaches the cloud-capable harness in the first place. As of
2026-09 the Deflector also no longer honors OpenJarvis's own cloud-burst
escalation (`cloud/jarvis`) at all — that work stays local unconditionally
until a harness actually forwards declared sensitivity — so this isn't the
only backstop, but declared sensitivity still isn't forwarded by either
harness. That is what TELOS's own contract demands of any skill injecting it:

> *All skills injecting TELOS content MUST set `X-LifeOS-Sensitivity: private` —
> cloud escalation forbidden.*

For this reason `private` short-circuits **before** any model is consulted. A
classifier that can be argued out of a privacy decision is not a control.

## Failure behaviour

Falls back rather than failing, so a caller always gets something:

```
hermes fails ──► jarvis ──► plain lifeos_call (tier C local)
```

A **403** from the Deflector is *not* retried on another harness — that is a
deliberate privacy block, and routing around it would defeat the gate.

## Usage

```bash
source "$SKILL_DIR/../_lib/call.sh"
ANSWER=$(lifeos_dispatch "$SENSITIVITY" "$PROMPT")     # auto-select
ANSWER=$(lifeos_dispatch "$SENSITIVITY" "$PROMPT" jarvis)   # force
```
