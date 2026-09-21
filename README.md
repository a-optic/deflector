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

> **Interoperates with LifeOS.** The `lifeos-*` lanes and the `X-LifeOS-Sensitivity` header
> exist to serve **LifeOS**, Daniel Miessler's personal-AI harness (formerly *PAI, Personal AI
> Infrastructure*) — MIT. Deflector is a separate project and contains none of its code; it
> implements only the server side of that sensitivity contract. A working reference client
> lives in [`lifeos/client/`](lifeos/client/). Those skills run under **Pi**
> (`@earendil-works/pi-coding-agent`, MIT), the agent runtime LifeOS uses. Credit for both
> belongs upstream:
> - LifeOS: https://github.com/danielmiessler/LifeOS
> - Pi: https://github.com/earendil-works/pi

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

### Declared sensitivity — the `lifeos-*` lanes

`lifeos-cloud-code`, `-reason` and `-long` are **not models**. They are a client contract: a
caller sends `X-LifeOS-Sensitivity: private|personal|public` and requests one of those lane
ids, and `lifeos_gate()` decides what actually happens. `private` never escalates. A
prefilter hit never escalates. Anything else resolves to the real cloud model behind the lane
(`routing.lifeos_prefixes`), falling back to a local model otherwise.

Sensitivity is **declared, not inferred** — and that is the point. The scan above reads what a
request *contains*; it cannot know that a prompt concerns someone's private life, because
nothing in the bytes says so. Only the client knows. So the client asserts it and Deflector
enforces it server-side, where the caller can no longer influence the outcome.

Any client can speak this contract; nothing about it is LifeOS-specific beyond the name. A
complete working implementation — twelve skills, scheduling, and the document schema they
read — is in [`lifeos/client/`](lifeos/client/).

### Routing + Claude
- **Claude override** — only the `X-Deflector-Mode` header can route to Claude (the request body
  is never trusted); opus additionally requires `X-Deflector-Opus: 1`. Claude is served via the
  `claude` CLI, not the Anthropic API (no API key needed on the box).
- Model-name routing across local/cloud tiers for everything else.

### Encrypted troubleshooting capture

Metadata-only logging by default. Optionally, per request, the full exchange is captured and
encrypted so **only the client can read it** — the server keeps no key. Clients enrol themselves
and are identified by their certificate fingerprint, so nothing breaks when a DHCP lease moves.

### Fail-fast efficiency
- **Kill-supervisor** — every stream is watched for token / wall-clock / n-gram-loop / stall
  conditions and aborted the moment it goes bad, so a doomed generation never runs to completion.
  "Stall" means *nothing has happened for `stall_idle_s`* — measured across content, reasoning
  **and streamed tool-call arguments**, since a real agent turn is mostly tool calls and counting
  only prose made healthy 12KB responses look idle. Requests carrying `tools` get a much larger
  budget (`stall_idle_tools_s`): Ollama buffers a whole tool call and emits it as one frame, so
  the wire is genuinely silent for the entire time the model spends writing a large file —
  measured at 31–64s — with no liveness signal to distinguish that from a hang.
- **Concurrency semaphore** — caps in-flight `claude` processes; overflow queues instead of
  erroring, protecting rate limits and RAM.
- **Model cooldowns** — a model the upstream refuses is dropped from the thin-client catalog, so
  it stops being offered. Duration is per status, because the refusals differ: `402`/`429` are
  account state that clears (24h), while `410` is retirement and never clears — a timed cooldown
  there would return a permanently-dead model to the dropdown every day. Inspect or undo with
  `scripts/deflector-cooldowns.py`.
- **Token-preflight downshift** — estimates prompt size and, when it won't fit, routes to a local
  tier *before* the expensive hop instead of rejecting after the fact.
- **Thinking suppression** — chain-of-thought is off by default for the models in
  `routing.think_off_models`. Measured on the local coding lane at a realistic ~44KB agent
  payload, thinking was ~60% of wall time and ~94% of emitted chunks for a one-sentence
  answer — a bad trade on a tool-loop model called dozens of times per task. See
  [Chain-of-thought](#chain-of-thought) to turn it back on.

## Chain-of-thought

Thinking is **suppressed by default** for the models listed in `routing.think_off_models`
(`config.yaml`). The primary mechanism is to ask upstream not to *produce* reasoning, rather
than to filter it afterwards.

There is one exception, and it is not optional: some models emit reasoning into the response
body where `think: false` does not suppress it (`lfm2.5` is the current case — see the note
in `routing.think_off_models`). For those, a **non-streaming** response has everything up to
the final `</think>` removed before it is returned (`postprocess.strip_cot`). **Streamed
responses are forwarded untouched** — stripping across chunk boundaries is not attempted.

Precedence, highest first:

1. **`reasoning_effort` / `think` already in the request body** — a caller that asked for
   something specific is never overridden.
2. **The `X-Deflector-Think` header** — `off` | `low` | `medium` | `high`.
3. **`routing.think_off_models`** — the default-off list.

The field written is **path-dependent**, because Ollama's two APIs disagree: native `/api/*`
honors `think: false`, while the OpenAI-compat `/v1` path silently ignores it and needs
`reasoning_effort: "none"`. Writing the wrong one is a silent no-op, not an error.

### The levels are on/off, not a dial

Ollama accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. Only `none` means off;
**every other value just means on**. Measured on `qwen3.6:35b-a3b`, 3 distinct prompts per level,
reasoning characters emitted:

| `none` | `low` | `high` | `medium` | `max` | `minimal` | `xhigh` |
|---|---|---|---|---|---|---|
| 0 | 3236 | 3531 | 4542 | 5074 | 5674 | 6305 |

Sorted ascending — and note the order is meaningless: `minimal` outproduces `high`. The spread
*within* one level (`low` ranged 1697–7329 across three prompts) is far wider than any gap
*between* levels. Treat it as a switch and ignore the label.

`off` is **not** a valid Ollama value — it returns a hard 400. Deflector rewrites it to `none`
for you, since `off` is the word its own header vocabulary uses, but anything talking to Ollama
directly must send `none`.

```bash
# default: no reasoning emitted
curl -s localhost:11500/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"pi-qwen3.6-128k","stream":true,"messages":[{"role":"user","content":"hi"}]}'

# raise it for one request
curl -s localhost:11500/v1/chat/completions -H 'content-type: application/json' \
  -H 'X-Deflector-Think: high' \
  -d '{"model":"pi-qwen3.6-128k","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

### Where to set it

| scope | how |
|---|---|
| one request | `X-Deflector-Think: off\|low\|medium\|high` header, or `reasoning_effort` in the body |
| a Pi session | `pi --thinking <level>`, or the `/thinking` command |
| Pi, persistently | `thinkingLevel` in `~/.pi/agent/settings.json` |
| a model's default | add/remove it from `routing.think_off_models` in `config.yaml` |

Highest precedence wins, top to bottom in the list under [Chain-of-thought](#chain-of-thought).

### Thin clients

A thin client needs **two** flags before it will send `reasoning_effort`. The Pi client gates on
`options.reasoningEffort && model.reasoning && compat.supportsReasoningEffort`, so missing either
one silently drops the field — no error, just no chain-of-thought ever, which reads as the proxy
stripping it.

- `routing.pi_clients.<name>.compat.supportsReasoningEffort` — must be **true**, or rule 1 above
  is unreachable and the default-off list becomes the *only* behavior.
- `reasoning: true` on each model entry — a per-model capability declaration.

Do **not** blanket-enable `reasoning`. It is a claim about the model, and getting it wrong breaks
things: `llama4:latest` answers normally with no `reasoning_effort` but returns **400** when the
field is sent. Probe a model before marking it.

Clients pull this from `GET /pi/models.json`, so change it here rather than editing the client's
local copy, which gets overwritten on the next refresh.

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

## Troubleshooting logs

Deflector writes **metadata-only** logs by default — timings, sizes, models, routing decisions,
outcomes. Prompt and response text is never recorded unless you explicitly turn on encrypted
capture (below).

Every record carries a shared trace `id`, so one request can be followed across all four files.

```bash
scripts/deflector-logs.py                      # last 20 requests, one line each
scripts/deflector-logs.py --stalls             # requests that never completed
scripts/deflector-logs.py --outcome client_disconnect
scripts/deflector-logs.py --id <trace-id>      # everything known about one request
scripts/deflector-logs.py --summary            # outcome / model / kill rollup
scripts/deflector-logs.py --follow             # tail live
```

From a client machine the same view is one command — `agentstop logs`, with every flag above
passed through. It needs no Keychain, so unlike `agentstop read` it works over ssh.

Each request emits `arrive` → `body_read` → `headers` → `complete`. The `complete` event is the
important one: it records `outcome` (`ok`, `client_disconnect`, `kill:<reason>`, `error:<Type>`),
`ttfb`, `duration`, `bytes_out` and `chunks`. A streaming request with headers but no `complete`
is a stall — that exact signature (200 headers in 0.07s, then 300s of silence and zero bytes) is
what a prior multi-hour outage looked like with no way to see it.

### Retention

Log files are date-stamped in **UTC** and pruned automatically: metadata after **14 days**,
encrypted captures after **7**. The sweep runs in-process at startup and hourly.

> `~/.agentstop/logs` is **shared** with other daemons, and contains this service's own
> launchd `stdout.log`/`stderr.log` — which launchd holds open, so deleting one does *not*
> error, it silently sends all future output to a dead inode. The sweep therefore deletes only
> an **allowlist** of exact filenames Deflector owns. Everything else is left alone regardless
> of age. `stdout.log`/`stderr.log` are deliberately unmanaged; they need
> `newsyslog`/copytruncate, not unlink.

Tunable under `logging:` in `config.yaml`.

### Encrypted capture (opt-in, off by default)

For hard problems you can capture the full request and response — encrypted so that **the server
cannot read what it wrote**. Deflector holds only public certificates; the private key lives on
the client and never leaves it.

Each blob holds three things, which together let you prove the privacy pipeline behaved:
`request_in` (pristine), `request_upstream` (post-redaction — what actually left the box), and
`response_out`. The difference between the first two *is* the redaction audit trail.

**Why this is safe rather than merely obscured.** A client already possesses everything in its own
request and response, so encrypting that back to *that client's* key tells the key holder nothing
new — while removing the server's ability to read it. Naming a key you do not hold gains you
nothing either: the only request affected is your own, which you would be handing to someone else
while losing the ability to read it back. It cannot expose or redirect a third party's data.

#### Set up a client

Three commands, on the machine that will read the captures. No server-side step, nothing to edit,
no restart.

```bash
scripts/install-client.sh --server <deflector-host>   # from a checkout on the CLIENT
agentstop enroll --server <deflector-host>
agentstop backup --bitwarden                          # optional but strongly advised
```

`enroll` generates a key pair, stores the private key in the login Keychain, deletes the key file,
registers the certificate with the server and prints your **capture id**.

Run these in Terminal **at that Mac's own screen**. Writing to the login Keychain needs an
unlocked one, which only a GUI (Aqua) session has; an ssh session fails with `User interaction is
not allowed`. The commands say so rather than failing obscurely.

#### Use it

```bash
curl -XPOST $B/v1/chat/completions -H 'X-Deflector-Capture: <your-capture-id>' ...

agentstop status          # is it working, and who can read my logs
agentstop read            # decrypt and show one, plaintext never touches disk
agentstop backup --verify # restore test: decrypt with the BACKUP key alone
```

#### How a client is identified

By the **SHA-256 of its own certificate** — not its IP. An IP-keyed recipient stops capturing the
moment a DHCP lease moves, silently. A fingerprint survives DHCP, hostname changes, user renames
and reinstalls.

It also removes a whole class of problem instead of managing it: a different certificate *is* a
different fingerprint, so "replace the certificate registered for this identity" cannot be
expressed, and re-enrolling is idempotent — a reinstall needs no approval.

A **MAC address** is recorded alongside, purely as a label so you can recognise a machine in a
listing. The server cannot verify one (it is not in an HTTP request, and ARP only reaches the same
L2 segment), so it is never treated as a credential. The binding to the machine is possession of
the private key, which is a far stronger claim.

`recipients` in `config.yaml` is the older IP-keyed path. It still works for
`X-Deflector-Capture: 1`, so existing setups are untouched.

#### Registering a backup is the one thing that needs authorisation

Backups are **per-client**, and registering one requires decrypting a nonce the server encrypts to
your own certificate — proof you hold the key.

This is deliberately unlike enrolment, for two reasons. A global backup would let whoever set it
read *every* client's captures. And where a hijacked primary is loud — your own decrypts start
failing, and `status` is built to catch it — **a hijacked backup is silent**: an attacker's
certificate added alongside yours receives a readable copy of everything while your decryption
carries on working normally.

`backup_recipients` in `config.yaml` remains as deliberate operator policy. Clients cannot write
to it.

#### Enable it on the server

```yaml
capture:
  enabled: true
  recipients: {}                  # empty: clients enrol themselves
  backup_recipients: []           # optional operator-wide backup certs
```

#### Prove the claim, don't take it on trust

```bash
scripts/capture-selftest.sh       # on the client, in a GUI session
```

One marked request, then four checks in order: the blob exists, the **server** fails to decrypt it
and holds no private key, **you** decrypt it, and the plaintext carries your marker. It also
confirms the blob is addressed to your certificate's serial, so a stale `.crt` is caught rather
than silently producing unreadable captures. Anything short of all four is a fail — a decrypt that
succeeds proves nothing on its own if the server could do it too.

#### Guides

| | |
| --- | --- |
| [macos-client-capture-setup.md](docs/macos-client-capture-setup.md) | click-by-click setup, including the Keychain Access route |
| [reading-captures.md](docs/reading-captures.md) | where every file lives, and the GUI-vs-terminal question |
| [backup-key.md](docs/backup-key.md) | backup keys, Bitwarden, and the recovery drill |
| [verifying-capture-privacy.md](docs/verifying-capture-privacy.md) | twelve tests to audit the privacy claim by hand, and what it does **not** protect |

Notes worth knowing before changing any of this:

- **AES-256-CBC is deliberate, not legacy.** macOS's bundled LibreSSL supports only CBC in CMS,
  so GCM would produce blobs the client cannot open.
- **The Keychain holds the key itself, but `openssl` does the decryption.** Apple's
  `security cms -D` is not used: it failed even on its own `-E` output during testing with
  `-8147` (`SEC_ERROR_NOT_A_RECIPIENT`), which is a *key-lookup* failure — the identity was in a
  temp keychain outside the decoder's search list. It may well work with a properly-imported
  login-keychain identity, but the `openssl` path is already proven against these exact blobs.
- **A locked keychain and a missing item are different failures.** Both surface as a failed
  `find-generic-password`; the fixes are opposite. `decrypt-capture.sh` tells them apart rather
  than sending you to re-import a key that is already there.
- **Backup keys must be generated on the client, never the server.** The server is the one machine
  that must never hold a key able to read captures, and "only briefly" is not a defence: `rm -P` is
  best-effort on SSD, memory can page to swap, and a server compromised in that window yields the
  key *and* its passphrase.
- **Real OpenSSL is required to create a backup key — LibreSSL's `genrsa -aes256` is not enough.**
  It writes traditional PEM, whose KDF is MD5 at a single iteration. Real OpenSSL writes PKCS#8;
  `make-backup-key.sh` pins PBKDF2-HMAC-SHA256 at 600,000. LibreSSL can still *read* the result,
  so recovery works on a stock macOS client. Tooling searches every `PATH` entry rather than
  assuming Homebrew's prefix — `/usr/bin/openssl` normally shadows the real one.
- **The key is stored base64-encoded, and that is load-bearing.**
  `security find-generic-password -w` returns *hex* for any value that is not a plain printable
  string, and a PEM contains newlines — so storing it raw round-trips as hex and silently
  corrupts the key (verified: 3272 bytes in, 6543 bytes of hex out). `import-capture-key.sh`
  handles the encoding and verifies the round trip before you delete the file.
- **`openssl` is invoked by absolute path.** Under a LaunchDaemon's minimal `PATH`, bare
  `openssl` resolves to LibreSSL, which cannot produce what the client needs — silently.
  Deflector refuses to start capture unless the binary identifies as OpenSSL
  (LibreSSL is rejected).
- Capture runs *after* the response completes, on a bounded queue, so it never delays a byte.
  If anything fails, the request is unaffected and the reason is recorded in the metadata log.

## Test

```bash
.venv/bin/python -m pytest -q      # 447 passing
```

The suite runs with or without `private_config.py` (it falls back to placeholders), so CI needs
no secrets. A `conftest.py` fixture redirects `LOG_DIR` for every test and a session-scoped guard
fails the run if anything writes to the real log directory.

## License

Copyright (c) 2026 Alex Vargas.

Privacy Deflector is licensed under the **Mozilla Public License 2.0** (MPL-2.0) — see `LICENSE`. MPL-2.0
is file-level (weak) copyleft: **modifications to Privacy Deflector's own source files must be released
under MPL-2.0**, but you may combine Deflector with other code — including proprietary or
commercial code — in a "Larger Work" without opening that other code. In short: improve the
files, share those improvements; build on top, keep your additions however you like.
