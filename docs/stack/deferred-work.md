<!--
This Source Code Form is subject to the terms of the Mozilla Public
License, v. 2.0. If a copy of the MPL was not distributed with this
file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->

# Deferred work

Decisions deliberately left in a simpler state than they could be, with enough
context to pick them up cold in a later session. Each entry says what was done,
why it was done that way, and what a better version would need.

This is not a bug list. Everything here works; these are known trade-offs.

---

## `lifeos_pick_tier_c` is RAM-blind by design

**Status:** deliberate, 2026-09-21. Revisit only if automatic model selection
needs to adapt to load again.

**What it does now.** `lifeos_pick_tier_c` (`docs/stack/lifeos-call.sh`) returns
exactly two models: `qwen3.6:35b-a3b` for `pref=speed` on short content, and
`pi-qwen3.6-128k` for everything else. It no longer reads free RAM at all.

**What it used to do.** For `pref=quality` with content under 16k tokens, it
returned `llama4:latest` (67.4 GB) whenever `lifeos_free_gb` reported ≥ 70 GB.
That branch was removed for two reasons:

1. **It auto-selected a demoted model.** `llama4` is kept routable in
   `routing.main_models` but deliberately hidden from the `pi_clients` dropdown —
   its vision measured 0/3 and laguna beat it on the long-context retrieval it
   existed for (287s vs 383s at equal RAM). The picker was choosing automatically
   what the operator had decided against choosing by hand.
2. **The gate was inverted.** A 67.4 GB load triggered by a *free-RAM test* means
   the more headroom the box had, the more of it would be consumed. And the
   2026-09-20 daemon retune (`keep_alive` 24h → 1h, `MAX_LOADED_MODELS` 2 → 1 on
   main) moved `free_gb >= 70` from rare to common — so a fix for memory
   exhaustion would have *increased* how often a 67 GB model loaded unasked.

**What the current state costs.** Automatic model selection no longer adapts to a
loaded box in either direction. It will pick `pi-qwen3.6-128k` (23.9 GB) whether
the machine is idle or nearly full. That is safe but not smart: on a genuinely
idle box there is headroom for a better model that nothing will ever reach for.

**What a better version needs.** If this is made adaptive again, the input must be
**combined residency across both ollama servers**, not free RAM on one host.

```bash
./scripts/ollama-mem.sh     # the combined figure, main + tasks, plus swap
```

The two servers (`:11434` main, `:11435` tasks) share one pool of physical RAM
and have no knowledge of each other. `lifeos_free_gb` uses `vm_stat`, which
reports what is free *now* — it cannot tell you that the other server is holding
68 GB that it intends to keep, nor that a 24h keep-alive means that memory is not
coming back. That blindness is the specific mechanism behind the 2026-09-20
kernel panic: main held laguna at 66.2 GB while tasks independently loaded a
33.8 GB model, reaching 129 GB on a 137 GB box.

A correct version would:

- read residency from both `/api/ps` endpoints (as `ollama-mem.sh` does), not `vm_stat`
- subtract what is *pinned* (check `expires_at`, not just current size)
- reserve headroom for the OS rather than spending to the last byte
- and still never select a model the dropdown hides — demotion is an operator
  decision, and an automatic path should not quietly overrule it

`lifeos_free_gb` is retained as a public helper for skills that want it, but it
no longer gates any model choice.
