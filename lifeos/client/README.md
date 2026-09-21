<!--
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->

# LifeOS client kit

A working reference client for Deflector's `X-LifeOS-Sensitivity` contract: twelve
scheduled and on-demand skills, the shared helper library they source, LaunchAgent
templates, and the TELOS schema they read.

Deflector's `lifeos/` package implements the **server** side of that contract. This
directory is the **client** side, so the contract is demonstrated end to end rather
than described.

## What this is built on

| | what | license |
|---|---|---|
| **LifeOS** | Daniel Miessler's personal-AI harness — [github.com/danielmiessler/LifeOS](https://github.com/danielmiessler/LifeOS) (formerly PAI, *Personal AI Infrastructure*) | MIT |
| **Pi** | the agent runtime these skills run under — [github.com/earendil-works/pi](https://github.com/earendil-works/pi), `@earendil-works/pi-coding-agent` | MIT |

Deflector is neither project and ships no code from either. These skills are written
against LifeOS conventions — a TELOS document, `SKILL.md` frontmatter, a skills
directory Pi loads — and are offered as an example of driving a privacy gate from a
LifeOS-style client. Credit for the ideas and structure belongs upstream.

## The privacy stance — read this first

**The machinery is public. The content never is.**

Not one of these skills contains personal data. Every one reads TELOS at runtime via
`lifeos_read_telos()` and injects it into the prompt; none hardcode a bright line, a
health threshold, or a value. That is not incidental tidying — it is why the skills
can be published at all, and it means a skill cannot drift from the document it claims
to be grounded in.

What stays on your machine and is never committed anywhere:

- **TELOS** — `~/.pi/agent/TELOS.md`. `.gitignore` and a pre-commit hook both refuse a
  file with that exact basename, so an accidental `git add` fails loudly.
- **Memories** — whatever your Open Brain holds.
- **Skill output** — reviews and scores are written back to Open Brain, locally.

On top of that, Deflector enforces the sensitivity contract *server-side*: a request
marked `private` never escalates to a cloud model, regardless of what the client asks
for. A client cannot talk its way past it, because the refusal happens after the
request leaves the client's control.

## The skills

| skill | cadence | sensitivity | TELOS-grounded | needs Open Brain |
|---|---|---|---|---|
| `daily-brief` | 07:00 daily | mixed | – | no |
| `presence-check` | 21:30 daily | private | **yes** | yes |
| `health-score` | Sun 17:00 | personal | **yes** | yes |
| `weekly-review-personal` | Sun 18:00 | personal | **yes** | yes |
| `weekly-review-security` | Sun 20:00 | private | no | yes |
| `escalation-audit` | Sun 21:00 | private | no | no |
| `research-digest` | Sat 10:00 | public | no | yes |
| `homelab-drift-log` | Mon 08:00 | private | – | yes |
| `decision-helper` | on demand | private | **yes** | no |
| `future-business-context` | on demand | private | **yes** | yes |
| `dispatch` | on demand | mixed | no | no |
| `brain-capture` | on boundary | mixed | – | no |

`dispatch` is the interesting one: it routes a prompt to whichever agent harness fits
it, and **`private` short-circuits to the local-only harness before any model is
consulted** — a classifier that can be argued out of a privacy decision is not a
control. See its `SKILL.md`.

Skills that need Open Brain and find nothing log a skip and exit 0; they do not fail.

## Requirements

- **Pi** — `@earendil-works/pi-coding-agent`
- **A running Deflector** on `:11500` (this repo), reachable from the client machine
- **Open Brain** or any service exposing `GET /memories`, `POST /memories/search`,
  `POST /memories` — used for recall and for writing results back
- **Python 3** and **curl** — the helpers shell out to both
- Optional: the `jarvis` / `hermes` harness binaries, only if you use `dispatch`

## Setup

**1. Install Pi and place the skills**

```bash
cp -R skills/ ~/.pi/agent/skills/
chmod +x ~/.pi/agent/skills/*/run.sh
```

**2. Point Pi at them**

Merge `pi-settings.fragment.json` into `~/.pi/agent/settings.json`. `skills` is an
array — **append** to it, keep what is already there, or Pi stops seeing the skills it
already loads.

**3. Write your TELOS**

```bash
cp TELOS.template.md ~/.pi/agent/TELOS.md
$EDITOR ~/.pi/agent/TELOS.md
```

The template is the schema only. Skills parse the headings by name, so keep them; the
prose under each is yours. Pay particular attention to `## Non-negotiables` — three
skills score directly against it.

A missing TELOS is a **hard stop**, deliberately: `lifeos_read_telos()` returns
non-zero and skills run under `set -e`, so they abort rather than emit an ungrounded
answer that looks grounded. That exists because the opposite once happened — a
hardcoded path meant TELOS-grounded reviews ran ungrounded for five weeks, and it
surfaced only because one skill wrote *"Cannot score. TELOS not provided in input"*
into its own output, where it sat unread.

**4. Schedule them (optional)**

```bash
sed -e "s|__HOME__|$HOME|g" \
    -e "s|__SKILLS_DIR__|$HOME/.pi/agent/skills|g" \
    -e "s|__AGENTSTOP_URL__|http://<your-deflector>:11500|g" \
    -e "s|__BRAIN_URL__|http://127.0.0.1:8000|g" \
    launchagents/com.example.lifeos.daily-brief.plist \
  > ~/Library/LaunchAgents/com.example.lifeos.daily-brief.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.example.lifeos.daily-brief.plist
```

Use `bootout` + `bootstrap` to reload after editing — **never `launchctl kickstart -k`**,
which restarts the job from launchd's *cached* definition, so your edit lands on disk
and changes nothing observable.

## Running one by hand

```bash
export AGENTSTOP=http://<your-deflector>:11500
export BRAIN_URL=http://127.0.0.1:8000
export LOG_DIR=$HOME/.agentstop/logs

bash ~/.pi/agent/skills/dispatch/run.sh --explain private "audit my open loops"
bash ~/.pi/agent/skills/weekly-review-personal/run.sh
```

> **Export `AGENTSTOP` first.** `_lib/call.sh` defaults it to `http://127.0.0.1:11500`,
> which is right on the Deflector host and wrong everywhere else. It fails *silently*:
> the classifier call gets connection-refused, returns empty, and the unparseable-verdict
> branch downgrades to the local harness. Measured — the same hard prompt routes to
> `hermes` with it set and `jarvis` without, with no error either way. Nothing leaks
> (downgrade is the safe direction), but hard work quietly gets the small model.
> The LaunchAgents set it explicitly; only interactive use is exposed.

## What is deliberately absent

- **TELOS content** — the schema is here, the substance never is.
- **Memories and skill output** — yours, local.
- **`BRAIN_ANON_KEY`** — the deployed plists carry one; it is omitted rather than
  templated, because `_lib/call.sh` documents it as a retained no-op. A placeholder
  would teach you to set something inert.
- **Four vendored skills** (`pptx`, `mermaid`, `graphviz`, `academic-pptx`) — these came
  from elsewhere and carry their own licenses; one is *"© 2025 Anthropic, PBC. All
  rights reserved."* Install them yourself from their sources.
