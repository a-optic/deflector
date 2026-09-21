<!--
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->

# Using the stack — how to actually call LifeOS, Hermes, OpenJarvis and Pi

Every other document here describes how the stack is *built*. This one is about
invoking it. If you know what you want to run but not what to type, start here.

## The one thing to get straight first

**These four are peers, not a hierarchy.** Pi, Hermes, OpenJarvis and the LifeOS
skills each call the Deflector on `:11500` independently — none of them proxies
another. `scripts/stack-check.sh` verifies exactly that.

LifeOS is an orchestrator only in the sense that it owns TELOS, the skill library
and the verification criteria, and that **work LifeOS itself initiates** can be
handed to a harness via `lifeos_dispatch`. It is not a layer that Pi's traffic
passes through.

```
                      ┌──────────────────────────────────┐
   M4 mini            │   Mac Studio  <STUDIO_LAN_IP>    │
   ─────────          │   ────────────────────────       │
   Pi            ──┐  │                                  │
   Hermes        ──┼──┼──►  Deflector :11500  ──┬──► ollama-main  :11434
   OpenJarvis    ──┤  │     routing + privacy   └──► ollama-tasks :11435
   LifeOS skills ──┘  │                                  │
                      │     Open Brain :8000             │  (mini reaches it
                      └──────────────────────────────────┘   over an SSH tunnel)
```

The harness processes and the skills run **on the mini**. The GPU work always
lands on the **Studio**. The privacy gate sits between the two.

## Before you run anything by hand on the mini

```bash
export AGENTSTOP=http://<STUDIO_LAN_IP>:11500
```

**Do this first, every time.** `_lib/call.sh` defaults `AGENTSTOP` to
`http://127.0.0.1:11500`, which is correct on the Studio and wrong on the mini —
nothing listens on the mini's loopback. The scheduled LaunchAgents set it
explicitly, so cron jobs are unaffected; only interactive shell use is exposed.

It fails *silently*, which is what makes it worth a heading. Measured:

```
$ export AGENTSTOP=...      → dispatch --explain public "<hard prompt>"   → hermes
$ unset AGENTSTOP           → dispatch --explain public "<same prompt>"   → jarvis
```

The classifier call gets connection-refused, returns empty, and the
unparseable-verdict branch downgrades to jarvis. That is the *safe* direction —
TELOS's "when in doubt, downgrade one tier" — so nothing leaks. But every hard
prompt quietly gets the small local harness and nothing says so.

## Path 1 — one-shot inference (no tools)

One prompt, one answer. What most scheduled skills use.

```bash
source ~/.pi/agent/skills/_lib/call.sh
lifeos_call "pi-qwen3.6-128k" personal "Summarise this week" ""
```

Arguments are `<model> <sensitivity> <prompt> <memories-json>`. Sends
`X-LifeOS-Sensitivity`, so the Deflector's declared-sensitivity gate applies —
this is the **only** path where it does (see Path 2).

Don't want to pick a model? `lifeos_pick_tier_c <content> [quality|speed|long-ctx]`
chooses one. It deliberately never returns a 66 GB+ model; see "Big models" below.

## Path 2 — agentic work via dispatch (tool loops)

`lifeos_call` cannot use tools. When work needs a tool loop, dispatch it:

```bash
# see the routing decision, run nothing
bash ~/.pi/agent/skills/dispatch/run.sh --explain private "audit my open loops"

# actually run it
bash ~/.pi/agent/skills/dispatch/run.sh private "audit my open loops"

# or from inside a skill
source "$SKILL_DIR/../_lib/call.sh"
ANSWER=$(lifeos_dispatch "$SENSITIVITY" "$PROMPT")        # auto-select
ANSWER=$(lifeos_dispatch "$SENSITIVITY" "$PROMPT" jarvis) # force a harness
```

How it chooses:

```
private ───────────────────────► jarvis   (local models only)
routine / mechanical ──────────► jarvis
cloud-safe AND genuinely hard ─► hermes   (can reach Claude)
```

**`private` short-circuits before any model is consulted.** Sensitivity is a
lookup from the calling skill's frontmatter, not a judgement — only *effort* is
asked of a model, and that uses tier A (`lfm2.5`).

> **Why that matters:** neither harness forwards `X-LifeOS-Sensitivity`, so work
> dispatched through one loses the *declared* half of the privacy gate. The
> content prefilter (RFC1918, credential shapes) still fires. Routing `private`
> to OpenJarvis — pinned to local models — is what keeps private work off a
> cloud-capable harness. A classifier that can be argued out of a privacy
> decision is not a control.

Falls back `hermes → jarvis → lifeos_call` so a caller always gets something. A
**403 is never retried on another harness** — that is a deliberate block, and
routing around it would defeat the gate. Decisions land in
`~/.agentstop/logs/dispatch.jsonl`.

## Path 3 — a harness directly

```bash
jarvis ask "respond exactly: ok"        # OpenJarvis — local models only
hermes chat -q "explain this traceback" # Hermes — Ollama or Claude
```

Both are on the mini at `~/.local/bin/` and both point at the Studio's Deflector.

Their model tiers, and why:

| harness | tier 1 | tier 2 | notes |
|---|---|---|---|
| **OpenJarvis** | `lfm2.5:latest` | `qwen3.5:9b` | 1.0.3 has exactly two slots. These are the quality floor for **all private work**. |
| **Hermes** | `hermes-qwen3.6-64k` | escalates under pressure | cloud-capable — never receives `private` work via dispatch |

`qwen3-coder:30b` is deliberately *not* an OpenJarvis tier despite winning
compute efficiency by 26×: its hallucination resistance measured 0.33, the worst
of any model tested. Private work here is factual synthesis about your own life,
where invention is the worst available failure.

## Path 4 — Pi, interactively

Pi loads skills from both directories now:

- `~/.pi/skills/` — 54 general skills
- `~/.pi/agent/skills/` — the LifeOS skills, including `/dispatch`

So `/dispatch` and the LifeOS skills are available as slash commands in a Pi
session. Pi's default model is `pi-qwen3.6-128k` (23.9 GB, 192K verified context —
a needle was retrieved at 182,542 real tokens).

To use a different model, pick from the dropdown Pi pulls from
`GET /pi/models.json` — that endpoint is the single source of truth, so a model
added to `routing.pi_clients` in `config.yaml` appears on every thin client at
its next refresh with no per-machine push.

## Big models — explicit request only

Three models are routable but kept **out** of the dropdown on purpose, so nothing
loads them casually:

| model | size | ask for it when |
|---|---|---|
| `laguna-s-2.1:nvfp4` | 66.2 GB | genuine long-document retrieval (fastest measured: 287s on a 567,291-char prompt) |
| `llama4:latest` | 67.4 GB | rarely — demoted; vision measured 0/3 and laguna beat it at equal RAM |
| `qwen3.8-flash-next:125b-mlx` | 105.5 GB | deliberate solo use; it monopolises the box |

`orcarouter/Qwen3.8-27B-Uncensored:mlx-8bit` (33.8 GB) is research/red-team only
and sits on the **tasks** lane alongside `qwen3-coder:30b`.

**Before loading any of these, check combined residency.** The two ollama servers
share one pool of RAM and cannot see each other:

```bash
./scripts/ollama-mem.sh              # combined residency + swap, both servers
./scripts/ollama-mem.sh free laguna  # hand the RAM back when you're done
```

This is not theoretical. On 2026-09-20, laguna (66.2 GB, manually selected and
then pinned by a 24h keep-alive) plus a 33.8 GB model on the tasks lane reached
129 GB on a 137 GB box. The VM compressor hit 100% of its segment limit, 100
swapfiles were created, `watchdogd` was starved for 94 seconds and the kernel
watchdog panicked the machine. `keep_alive` is now 1h and the main lane defaults
to a 23.9 GB model, but two concurrent large picks can still overcommit.

## Checking it all works

```bash
./scripts/stack-check.sh    # every consumer reaches inference + memory
./scripts/ollama-mem.sh     # what is resident right now, across both servers
./scripts/deflector-logs.py # last 20 requests, one line each
./scripts/deflector-logs.py --stalls --summary
```

## Where things live

| what | where |
|---|---|
| skill library + `_lib/call.sh` | `~/.pi/agent/skills/` (mini) |
| harness binaries | `~/.local/bin/{jarvis,hermes}` (mini) |
| scheduled LifeOS jobs | `~/Library/LaunchAgents/com.<org>.lifeos.*` (mini) |
| dispatch decisions | `~/.agentstop/logs/dispatch.jsonl` |
| Deflector logs | `~/.agentstop/logs/` (Studio) |
| model roster + routing | `config.yaml` → `routing` (Studio) |

> `~/.pi/LIFEOS/` on the mini is a **different** system — the bun/TypeScript PAI
> runtime, driven by the `com.lifeos.*` LaunchAgents. It is unrelated to the
> shell skill library described here, despite the shared name.
