# Privacy Deflector

A privacy-and-efficiency gate for LLM traffic. Privacy Deflector sits in front of your model
backends and speaks the Ollama HTTP API, so any Ollama-compatible client can point at it
unchanged. Every request passes two gates before it ever reaches a model:

> **Naming note:** the project's display name is "Privacy Deflector." Code-level identifiers
> — the `X-Deflector-Mode` header, service labels, module/package names — remain `deflector`
> unchanged; only docs and branding use the fuller name.

1. **Privacy gate** — a deterministic scan blocks, reroutes, or redacts sensitive content
   before it can leave for a cloud model or Claude.
2. **Fail-fast efficiency** — Deflector predicts and stops wasteful work *before* it burns
   compute: it aborts generation that has gone runaway, looping, or stalled, and it downshifts
   an oversized prompt away from a doomed, expensive cloud hop instead of paying for it and
   failing. On local/laptop hardware that saved energy is the difference between a warm fan and
   a dead battery.

> **Attribution.** Privacy Deflector is an **independent implementation** inspired by the **AgentStop**
> concept from **Brave** (early-termination of agent work that will fail, to save resources). It
> shares none of Brave's code — it was built from scratch — and reuses none of their name for the
> project. Credit to the original idea and research:
> - Blog: https://brave.com/blog/agentstop/
> - Reference implementation: https://github.com/brave-experiments/AgentStop
> - Paper: https://arxiv.org/pdf/2605.15206

## What it does

### Privacy gate (Rule 1, absolute precedence)
A tiered scan runs before any dispatch:
- Tier A: operator static identity (exact values) → block / reroute / redact.
- Tier B: secrets and high-entropy blobs → hard block.
- Tier C: best-effort PII (Presidio, optional) → redact.
- Tier D: local-LLM rewrite (optional, off by default).

Outcome precedence is `block > reroute > redact > proceed`. A separate deterministic pre-filter
(`lifeos/prefilter.py`) flags private-network signals — RFC1918 IPs, your internal domain's
`lab`/`vpn`/`homelab`/`internal` subdomains, credential shapes — to keep sensitive prompts off
cloud tiers.

### Routing + Claude
- **Claude override** — only the `X-Deflector-Mode` header can route to Claude (the request body
  is never trusted); opus additionally requires `X-Deflector-Opus: 1`. Claude is served via the
  `claude` CLI, not the Anthropic API (no API key needed on the box).
- Model-name routing across local/cloud tiers for everything else.

### Fail-fast efficiency
- **Kill-supervisor** — every stream is watched for token / wall-clock / n-gram-loop / stall
  conditions and aborted the moment it goes bad, so a doomed generation never runs to completion.
- **Concurrency semaphore** — caps in-flight `claude` processes; overflow queues instead of
  erroring, protecting rate limits and RAM.
- **Token-preflight downshift** — estimates prompt size and, when it won't fit, routes to a local
  tier *before* the expensive hop instead of rejecting after the fact.

## Requirements

- Python 3.14, [`uv`](https://docs.astral.sh/uv/) (the virtualenv is uv-managed; use `uv pip`,
  not `pip`).
- A model backend reachable over the Ollama API (e.g. one or more `ollama serve` instances).
- Optional: the `claude` CLI (Claude Code) authenticated, for the Claude route.
- Optional: `presidio-analyzer` + a spaCy model, for Tier C PII redaction.

## Install

```bash
git clone <your-fork-url> deflector && cd deflector

# 1) REQUIRED — create your private constants (never committed):
cp private_config.example.py private_config.py
$EDITOR private_config.py          # set INTERNAL_DOMAIN, OPERATOR_NAMES, PRIVATE_TEST_IP

# 2) install the pre-commit guard that keeps private_config.py + your PII out of git:
git config core.hooksPath scripts/hooks

# 3) create the venv and install deps (uv, Python 3.14):
uv venv --python 3.14 .venv
uv pip install -p .venv/bin/python fastapi uvicorn httpx pyyaml tiktoken
# optional Tier C: uv pip install -p .venv/bin/python presidio-analyzer && \
#   .venv/bin/python -m spacy download en_core_web_lg
```

`private_config.py` holds the operator-specific identifiers Deflector needs but must never
publish (your internal domain drives the pre-filter's hostname detection; the name list and test
IP are used by the test suite). It is gitignored, and the pre-commit hook refuses to commit it or
any staged content containing your real identifiers. If the file is absent (a fresh clone),
neutral placeholders from `private_ref.py` are used, so the code still imports and tests still
pass — you just get example values until you create your own.

Runtime privacy config lives **outside** the repo in `~/.agentstop/` (`provider-trust.yaml`,
`redact-list.yaml`); routing/thresholds are in `config.yaml`.

> Note: the client header is `X-Deflector-Mode` (`X-Deflector-Opus` for opus). The legacy
> `X-AgentStop-Mode` / `X-AgentStop-Opus` headers are still accepted for backward compatibility.
> A few operator-local paths still use `agentstop` (the `~/.agentstop/` config dir); these are not
> part of the public API.

## Run

```bash
.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 11500
```

Point any Ollama client at `http://<host>:11500`. Quick check:

```bash
B=http://127.0.0.1:11500
# privacy block (fake SSN) -> 403
curl -s -o /dev/null -w "%{http_code}\n" -XPOST $B/api/chat -H 'content-type: application/json' \
  -d '{"model":"your-local-model","stream":false,"messages":[{"role":"user","content":"ssn 123-45-6789"}]}'
# Claude route (needs the claude CLI) -> streams "pong"
curl -s -N -XPOST $B/api/chat -H 'content-type: application/json' -H 'X-Deflector-Mode: claude-haiku-4-5' \
  -d '{"model":"x","stream":true,"messages":[{"role":"user","content":"reply one word: pong"}]}' | tail -1
```

## Test

```bash
.venv/bin/python -m pytest -q      # 48 passing
```

The suite runs with or without `private_config.py` (it falls back to placeholders), so CI needs
no secrets.

## License

Copyright (c) 2026 Alex Vargas.

Privacy Deflector is licensed under the **Mozilla Public License 2.0** (MPL-2.0) — see `LICENSE`. MPL-2.0
is file-level (weak) copyleft: **modifications to Privacy Deflector's own source files must be released
under MPL-2.0**, but you may combine Deflector with other code — including proprietary or
commercial code — in a "Larger Work" without opening that other code. In short: improve the
files, share those improvements; build on top, keep your additions however you like.
