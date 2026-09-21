# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import base64
import asyncio
import collections
import copy
import hashlib
import json
import os
import pathlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from budget import estimate_prompt_tokens, over_budget
from lifeos.compact import compact_messages
from lifeos.postprocess import strip_cot
from lifeos.prefilter import (
    has_block as _lifeos_has_block,
    redact as _lifeos_redact,
    scan as _lifeos_scan,
    summarize as _lifeos_summarize,
)
from privacy import Block, Redact, Reroute, privacy_evaluate
from privacy.config import LOCAL, get_config
from privacy.engine import _text_slots as _redact_text_slots
from privacy.rehydrate import RehydrateStream, rehydrate_complete
import model_cooldown
import open_brain
from private_ref import PI_HOST
import capture
import retention
import enrollment
import tracing
from providers.claude_cli import (
    MODEL_ALIAS,
    build_prompt,
    build_system,
    stream_claude,
)

CFG_PATH = pathlib.Path(__file__).parent / "config.yaml"
with open(CFG_PATH) as f:
    CFG = yaml.safe_load(f)

LOG_DIR = pathlib.Path(os.path.expanduser(CFG["logging"]["log_dir"]))
LOG_DIR.mkdir(parents=True, exist_ok=True)

TH = CFG["thresholds"]
RT = CFG["routing"]

# Bucket B (spec 03 §3a): cap concurrent `claude` CLI processes; overflow awaits here.
# Semaphore(N) construction is loop-agnostic in py3.10+; it binds to the running loop
# on first await under uvicorn — safe at module load.
_MAX_CLAUDE = (CFG.get("claude") or {}).get("max_concurrent_claude", 3)
_claude_sem = asyncio.Semaphore(_MAX_CLAUDE)

# Rule 2 (v4.0 §2): only the X-Deflector-Mode header routes to Claude, never the body
# (legacy X-AgentStop-Mode still accepted). Header must exactly equal an allowlisted
# model; opus additionally needs the opt-in.
CLAUDE_ALLOWLIST = {"claude-haiku-4-5", "claude-sonnet-5"}
CLAUDE_OPUS = "claude-opus-4-8"
ANTHROPIC = "anthropic"
OLLAMA_CLOUD = "ollama-cloud"

# Destination label for an upstream with no entry in upstream.provider_labels.
# Deliberately NOT in either trust class in provider-trust.yaml, which lands it
# on exactly the safe combination privacy/engine.py already implements:
# is_cloud() true (so Tier B secret blocking and every redaction tier run) and
# is_restricted() false (so it does not spuriously trigger the Anthropic
# reroute). Nothing in the engine needed changing -- the safe path already
# existed, it just was not being reached.
UNKNOWN_REMOTE = "unknown-remote"

PROVIDER_LABELS: dict[str, str] = dict(CFG["upstream"].get("provider_labels") or {})


def _provider_for(client_key: str) -> str:
    """Privacy destination label for an upstream key.

    Fails CLOSED. This used to be `OLLAMA_CLOUD if key == "cloud" else LOCAL`,
    which answered "is this the one cloud key I know about?" rather than "is
    this off-box" -- so any fourth upstream would have been classified as this
    machine and had its prompts sent out unredacted, with no error and nothing
    in the logs. An unrecognised key is now remote.
    """
    return PROVIDER_LABELS.get(client_key, UNKNOWN_REMOTE)

# Upstream statuses meaning "this ACCOUNT may not use this tier", as opposed to
# "this request was bad". Observed from ollama.com:
#   429 -- "you (<account>) have reached your session usage limit"
#   402 -- "this model requires a subscription or extra usage"
# Neither is fixable by retrying or reshaping the request, and both are survivable
# by answering from the local tier instead of ending the session. Scoped tightly on
# purpose: a 400/404/500 is about THIS request or THIS model, and quietly answering
# those from a different model would hide a real bug behind a plausible answer.
_TIER_REFUSAL_STATUSES = frozenset({402, 429})

# Thinking-suppression policy (see routing.think_off_models in config.yaml for
# the measurements and the path-dependence that motivate this).
THINK_OFF_MODELS = frozenset(RT.get("think_off_models") or [])
THINK_HEADERS = ("x-deflector-think", "x-agentstop-think")
THINK_OFF_WORDS = {"off", "none", "false", "0", "no"}

# Cloud gets connect/read fail-fast bounds on top of the overall ceiling: a cloud
# model that's simply down sends nothing back at all, and the plain overall
# timeout used for main/tasks would otherwise leave that hanging for the full
# duration instead of surfacing the dead endpoint quickly (see config.yaml).
_cloud_timeout = httpx.Timeout(
    CFG["upstream"]["cloud_timeout_s"],
    connect=CFG["upstream"]["cloud_connect_timeout_s"],
    read=CFG["upstream"]["cloud_read_timeout_s"],
)

# Local upstreams get a read bound too. The supervisor's idle-gap stall check
# runs inside the chunk loop, so it cannot see a stream that has gone entirely
# silent -- that case is the read timeout's job. Left generous because this also
# covers prefill on a large payload (see config.yaml).
_local_timeout = httpx.Timeout(
    CFG["upstream"]["timeout_s"],
    read=CFG["upstream"]["local_read_timeout_s"],
)

_clients = {
    "main":  httpx.AsyncClient(base_url=CFG["upstream"]["main"],
                               timeout=_local_timeout),
    "tasks": httpx.AsyncClient(base_url=CFG["upstream"]["tasks"],
                               timeout=_local_timeout),
    "cloud": httpx.AsyncClient(base_url=CFG["upstream"]["cloud"],
                               timeout=_cloud_timeout),
}

app = FastAPI(title="Deflector")

_lock = threading.Lock()
_active: dict[str, int] = {}


def _inc(model: str) -> None:
    with _lock:
        _active[model] = _active.get(model, 0) + 1


def _dec(model: str) -> None:
    with _lock:
        _active[model] = max(0, _active.get(model, 0) - 1)


def _hermes_active() -> bool:
    # Currently unused: its only caller was resolve_routing's cloud/jarvis
    # branch, retired 2026-09 (see that function). Left in place, not removed,
    # since `_active` is still populated elsewhere (_inc/_dec) and a future
    # re-enable of cloud/jarvis escalation would want this check back.
    with _lock:
        return _active.get(RT["hermes_model"], 0) > 0


def _jarvis_concurrent() -> int:
    # Currently unused -- see _hermes_active's comment above.
    with _lock:
        return sum(_active.get(m, 0) for m in RT["jarvis_models"])


def _read_pressure() -> dict:
    try:
        return json.loads(pathlib.Path(RT["pressure_flag"]).read_text())
    except Exception:
        return {"use_cloud_tasks": False, "pressure_pct": 0}


# --- Repeat guard --------------------------------------------------------------
# Catches a client stuck retrying the same ask (e.g. a tool call that keeps
# failing) across separate HTTP requests -- the per-stream kill-supervisor in
# _supervised_stream can't see this, since each retry is its own request/response.
# Keyed per originating model, not per session: this box serves one client at a
# time in practice, and the model is the cheapest stable key we have without a
# conversation id. State is in-memory and resets on restart -- that's fine, the
# failure mode it guards against reoccurs within seconds, not across restarts.
#
# Fingerprints the FULL conversation (via _extract_body_text, same reader used for
# the token budget -- includes tool_calls arguments), not just the trailing user
# turn. An agentic client like Pi calls the model repeatedly per single human
# instruction (check build -> read file -> propose edit -> rebuild -> verify...),
# and the last user-authored message stays fixed at e.g. "continue" the entire
# time even though real, distinct work is happening turn to turn -- fingerprinting
# only that turn flagged every ordinary multi-step tool loop as a stuck repeat.
# Fingerprinting the whole body instead only matches when NOTHING changed at all
# between calls: no new tool call, no new tool result, no new assistant turn --
# which is what a genuinely dead-end retry loop looks like, and what an active
# agentic loop never looks like.
#
# Two more guards against false-triggering on a client's own retry-with-backoff
# after a transient failure (which legitimately resends the identical prompt a
# few times within seconds): REPEAT_WINDOW_S requires the repeat to have persisted
# a while, not just repeated a few times fast, and the response is a plain 400
# rather than 429 -- 429 is conventionally "back off and retry", so returning it
# here made well-behaved retry logic retry *the block itself*, compounding the
# count (observed: 3->4->5->6 inside 14s, all backoff-spaced). The incident this
# guards against ran for ~23 hours; a minute of persistence is nowhere close.
REPEAT_THRESHOLD = 4
REPEAT_WINDOW_S = 60
CONFIRM_HEADERS = ("x-deflector-confirm-repeat", "x-agentstop-confirm-repeat")

_repeat_lock = threading.Lock()
_repeat_state: dict[str, dict] = {}  # model -> {"fp": str, "count": int, "first_ts": float}


def _repeat_status(key: str, body: dict) -> tuple[int, float]:
    """(consecutive count, seconds since the first of this run) for `key`,
    updating state as a side effect. Both zero if there's no body text to key on."""
    text = _extract_body_text(body)
    if not text:
        return 0, 0.0
    fp = hashlib.sha256(text.encode()).hexdigest()
    now = time.time()
    with _repeat_lock:
        st = _repeat_state.get(key)
        if st is None or st["fp"] != fp:
            st = {"fp": fp, "count": 1, "first_ts": now}
        else:
            st["count"] += 1
        _repeat_state[key] = st
        return st["count"], now - st["first_ts"]


def _repeat_reset(key: str) -> None:
    with _repeat_lock:
        _repeat_state.pop(key, None)


# --- In-flight request dedup -----------------------------------------------------
# A slow local model (e.g. pi-qwen3.6-128k near its context ceiling) can sit quiet
# for a long prefill before the first token; a caller that gives up early and resends
# the identical prompt doesn't cancel the first attempt -- it queues a second,
# independent generation on the same hardware, which only makes both slower and
# invites a third resend. Coalesce: if a request identical to one already in flight
# for the same model shows up, attach to its output instead of starting another.
#
# Keyed on the pristine incoming conversation, computed once before any mutation
# (routing, redaction) touches `body` -- Tier A/C redaction injects a fresh random
# nonce into placeholders on every pass (privacy/engine.py), so fingerprinting
# post-redaction would never match twice even for the exact same original ask.
#
# Fingerprints the full body (_extract_body_text), not just the trailing user
# turn, for the same reason the repeat guard does below: an agentic client calls
# the model many times per single human instruction, with a fixed trailing user
# turn but a genuinely different tool-call/result history each call. Keying on
# just that turn would coalesce two DISTINCT steps of an active tool loop into
# one, handing the second call back a stale result meant for the first.
@dataclass
class _InFlight:
    chunks: list[bytes] = field(default_factory=list)
    done: bool = False
    result: tuple[dict, int] | None = None  # (resp_json, status_code) for non-streaming
    error: BaseException | None = None  # set instead of `result` if the primary raised
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


_inflight_lock = threading.Lock()
_inflight: dict[tuple[str, str], _InFlight] = {}


def _inflight_key(model: str, body: dict) -> tuple[str, str] | None:
    text = _extract_body_text(body)
    if not text:
        return None
    return (model, hashlib.sha256(text.encode()).hexdigest())


def _inflight_join(key: tuple[str, str]) -> tuple[_InFlight, bool]:
    """(entry, is_primary). Primary drives `entry`; followers just read it."""
    with _inflight_lock:
        existing = _inflight.get(key)
        if existing is not None:
            return existing, False
        entry = _InFlight()
        _inflight[key] = entry
        return entry, True


def _inflight_release(key: tuple[str, str], entry: _InFlight) -> None:
    with _inflight_lock:
        if _inflight.get(key) is entry:
            del _inflight[key]


async def _inflight_follow_stream(entry: _InFlight) -> AsyncIterator[bytes]:
    idx = 0
    while True:
        async with entry.condition:
            while idx >= len(entry.chunks) and not entry.done:
                await entry.condition.wait()
            pending = entry.chunks[idx:]
            idx = len(entry.chunks)
            done = entry.done
        for c in pending:
            yield c
        if done:
            break


async def _mark_stream_done(key: tuple[str, str], entry: _InFlight) -> None:
    """Mark-done, notify, and release as one atomic shielded unit -- see the
    call site's comment for why the release has to be bundled in here too."""
    async with entry.condition:
        entry.done = True
        entry.condition.notify_all()
    _inflight_release(key, entry)


async def _dedup_stream(key: tuple[str, str] | None, inner: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Wrap a primary's output generator so followers can attach to it, or -- if
    `key` matches an entry already in flight -- skip `inner` entirely and just
    read the primary's output. `inner` is never iterated in the follower case, so
    constructing it (no side effects until first __anext__) then discarding it
    unread is safe."""
    if key is None:
        async for chunk in inner:
            yield chunk
        return
    entry, is_primary = _inflight_join(key)
    if not is_primary:
        async for chunk in _inflight_follow_stream(entry):
            yield chunk
        return
    try:
        async for chunk in inner:
            async with entry.condition:
                entry.chunks.append(chunk)
                entry.condition.notify_all()
            yield chunk
    finally:
        # This finally can itself run while the surrounding task is being
        # cancelled (e.g. the client disconnected mid-stream, which Starlette
        # turns into cancelling the task driving this generator) -- an
        # unshielded await right here can be cancelled again before it
        # completes, so entry.done never flips. Nothing would ever notify a
        # follower sharing this key again; it hangs on entry.condition.wait()
        # forever, with zero CPU use, until a process restart. Shield the
        # cleanup so it always finishes regardless of outer cancellation.
        #
        # Release has to be part of that same shielded unit, not a separate
        # unshielded line after it: if only the shield()-await itself got
        # cancelled (the mark-done+notify still completes in the background),
        # a bare `_inflight_release(key, entry)` right here would never run,
        # leaving `done=True` but the entry still registered -- a brand new
        # request reusing this key would then wrongly attach as a follower to
        # an already-finished entry and replay its stale/partial chunks
        # instead of starting fresh.
        await asyncio.shield(_mark_stream_done(key, entry))


async def _finish_result(key: tuple[str, str], entry: _InFlight, *, resp: dict | None = None,
                         status: int | None = None, error: BaseException | None = None) -> None:
    """Mirrors _mark_stream_done: mark/notify/release bundled into one
    shielded unit so an outer cancellation can't leave `done=True` with the
    entry still registered (see _dedup_stream's finally-block comment)."""
    async with entry.condition:
        if error is not None:
            entry.error = error
        else:
            entry.result = (resp, status)
        entry.done = True
        entry.condition.notify_all()
    _inflight_release(key, entry)


async def _dedup_result(key: tuple[str, str] | None, run) -> tuple[dict, int]:
    """Non-streaming counterpart: `run()` is an awaitable producing (resp, status).
    A follower awaits the primary's result instead of calling `run()` again.

    If the primary's `run()` raises, followers must still be released with that
    same failure rather than hang on `entry.condition` forever -- a stuck entry
    here previously blocked every future request sharing this (model, content)
    key permanently, since nothing else ever clears it short of a process
    restart. The primary re-raises unchanged; followers raise the same
    exception instead of getting a fabricated success result.

    The same orphaning can happen a second way even with that handled: if the
    primary's own task gets cancelled (e.g. the client disconnected and
    Starlette/ASGI cancels the in-flight handler), that cancellation can
    re-interrupt an unshielded cleanup await before entry.done ever flips --
    so _finish_result is always awaited via asyncio.shield() to guarantee it
    completes regardless of what's cancelling the caller."""
    if key is None:
        return await run()
    entry, is_primary = _inflight_join(key)
    if not is_primary:
        async with entry.condition:
            while not entry.done:
                await entry.condition.wait()
        if entry.error is not None:
            raise entry.error
        return entry.result
    try:
        resp, status = await run()
    except BaseException as exc:
        await asyncio.shield(_finish_result(key, entry, error=exc))
        raise
    await asyncio.shield(_finish_result(key, entry, resp=resp, status=status))
    return resp, status


def _retrieval_query(body: dict) -> str:
    """The text retrieval should be relevant TO: the live turn, not the history.

    Embedding the whole conversation would return whatever the session has
    talked about most, which is the opposite of useful -- the model already has
    that. The last user turn is what it is currently stuck on.
    """
    for m in reversed(body.get("messages") or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(seg.get("text", "") for seg in c
                             if isinstance(seg, dict) and isinstance(seg.get("text"), str))
    return ""


def _inject_retrieved(body: dict, destination: str, cfg) -> dict | None:
    """Put relevant earlier material in front of the model. Mutates `body`.

    CALLED BETWEEN destination resolution and privacy_evaluate, and that
    placement is the entire safety argument:

      after routing   -- retrieval can never change where the request goes. The
                         `*-auto` lanes escalate on prompt SIZE, so injecting
                         earlier could push a request past the threshold and
                         CAUSE the cloud hop the guardrails exist to make safe.
      before the gate -- anything cloud-bound is redacted like the rest of the
                         body. The gate is content-based, not provenance-based:
                         privacy/engine.py's _text_slots walks every message
                         regardless of role, so an injected system message is
                         scanned exactly like a user turn.

    That is why there is no separate "send the brain's output through the
    deflector" step. The content is already inside the thing the deflector
    scans.

    For a CLOUD destination the gate is the backstop, not the control. Tiers
    A/B/C catch things with shape -- keys, SSNs, IPs, names Presidio knows --
    and a sentence like "the review went badly" has no shape at all. So whole
    categories are excluded before the gate is ever asked. See the deny lists in
    config.yaml.

    Returns a small dict for logging, or None when nothing was injected.
    Fail-open throughout: retrieval is an enhancement, never a reason to fail a
    request that would otherwise work.
    """
    rcfg = CFG.get("retrieval") or {}
    if not rcfg.get("enabled"):
        return None

    is_cloud = cfg.is_cloud(destination)
    budget = rcfg.get("cloud" if is_cloud else "local") or {}

    query = _retrieval_query(body)
    if not query.strip():
        return None

    try:
        hits = open_brain.search(
            query,
            base_url=rcfg["base_url"],
            embed_model=rcfg["embed_model"],
            embed_url=rcfg["embed_url"],
            limit=int(budget.get("max_results", 3)),
            max_distance=float(budget.get("max_distance", 0.70)),
            timeout_s=float(rcfg.get("timeout_s", 5)),
            exclude_tags=tuple(budget.get("deny_tags") or ()),
        )
    except Exception:
        # open_brain.search does not raise by contract; this is belt and braces
        # so a future change there cannot take the whole request down.
        return None

    deny_sources = set(budget.get("deny_sources") or ())
    hits = [h for h in hits if h.source not in deny_sources]
    if not hits:
        return None

    kept, used = [], 0
    cap = int(budget.get("max_injected_tokens", 2000))
    for h in hits:
        cost = estimate_prompt_tokens(h.content)
        if used + cost > cap:
            break               # truncate by whole memories; never mid-note
        kept.append(h)
        used += cost
    if not kept:
        return None

    blocks = "\n\n".join(f"[{h.source}] {h.content}" for h in kept)
    body.setdefault("messages", []).insert(0, {
        "role": "system",
        "content": ("[retrieved from earlier sessions -- background only, not "
                    "part of this conversation]\n\n" + blocks),
    })
    return {"hits": len(kept), "tokens": used, "cloud": is_cloud,
            "closest": round(kept[0].distance, 4),
            "sources": sorted({h.source for h in kept})}


def _local_fallback_target(entry: dict) -> int:
    """Token budget to compact to when a `*-auto` lane answers locally instead.

    The local model's window MINUS room for the reply. Ollama sizes its context
    to cover the prompt AND the generation, so a prompt that fills the window
    leaves the answer nowhere to go -- and Ollama resolves that by silently
    dropping the OLDEST messages. That is the same fidelity loss compaction
    exists to manage, taken blindly, with nothing in the logs to say it
    happened. Compacting just before the cliff means the loss is at least
    deliberate and summarized.

    Deliberately NOT the lane's 45% escalation threshold, which is what an
    earlier draft of this proposed. Measured on qwen3.6:35b-a3b with an 83,030
    token agent transcript: answering it directly took 155.7s (148.5s of that
    prefill), while summarizing and then answering took 204.6s -- 31% slower,
    and repeated every turn because Pi resends full history and no summary is
    cached. Prefill dominates and the summarizer has to prefill the whole fold,
    so summarize+answer can never beat answer alone. Compaction is a FIT
    mechanism; no target value makes it a speed one.
    """
    reserve = int(entry.get("reply_reserve_tokens",
                            RT.get("local_reply_reserve_tokens", 16384)))
    return max(1, int(entry["context_tokens"]) - reserve)


def _generic_local_entry() -> dict | None:
    """A `local_cloud_reasoning`-shaped entry for the generic local fallback.

    The Tier B secret-block path drops to `lifeos_local_fallback` when the
    blocked model has no escalation entry of its own. That branch used to skip
    compaction entirely, because the dispatch guard is `if lcr_entry:` and this
    fallback has no such entry by definition -- so an oversized prompt carrying
    a private key went to a model that might not hold it, and Ollama makes room
    by silently dropping the OLDEST messages.

    Returns None when the window is not configured, so the behaviour degrades to
    what it was rather than compacting against a guessed size.
    """
    tokens = RT.get("lifeos_local_fallback_context_tokens")
    if not tokens:
        return None
    return {
        "context_tokens": int(tokens),
        "reply_reserve_tokens": int(
            RT.get("lifeos_local_fallback_reply_reserve_tokens")
            or RT.get("local_reply_reserve_tokens", 16384)),
    }


def resolve_routing(model: str, body: dict | None = None) -> tuple[str, str, dict]:
    cloud_headers = {
        "Authorization": f"Bearer {os.environ.get('OLLAMA_API_KEY', '')}"
    }

    lcr = RT.get("local_cloud_reasoning") or {}
    if model in lcr:
        entry = lcr[model]
        tokens = estimate_prompt_tokens(_extract_body_text(body or {}))
        threshold = entry["context_tokens"] * entry.get("threshold_pct", 75) / 100
        extra = {"tokens": tokens, "threshold": threshold}
        if tokens > threshold:
            # A suppressed cloud model is hidden from the dropdown but was
            # still routed to, because model_cooldown was only ever consulted
            # by the /pi/models.json builder. That is worst for a 410: the
            # model is retired for good, so every escalation past this point
            # was a guaranteed failure the operator could not even see coming.
            # These lanes exist precisely because they have a local tier to
            # fall back to, so staying local is the answer that already works.
            if model_cooldown.is_suppressed(entry["cloud_model"]):
                _log_route("local-cloud-reasoning-cooldown-local",
                           model, entry["local_model"], extra=extra)
                return resolve_routing(entry["local_model"], body)
            _log_route("local-cloud-reasoning-cloud", model, entry["cloud_model"], extra=extra)
            return "cloud", entry["cloud_model"], cloud_headers
        _log_route("local-cloud-reasoning-local", model, entry["local_model"], extra=extra)
        return resolve_routing(entry["local_model"], body)

    if model.startswith(RT["jarvis_cloud_prefix"]):
        # Cloud escalation for OpenJarvis-labeled work disabled 2026-09:
        # OpenJarvis is the harness client-side dispatch reserves for
        # declared-private work, and neither harness forwards
        # X-LifeOS-Sensitivity, so the Deflector has no way to know a given
        # request isn't private. Stay local until something actually forwards
        # that signal -- re-enabling escalation should be a reviewed code
        # change, not a client-config side effect.
        _log_route("jarvis-cloud-disabled", model, RT["jarvis_cloud_fallback"])
        return "tasks", RT["jarvis_cloud_fallback"], {}

    if model in RT["main_models"]:
        return "main", model, {}

    if model == RT["hermes_model"]:
        pressure = _read_pressure()
        if pressure.get("use_cloud_tasks"):
            _log_route("hermes-cloud-pressure",
                       model, RT["hermes_cloud_model"],
                       extra={"pressure_pct": pressure.get("pressure_pct")})
            return "cloud", RT["hermes_cloud_model"], cloud_headers
        return "tasks", model, {}

    if model in RT["jarvis_models"]:
        return "tasks", model, {}

    if model.endswith(":cloud"):
        return "cloud", model, cloud_headers

    return "tasks", model, {}


def _log_route(reason: str, original: str, routed: str, extra: dict | None = None):
    if not CFG["logging"]["log_routing"]:
        return
    rec = {"ts": time.time(), "reason": reason,
           "original": original, "routed": routed, **(extra or {})}
    # `id` ties this line to the same request across all four log files, and is
    # set LAST so a caller-supplied `extra` can never clobber it. No current
    # caller passes `id`, but the identical mistake in this function once made
    # every privacy block log its detector name in place of its event type.
    rec["id"] = tracing.trace_id()
    with open(retention.log_path(LOG_DIR, "routing"), "a") as f:
        f.write(json.dumps(rec) + "\n")


def _log_request_trace(rec: dict) -> None:
    """Metadata-only request trace: shape and timing, never content.

    Exists because a class of stall was invisible to every other log here --
    a request that never reaches the handler's first logging point produces
    no routing entry, no gate decision, and no upstream call, so the only
    evidence was an ESTABLISHED socket and an idle process. This records
    arrival separately from completion, so "never entered the handler",
    "entered but stalled reading the body", and "stalled downstream" are
    distinguishable after the fact rather than only under live observation.
    """
    if not CFG["logging"].get("log_request_trace", True):
        return
    try:
        with open(retention.log_path(LOG_DIR, "requests"), "a") as f:
            f.write(json.dumps({"ts": time.time(), **rec}) + "\n")
    except Exception:
        pass  # tracing must never take down a request


@app.middleware("http")
async def _request_trace(request: Request, call_next):
    rid = tracing.new_trace_id()
    request.state.trace_id = rid
    # Install the shared per-request context here so every log writer below
    # can stamp the same id without threading `request` through the stack.
    tracing.start(rid, path=request.url.path,
                  client=request.client.host if request.client else None)
    t0 = time.time()
    _log_request_trace({
        "ev": "arrive", "id": rid, "path": request.url.path,
        "client": request.client.host if request.client else None,
        # the headers that decide whether a body can even be fully read
        "content_length": request.headers.get("content-length"),
        "transfer_encoding": request.headers.get("transfer-encoding"),
        "expect": request.headers.get("expect"),
        "sensitivity": request.headers.get("x-lifeos-sensitivity"),
    })
    try:
        resp = await call_next(request)
    except BaseException as exc:
        _log_request_trace({"ev": "error", "id": rid,
                            "dur": round(time.time() - t0, 2),
                            "exc": type(exc).__name__})
        raise
    # NOTE: for a StreamingResponse this fires when headers are ready, not
    # when the body finishes -- so "headers_ms" small + no upstream activity
    # still means a stall further in.
    _log_request_trace({"ev": "headers", "id": rid,
                        "dur": round(time.time() - t0, 2),
                        "status": resp.status_code})
    return resp


def _redaction_categories(mapping: dict) -> dict:
    """{category: count} from placeholder KEYS, never their values.

    Placeholders are built as `<PII_{nonce}_{CATEGORY}_{n}>` by all three tiers
    (privacy/tier_a.py, tier_b.py, tier_c.py). The category itself can contain
    underscores (IP_PRIVATE, US_SSN), so it is everything between the nonce and
    the trailing index rather than a fixed field.
    """
    out: dict = {}
    for ph in mapping or {}:
        parts = ph.strip("<>").split("_")
        # PII, nonce, <category...>, index
        if len(parts) < 4 or parts[0] != "PII":
            cat = "unknown"
        else:
            cat = "_".join(parts[2:-1]) or "unknown"
        out[cat] = out.get(cat, 0) + 1
    return out


def _log_lifeos_escalation(rec: dict) -> None:
    with open(retention.log_path(LOG_DIR, "lifeos-escalations"), "a") as f:
        f.write(json.dumps({"ts": time.time(), "id": tracing.trace_id(), **rec}) + "\n")


def _tool_call_text(m: dict) -> str:
    """Flatten an assistant message's tool_calls (name + arguments) to text.

    Pi runs openai-completions-compat with a tool-heavy loop; call arguments
    routinely carry file contents / fetched pages and are real context tokens,
    not commentary -- omitting them is what caused the threshold estimate to
    undercount Pi's actual usage by ~2x."""
    parts: list[str] = []
    for tc in m.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments", "")
        if isinstance(args, dict):
            args = json.dumps(args)
        parts.append(f"{fn.get('name', '')} {args}")
    return "\n".join(p for p in parts if p.strip())


def _extract_body_text(body: dict) -> str:
    """Serialize prompt/messages content for prefilter scanning."""
    parts: list[str] = []
    if isinstance(body.get("prompt"), str):
        parts.append(body["prompt"])
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for seg in c:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    parts.append(seg["text"])
        tc_text = _tool_call_text(m)
        if tc_text:
            parts.append(tc_text)
    if body.get("tools"):
        parts.append(json.dumps(body["tools"]))
    return "\n".join(parts)


def _lifeos_nonredactable_text(body: dict) -> str:
    """Tool-call function names + the tools-schema JSON -- still scanned (same
    net as before) but not redacted in place, since that would need structured
    JSON-aware editing rather than a plain text-slot substitution. A hit here
    still forces the existing local-fallback behavior, same as pre-redaction
    lifeos_gate did for any hit."""
    parts: list[str] = []
    for m in body.get("messages") or []:
        for tc in m.get("tool_calls") or []:
            name = (tc.get("function") or {}).get("name", "")
            if name:
                parts.append(name)
    if body.get("tools"):
        parts.append(json.dumps(body["tools"]))
    return "\n".join(parts)


def lifeos_gate(model: str, sensitivity: str, body: dict) -> tuple[str, dict, Redact | None]:
    """
    Enforce LifeOS sensitivity + prefilter policy on `lifeos-cloud-*` requests.

    Returns (effective_model, log_extras, redact_decision). If policy blocks
    escalation outright (private sensitivity, or a home-network/SSH-key hit),
    returns the local fallback model with redact_decision=None. If only
    credential-shaped hits are found, they're redacted in place and escalation
    proceeds: effective_model is the real cloud-capable model and
    redact_decision carries the redacted body + placeholder mapping for the
    caller to send upstream and later rehydrate. Caller feeds effective_model
    into normal routing either way.
    """
    prefixes = RT.get("lifeos_prefixes") or {}
    if model not in prefixes:
        return model, {}, None

    fallback = RT.get("lifeos_local_fallback")
    if sensitivity == "private":
        _log_lifeos_escalation({
            "skill_model": model, "decision": "refused-private",
            "sensitivity": sensitivity, "fallback": fallback,
        })
        _log_route("lifeos-refused-private", model, fallback,
                   extra={"sensitivity": sensitivity})
        return fallback, {"lifeos_refused": "private"}, None

    slots = list(_redact_text_slots(body))
    slot_hits = [_lifeos_scan(text) for text, _ in slots]
    all_redactable_hits = [h for hits in slot_hits for h in hits]
    nonredact_hits = _lifeos_scan(_lifeos_nonredactable_text(body))
    block_cat = _lifeos_has_block(all_redactable_hits)

    if block_cat or nonredact_hits:
        cats = _lifeos_summarize(all_redactable_hits + nonredact_hits)
        _log_lifeos_escalation({
            "skill_model": model, "decision": "refused-prefilter",
            "sensitivity": sensitivity, "hits": cats, "fallback": fallback,
        })
        _log_route("lifeos-refused-prefilter", model, fallback,
                   extra={"sensitivity": sensitivity, "hit_categories": cats})
        return fallback, {"lifeos_refused": "prefilter", "hits": cats}, None

    resolved = prefixes[model]

    if not all_redactable_hits:
        _log_lifeos_escalation({
            "skill_model": model, "decision": "escalated",
            "sensitivity": sensitivity, "routed": resolved,
        })
        return resolved, {"lifeos_escalated_to": resolved}, None

    # Credential-shaped hits only -- redact in place on a fresh deep copy
    # (never mutate the caller's body) and still escalate to the cloud model.
    work = copy.deepcopy(body)
    mapping: dict = {}
    counters: dict = {}
    nonce = secrets.token_hex(3)
    for original, setter in _redact_text_slots(work):
        hits = _lifeos_scan(original)
        if hits:
            setter(_lifeos_redact(original, hits, mapping, nonce, counters))
    cats = _lifeos_summarize(all_redactable_hits)
    _log_lifeos_escalation({
        "skill_model": model, "decision": "redacted-escalated",
        "sensitivity": sensitivity, "hits": cats, "routed": resolved,
    })
    _log_route("lifeos-redacted-escalated", model, resolved,
               extra={"sensitivity": sensitivity, "hit_categories": cats})
    return (resolved, {"lifeos_escalated_to": resolved, "lifeos_redacted": cats},
            Redact(body=work, mapping=mapping))


def _log_kill(reason: str, meta: dict):
    # Record the kill on the request context BEFORE the log_kills guard, so the
    # `complete` event still reports outcome="kill:<reason>" even when kill
    # logging is switched off. Needed because _supervised_stream yields its kill
    # chunk and then returns NORMALLY -- from outside, a kill is indistinguishable
    # from a clean end unless the reason is recorded here.
    tracing.note(kill_reason=reason)
    if not CFG["logging"]["log_kills"]:
        return
    rec = {"ts": time.time(), "reason": reason, **meta}
    # Call sites already pass their own per-request `id` in `meta`. Spreading
    # `meta` over a pre-set trace id would silently clobber it -- the exact
    # dict-merge bug that previously made routing.jsonl log detector names
    # instead of event types. Demote theirs to `req_id` and set `id` LAST so
    # `id` means the same thing in every file and cannot be overwritten.
    if "id" in rec:
        rec["req_id"] = rec.pop("id")
    rec["id"] = tracing.trace_id()
    with open(retention.log_path(LOG_DIR, "kills"), "a") as f:
        f.write(json.dumps(rec) + "\n")


def _capture_upstream(body: dict) -> None:
    """Snapshot the post-routing, post-redaction body for an active capture.

    Called from every branch that dispatches upstream, because they return at
    different points -- the Claude branch returns before the Ollama one, and
    missing it there left the cloud-egress path with a null upstream.
    """
    ctx = tracing.get()
    if not ctx or not ctx.get("cap_certs"):
        return
    up, truncated = capture.clip(
        json.dumps(body, ensure_ascii=False),
        int((CFG.get("capture") or {}).get("max_request_bytes", 524288)))
    ctx["cap_request_upstream"] = up
    ctx["cap_truncated"]["request_upstream"] = truncated


def _finish_capture(model: str, response_text: str | None, out_bytes: int) -> None:
    """Hand a completed request to the encrypt pool.

    Shared by the streaming and NON-streaming paths. The non-streaming branch
    returns a JSONResponse and never builds a _traced_stream, so without this it
    would accept the capture header, populate the context, and then silently
    drop everything -- producing neither a blob nor a skip reason, which is the
    one ambiguity the skip logging exists to prevent.
    """
    ctx = tracing.get()
    if not ctx or not ctx.get("cap_certs"):
        return
    resp_cap = int((CFG.get("capture") or {}).get("max_response_bytes", 1048576))
    trunc = dict(ctx.get("cap_truncated") or {})
    trunc["response_out"] = out_bytes > resp_cap
    capture.enqueue({
        "trace_id": ctx.get("id"), "ts": time.time(),
        "client": ctx.get("cap_client"), "model": model,
        "certs": ctx["cap_certs"], "log_dir": LOG_DIR,
        "request_in": ctx.get("cap_request_in"),
        "request_upstream": ctx.get("cap_request_upstream"),
        "response_out": response_text,
        "truncated": trunc,
    })


async def _traced_stream(inner, model: str, req_id: str):
    """Wrap a response stream to emit exactly one `complete` event.

    This is the line that was missing during the outage that motivated all of
    this: a client sat for 300s and received 0 bytes, and nothing anywhere
    recorded that the stream ended, why, or how much had been sent. The
    middleware's `headers` event fires when headers are ready -- for a
    StreamingResponse that is long before the body finishes -- so it could not
    answer the question.

    Outcomes are distinguished deliberately:
      ok                -- generator ran to completion
      client_disconnect -- CancelledError/GeneratorExit, i.e. the caller hung up
      kill:<reason>     -- the supervisor terminated it (see _log_kill: the
                           generator returns normally after a kill, so this can
                           only be known from the recorded reason)
      upstream_error:<status>
                        -- the upstream tier rejected the request outright; the
                           body was an error document, not tokens. Recorded so
                           these stop counting as `ok`, which is how a run of
                           hard 400s once showed up as 100% success here.
      upstream_transport:<Type>
                        -- the tier never answered, or dropped the connection.
                           Caught in _supervised_stream, so it arrives here as
                           a normally-completed generator, not an exception.
      error:<Type>      -- anything else propagating out

    The `finally` only does synchronous work, so it completes even while the
    task is being torn down -- the same hazard the shielded cleanup in
    `_dedup_stream` exists for.
    """
    t0 = time.time()
    first: float | None = None
    n_bytes = 0
    n_chunks = 0
    outcome = "ok"
    ctx = tracing.get() or {}
    resp_buf = ctx.get("cap_resp")          # non-None only when capturing
    resp_cap = int((CFG.get("capture") or {}).get("max_response_bytes", 1048576))
    try:
        async for chunk in inner:
            if first is None:
                first = time.time() - t0
            n_bytes += len(chunk)
            n_chunks += 1
            if resp_buf is not None and len(resp_buf) < resp_cap:
                resp_buf.extend(chunk[:resp_cap - len(resp_buf)])
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        outcome = "client_disconnect"
        raise
    except BaseException as exc:
        outcome = f"error:{type(exc).__name__}"
        raise
    finally:
        if (kr := ctx.get("kill_reason")):
            outcome = f"kill:{kr}"
        elif (us := ctx.get("upstream_status")):
            outcome = f"upstream_error:{us}"
        elif (ue := ctx.get("upstream_error_type")):
            # Not optional. _supervised_stream now CATCHES transport failures
            # rather than letting them propagate, so the generator completes
            # normally and the `except BaseException` above never runs -- a
            # request that spent 60s getting nothing would otherwise be
            # recorded as `ok`. That is the same blind spot noted above, where
            # a run of hard 400s showed up here as 100% success.
            outcome = f"upstream_transport:{ue}"
        _log_request_trace({
            "ev": "complete", "id": ctx.get("id"), "req_id": req_id,
            # After a quota fallback the answer came from a DIFFERENT model than
            # the one this stream was opened for; recording only `model` would
            # credit the tier that refused.
            "model": ctx.get("quota_fallback") or model,
            "escalated_from": model if ctx.get("quota_fallback") else None,
            "outcome": outcome,
            "ttfb": round(first, 3) if first is not None else None,
            "dur": round(time.time() - t0, 2),
            "bytes_out": n_bytes, "chunks": n_chunks,
        })
        # Hand off AFTER the response is done, so encryption never delays a
        # byte. enqueue() is synchronous and non-raising by contract -- an
        # await here could be re-cancelled mid-teardown on client disconnect.
        _finish_capture(model,
                        bytes(resp_buf).decode("utf-8", "replace")
                        if resp_buf else None,
                        n_bytes)


def _ngram_repeat_ratio(tokens: list[str], window: int, n: int) -> float:
    if len(tokens) < window or n < 2:
        return 0.0
    recent = tokens[-window:]
    grams = [tuple(recent[i:i + n]) for i in range(len(recent) - n + 1)]
    if not grams:
        return 0.0
    counts = collections.Counter(grams)
    dup = sum(c for c in counts.values() if c > 1)
    return dup / len(grams)


def _kill_chunk(model: str, reason: str, is_chat: bool, is_openai: bool) -> bytes:
    """A well-formed terminal chunk for a forced kill, shaped for whichever
    wire format the caller is actually speaking (native Ollama NDJSON, or
    OpenAI-compatible SSE) so a supervisor kill reads as a clean stream end
    instead of a truncated/unparseable object the client chokes on.
    """
    if is_openai:
        obj = {
            "id": f"agentstop-kill-{reason}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": f"agentstop_{reason}"}],
        }
        return f"data: {json.dumps(obj)}\n\ndata: [DONE]\n\n".encode()
    obj = {
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat() + "Z",
        "done": True,
        "done_reason": f"agentstop_{reason}",
    }
    if is_chat:
        obj["message"] = {"role": "assistant", "content": ""}
    else:
        obj["response"] = ""
    return (json.dumps(obj) + "\n").encode()


def _upstream_error_chunk(model: str, status: int, is_chat: bool, is_openai: bool) -> bytes:
    """Terminal chunk for an upstream HTTP error, in the caller's wire format.

    Mirrors _kill_chunk but stays a separate function on purpose: a tier that
    REJECTED the request is not a supervisor kill, and collapsing the two would
    make `kills.jsonl` lie about why generation stopped.

    Without this, an upstream 4xx was forwarded as the *payload* of a 200 and the
    stream simply stopped -- no finish_reason ever reached the client. OpenAI-compat
    clients report that as "Stream ended without finish_reason" and then retry, which
    is pure waste against a deterministic 400: the caller cannot tell "the tier
    rejected this" from "the connection died", so it assumes the latter.
    """
    reason = f"upstream_error_{status}"
    if is_openai:
        obj = {
            "id": f"deflector-upstream-{status}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
        }
        return f"data: {json.dumps(obj)}\n\ndata: [DONE]\n\n".encode()
    obj = {
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat() + "Z",
        "done": True,
        "done_reason": reason,
    }
    if is_chat:
        obj["message"] = {"role": "assistant", "content": ""}
    else:
        obj["response"] = ""
    return (json.dumps(obj) + "\n").encode()


def _transport_error_chunk(model: str, is_chat: bool, is_openai: bool) -> bytes:
    """Terminal chunk for a tier that never answered, in the caller's format.

    Deliberately a sibling of _upstream_error_chunk rather than a generalisation
    of it. That one is shaped around a status code (its reason string embeds
    one), and a transport failure has none -- inventing a synthetic status to
    reuse it would push a fake number into logs and into the cooldown table,
    both of which are documented as carrying real observed statuses.

    The failure this covers is the one _upstream_error_chunk's docstring
    describes: without a terminal frame the stream just stops, and an
    OpenAI-compat client can only report "stream ended without finish_reason"
    and retry -- which is how a provider outage on 2026-09-08 turned into a
    retry storm the repeat-guard then rejected with a misleading 400.
    """
    reason = "upstream_timeout"
    if is_openai:
        obj = {
            "id": "deflector-upstream-timeout",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
        }
        return f"data: {json.dumps(obj)}\n\ndata: [DONE]\n\n".encode()
    obj = {
        "model": model,
        "created_at": datetime.now(timezone.utc).isoformat() + "Z",
        "done": True,
        "done_reason": reason,
    }
    if is_chat:
        obj["message"] = {"role": "assistant", "content": ""}
    else:
        obj["response"] = ""
    return (json.dumps(obj) + "\n").encode()


def _stream_media_type(path: str) -> str:
    """Content type for a streaming response, chosen by the API the caller spoke.

    The /v1 lane emits real SSE -- `data: {...}\n\n` frames and a `data: [DONE]`
    sentinel, from _kill_chunk, _upstream_error_chunk and _rehydrate_sse alike --
    but every streaming response used to be labelled `application/x-ndjson`.
    Clients pick a parser off this header, and pointing a line-splitting NDJSON
    reader at a framed protocol is asking for exactly the kind of mis-parse that
    a `data:` prefix and blank-line terminators are there to prevent.

    Uses the same path test as the rest of the streaming code (`is_openai`), so
    the label cannot drift from the frames actually being produced.
    """
    return ("text/event-stream" if path.rstrip("/").startswith("/v1/")
            else "application/x-ndjson")


def _stream_piece(obj: dict, is_openai: bool) -> str:
    """Extract just-the-visible-text from one parsed upstream line, for token
    accounting / kill-supervisor purposes only -- never used to decide what
    gets forwarded (see _supervised_stream: forwarding is unconditional).

    Handles both wire shapes, and both of Ollama's ways of keeping
    chain-of-thought out of the visible answer: a same-request sibling field
    (`message.thinking` on native NDJSON, `delta.reasoning` on OpenAI SSE) --
    counted here so stall/loop detection still sees real activity during a
    long thinking phase, but never mixed into forwarded content, since Ollama
    already keeps them structurally separate on both APIs observed in this
    deployment. (A model that instead inlines a literal `<think>` tag into
    the content field itself -- the case this used to try to strip -- would
    just have that tag pass through to the client unmodified; no known model
    in this deployment does that, and forwarding-but-not-stripping is a far
    safer failure mode than the indefinite buffering this replaces.)
    """
    if is_openai:
        delta = (obj.get("choices") or [{}])[0].get("delta", {})
        return delta.get("content", "") or delta.get("reasoning", "")
    msg = obj.get("message", {})
    return obj.get("response") or msg.get("content", "") or msg.get("thinking", "")


def _stream_tool_args(obj: dict, is_openai: bool) -> str:
    """Extract streamed tool-call argument text from one parsed upstream line.

    Separate from _stream_piece because the two feed DIFFERENT detectors. A real
    agent turn is mostly tool calls, and none of those bytes appear in `content`
    or `reasoning` -- so a supervisor that only reads those sees an idle stream
    while 12KB of healthy output flows past. That is not hypothetical: it killed
    Pi turns at ~42s with `tps 0.38`, having counted ~16 tokens out of 12589
    bytes, because the entire tool call was invisible.

    Named for its sibling _stream_piece, and distinct from _tool_call_text
    above: that one flattens a complete message from a REQUEST body for token
    estimation, this one reads one streaming RESPONSE frame.

    The two APIs disagree on the type of `arguments`, both verified live:
      OpenAI SSE   choices[0].delta.tool_calls[].function.arguments -> str
      native NDJSON      message.tool_calls[].function.arguments    -> dict
    A partial frame may carry either, or neither, so both are isinstance-guarded
    rather than assumed.
    """
    if is_openai:
        calls = (obj.get("choices") or [{}])[0].get("delta", {}).get("tool_calls")
    else:
        calls = obj.get("message", {}).get("tool_calls")
    if not isinstance(calls, list):
        return ""
    out = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        args = (call.get("function") or {}).get("arguments")
        if isinstance(args, str):
            out.append(args)
        elif isinstance(args, (dict, list)):
            out.append(json.dumps(args))
    return "".join(out)


async def _supervised_stream(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: dict,
    model: str,
    req_id: str,
    extra_headers: dict,
) -> AsyncIterator[bytes]:
    start = time.time()
    token_buf: list[str] = []
    total_tokens = 0
    tool_arg_tokens = 0
    # Idle-gap stall detection: the question is "has anything happened lately",
    # not "what is the average rate since we started". The old cumulative
    # average was wrong in both directions -- it punished a slow-but-progressing
    # stream for its own history, and a fast burst followed by a total freeze
    # kept the average above the floor so only max_wall_seconds (900s) ever
    # ended it.
    # None until the stream produces something. NOT `start`: the gap from
    # request start to the first token is time-to-first-byte -- prefill -- and
    # charging that as idle kills big prompts outright. A 220KB compaction
    # request (a whole conversation squashed into 2 messages to summarize) took
    # 76s to prefill on the local 35B and was killed the instant its first
    # token landed, with idle == elapsed == ttfb == 76.5s.
    #
    # The `total_tokens > 0` guard below was meant to cover exactly this and
    # does not: the first chunk both sets total_tokens and is measured against
    # the pre-first-token gap, so the guard opens in the same iteration the
    # kill fires. Time-to-first-byte is bounded by the read timeout instead,
    # which is the right tool for it.
    last_activity: float | None = None

    # Ollama does NOT stream tool-call arguments incrementally on either API --
    # it buffers the whole call and emits it as one frame when the model is
    # done. Measured against Ollama directly, with this proxy out of the path:
    # a 250-line file written into a tool argument went 31s, 42s and 52s with
    # ZERO bytes on the wire, and the real Pi failure was a 64s window. That
    # silence scales with the size of what is being written, so it has no
    # useful upper bound.
    #
    # There is therefore no liveness signal to be clever with: a request that
    # can call tools has to be given an allowance longer than its tool calls
    # take. Requests without tools do stream token-by-token and keep the tight
    # threshold, which is why this is picked per-request rather than globally
    # relaxed.
    idle_limit = TH["stall_idle_tools_s"] if body.get("tools") else TH["stall_idle_s"]
    is_chat = path.rstrip("/").endswith("/api/chat")
    is_openai = path.rstrip("/").startswith("/v1/")

    # Line buffer only feeds token accounting / kill detection below, never
    # gates forwarding -- a line split across two httpx chunks would
    # otherwise fail to parse and silently undercount tokens for a stall
    # check, but that's a parsing nicety, not something forwarding should
    # ever wait on.
    line_buf = b""

    _inc(model)
    try:
        async with client.stream(method, path, json=body,
                                 headers=extra_headers or None) as upstream:
            if upstream.status_code >= 400:
                # Fail-open governs CONTENT, not status. A 4xx/5xx body is an error
                # document, never tokens, so forwarding it verbatim just hands the
                # client an unparseable stream that ends with no finish_reason. Read
                # it (it is small), record it, terminate the stream cleanly. Keyed
                # only off the status line and taken BEFORE any byte is forwarded,
                # so nothing about 2xx forwarding changes.
                raw = await upstream.aread()
                tracing.note(upstream_status=upstream.status_code)
                _log_request_trace({
                    "ev": "upstream_error", "id": tracing.trace_id(),
                    "req_id": req_id, "model": model,
                    "status": upstream.status_code,
                    "detail": raw[:300].decode("utf-8", "replace"),
                })
                # Take the model out of the dropdown so it stops being offered.
                # Duration is per status: 402/429 are account state and clear on
                # their own, 410 is retirement and never does.
                if model_cooldown.record(
                        model, upstream.status_code,
                        raw[:300].decode("utf-8", "replace"),
                        RT.get("model_cooldowns") or {}):
                    _log_route("model-cooldown", model, model,
                               extra={"status": upstream.status_code})
                yield _upstream_error_chunk(model, upstream.status_code,
                                            is_chat, is_openai)
                return
            async for chunk in upstream.aiter_bytes():
                yield chunk

                # Snapshot BEFORE parsing this chunk. The idle check has to
                # measure the gap that just elapsed, not the gap after this
                # chunk's own activity is folded in -- otherwise any chunk
                # carrying a single token resets the gap to zero, and a stream
                # dribbling one token every few minutes would never be caught.
                prev_activity = last_activity

                line_buf += chunk
                *complete_lines, line_buf = line_buf.split(b"\n")
                for line in complete_lines:
                    line = line.removeprefix(b"data: ") if is_openai else line
                    if not line.strip() or line.strip() == b"[DONE]":
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    piece = _stream_piece(obj, is_openai)
                    if piece:
                        new_toks = piece.split()
                        token_buf.extend(new_toks)
                        total_tokens += len(new_toks)
                        last_activity = time.time()

                    # Counted as activity, but deliberately NOT added to
                    # token_buf: tool arguments are structured JSON whose keys,
                    # braces and indentation repeat by nature, and feeding that
                    # to a 0.7-overlap n-gram detector would just trade the
                    # false-stall class for a false-ngram_loop one.
                    targs = _stream_tool_args(obj, is_openai)
                    if targs:
                        n = len(targs.split())
                        total_tokens += n
                        tool_arg_tokens += n
                        last_activity = time.time()

                now = time.time()
                elapsed = now - start

                if total_tokens > TH["max_tokens_per_request"]:
                    _log_kill("max_tokens", {"id": req_id, "tokens": total_tokens})
                    yield _kill_chunk(model, "max_tokens", is_chat, is_openai)
                    await upstream.aclose()
                    return

                if elapsed > TH["max_wall_seconds"]:
                    _log_kill("max_wall_seconds", {"id": req_id, "elapsed": elapsed})
                    yield _kill_chunk(model, "max_wall_seconds", is_chat, is_openai)
                    await upstream.aclose()
                    return

                ratio = _ngram_repeat_ratio(
                    token_buf, TH["ngram_window"], TH["ngram_size"]
                )
                if ratio > TH["ngram_overlap_ratio"]:
                    _log_kill("ngram_loop", {"id": req_id,
                                              "ratio": ratio, "tokens": total_tokens})
                    yield _kill_chunk(model, "ngram_loop", is_chat, is_openai)
                    await upstream.aclose()
                    return

                # Measures the gap BETWEEN activity, and only once there has
                # been some. A model still working through a large prefill has
                # produced nothing yet, so there is no gap to judge -- that
                # window belongs to the read timeout, not here.
                #
                # Note this can only fire while chunks are still ARRIVING. A
                # stream that goes completely silent never re-enters this loop
                # at all; that case is bounded by the client read timeout (see
                # upstream.*_read_timeout_s), not here.
                idle = None if prev_activity is None else now - prev_activity
                if idle is not None and idle > idle_limit:
                    # `limit` recorded because the two budgets are far apart:
                    # without it a stall record cannot say which one it was
                    # judged against.
                    _log_kill("stall", {"id": req_id, "idle": idle,
                                        "elapsed": elapsed,
                                        "limit": idle_limit,
                                        "tools": bool(body.get("tools")),
                                        "tokens": total_tokens,
                                        "tool_arg_tokens": tool_arg_tokens})
                    yield _kill_chunk(model, "stall", is_chat, is_openai)
                    await upstream.aclose()
                    return
    except httpx.TransportError as exc:
        # The tier accepted the connection and produced nothing, or dropped it
        # mid-flight. Either way no tokens are coming, and letting this
        # propagate truncates a response whose headers are already on the wire
        # -- which the client can only report as "stream ended without
        # finish_reason". Same treatment as a 4xx/5xx above, for the same
        # reason: end the stream in a shape the caller can actually read.
        #
        # TransportError rather than TimeoutException: a refused connection
        # (ConnectError) and a reset one (RemoteProtocolError) mean the same
        # thing operationally, and handling them differently would only create
        # a second unreadable failure mode to discover later.
        #
        # `silent` distinguishes "never produced a byte" from "died after
        # generating". It does NO work for the fallback decision -- that is
        # taken from the first chunk only, so a mid-stream failure is already
        # structurally unreachable there -- but it is load-bearing for the
        # cooldown, where a failure after minutes of successful generation is
        # weak evidence that the tier is down.
        silent = last_activity is None
        tracing.note(upstream_error_type=type(exc).__name__,
                     upstream_silent=silent)
        _log_request_trace({
            "ev": "upstream_transport", "id": tracing.trace_id(),
            "req_id": req_id, "model": model,
            "error": type(exc).__name__, "silent": silent,
        })
        # Stop escalating into a tier that is answering with nothing, so the
        # next turn does not pay another full read timeout before falling back.
        #
        # Gated on the CLOUD client specifically. The local tiers use this same
        # function and time out too (local_read_timeout_s is 270s), and cooling
        # one of those down would take the fallback target offline along with
        # the thing it was covering for.
        #
        # Gated on `silent` because a failure after minutes of successful
        # generation is weak evidence that the tier is down -- the model just
        # proved it works, and one reset is as likely.
        if silent and client is _clients.get("cloud"):
            secs = CFG["upstream"]["cloud_silent_cooldown_s"]
            if model_cooldown.record_unavailable(model, secs, type(exc).__name__):
                _log_route("model-cooldown-silent", model, model,
                           extra={"error": type(exc).__name__, "secs": secs})
        yield _transport_error_chunk(model, is_chat, is_openai)
        return
    finally:
        _dec(model)


def _claude_override(request: Request) -> str | None:
    """Rule 2: return the allowlisted Claude model id iff the X-Deflector-Mode header
    opts in. The request body's model is never trusted for Claude routing; opus needs
    the extra X-Deflector-Opus: 1 header. Unknown values fall through (None).

    The legacy X-AgentStop-Mode / X-AgentStop-Opus headers remain accepted for backward
    compatibility during the rename transition (new header wins if both are present)."""
    mode = (request.headers.get("x-deflector-mode")
            or request.headers.get("x-agentstop-mode", ""))
    if mode in CLAUDE_ALLOWLIST:
        return mode
    opus_optin = (request.headers.get("x-deflector-opus")
                  or request.headers.get("x-agentstop-opus"))
    if mode == CLAUDE_OPUS and opus_optin == "1":
        return mode
    return None


def _apply_think_policy(body: dict, actual_model: str, path: str,
                        request: Request) -> None:
    """Set the thinking-suppression field on `body`, in place, per policy.

    Precedence, highest first:
      1. an explicit `reasoning_effort`/`think` already in the request body --
         a caller that asked for something specific is never overridden
      2. the X-Deflector-Think header (off | low | medium | high)
      3. routing.think_off_models -- default OFF for the configured models

    The field written is path-dependent, because Ollama's two APIs disagree:
    native /api/* honors `think: false`, while the OpenAI-compat /v1 path
    silently ignores it and needs `reasoning_effort: "none"` instead. Writing
    the wrong one is a silent no-op, not an error, which is exactly how the
    previous hermes-only `think = False` line came to have no effect on /v1
    clients (i.e. Pi -- effectively every real caller here).
    """
    if body.get("reasoning_effort") is not None or body.get("think") is not None:
        # Honour the caller's intent, but normalize the one spelling that is a
        # trap: Ollama accepts "none", "minimal", "low", "medium", "high",
        # "xhigh" and "max", and hard 400s on "off" -- the most natural word to
        # reach for, and the one this proxy's own header vocabulary uses. Both
        # mean the same thing, so rewriting it preserves precedence rule 1
        # rather than overriding it.
        eff = body.get("reasoning_effort")
        if isinstance(eff, str) and eff.strip().lower() in THINK_OFF_WORDS:
            body["reasoning_effort"] = "none"
        return

    is_openai = path.rstrip("/").startswith("/v1/")
    field = "reasoning_effort" if is_openai else "think"

    level = ""
    for h in THINK_HEADERS:
        level = (request.headers.get(h) or "").strip().lower()
        if level:
            break

    if level:
        if level in THINK_OFF_WORDS:
            body[field] = "none" if is_openai else False
        else:
            # Ollama treats any non-"none" effort as thinking-ON, and the
            # level is NOT a gradient in practice. Measured on qwen3.6:35b-a3b,
            # 3 distinct prompts per level, reasoning chars emitted:
            #   minimal 5674 | low 3236 | medium 4542 | high 3531
            #   xhigh   6305 | max 5074 | none 0
            # The within-level spread (low ranged 1697-7329) is far wider than
            # any gap between levels, so it is on/off with noise on top. The
            # caller's level is still passed through verbatim so a model that
            # DOES distinguish them gets the real value.
            body[field] = level if is_openai else True
        return

    if actual_model in THINK_OFF_MODELS:
        body[field] = "none" if is_openai else False


async def _compact_for_local(body: dict, client_key: str, actual_model: str,
                             context_tokens: int, *, reason: str,
                             original_model: str) -> dict:
    """Fold the conversation down if it would overflow a local model's window.

    Shared by the two paths that drop a request off cloud escalation onto the
    local tier -- a Tier B secret block, and a cloud quota refusal -- because
    both face the same problem: the prompt was sized for a window the local
    model does not have. Returns `body` untouched when it already fits, so the
    summarization hop stays a last resort rather than a routine cost.
    """
    tokens = estimate_prompt_tokens(_extract_body_text(body))
    if tokens <= context_tokens:
        return body

    summarizer_client = _clients[client_key]

    async def _summarize(prompt: str, _client=summarizer_client, _model=actual_model) -> str:
        r = await _client.post(
            "/api/chat",
            json={"model": _model, "stream": False,
                  "messages": [{"role": "user", "content": prompt}]},
        )
        return r.json().get("message", {}).get("content", "")

    out = await compact_messages(body, _summarize)
    _log_route(reason, original_model, actual_model,
               extra={"tokens": tokens, "context_tokens": context_tokens})
    return out


async def _quota_fallback_stream(primary, make_fallback):
    """Swap a cloud stream for a local one when the tier cannot answer at all.

    Two shapes, both properties of the TIER rather than the request:

    * A 402 or 429 from Ollama Cloud -- "you have reached your session usage
      limit", or "this model requires a subscription". No retry against that
      tier can succeed. (429 came first; 402 arrived days later when a model
      that had been free moved behind a subscription, killing sessions the same
      way -- hence a set rather than a single status.)
    * Silence. On 2026-09-08 the tier accepted connections and returned zero
      bytes for every model and both API dialects. There is no status to key
      off in that case, which is why it needs its own marker rather than a
      synthesised status code -- see _TIER_REFUSAL_STATUSES, which is scoped to
      account refusals on purpose.

    Either way the local tier can still answer, and for the `*-auto` lanes
    cloud was only ever an opportunistic upgrade, so falling back costs answer
    quality and nothing else.

    Implemented by peeking exactly ONE chunk rather than buffering: on an error
    _supervised_stream yields its terminal chunk and returns, so the decision is
    always available from the first chunk, and a healthy stream is passed
    straight through with only its first chunk delayed -- which the caller was
    waiting on regardless. Buffering the whole response would defeat streaming.
    """
    it = primary.__aiter__()
    try:
        first = await it.__anext__()
    except StopAsyncIteration:
        return

    ctx = tracing.get() or {}
    # `upstream_silent` rather than `upstream_error_type`: a failure that
    # happened AFTER real output must not be swapped, or the caller would get
    # a second, contradictory answer stitched onto the first. That case cannot
    # actually reach here today -- the decision is taken from the first chunk
    # only, so anything mid-stream has long since taken the passthrough tail
    # below -- but keying on the narrow flag means a future version that
    # buffers more than one chunk stays correct instead of silently wrong.
    if (ctx.get("upstream_status") in _TIER_REFUSAL_STATUSES
            or ctx.get("upstream_silent")):
        # Close the primary explicitly: it is parked at its `yield` inside a
        # try/finally that decrements the in-flight counter, and abandoning it
        # would leave that count high until GC got round to it.
        await it.aclose()
        # Keep WHY before dropping the markers. The routing log for the
        # fallback is written after this point, and "the tier was silent"
        # versus "the account was refused" are different operational stories
        # that would otherwise be indistinguishable after the fact.
        ctx["fallback_trigger"] = "silent" if ctx.get("upstream_silent") else "quota"
        # Drop the error markers so _traced_stream reports how this actually
        # ended, not the attempt we recovered from. All three, not just the one
        # that fired: a leftover `upstream_error_type` would record a request
        # that recovered and answered successfully as a transport failure.
        for key in ("upstream_status", "upstream_silent", "upstream_error_type"):
            ctx.pop(key, None)
        async for chunk in make_fallback():
            yield chunk
        return

    yield first
    async for chunk in it:
        yield chunk


def _rehydrate_body_for_local(body: dict, mapping: dict | None) -> None:
    """Restore real values before a body shaped for cloud reaches a local model.

    Local destinations are exempt from redaction by design -- privacy/engine.py
    short-circuits them to Proceed and never masks anything. A body carrying
    placeholders here therefore only happens because a CLOUD attempt was
    diverted onto the local tier afterwards: a tier refusal, a silent tier, or
    a Tier B secret block. Sending the masked form on costs answer quality for
    no privacy benefit, since nothing is leaving the box.

    The sharper reason is _compact_for_local. It asks a model to summarize the
    conversation, and a model paraphrasing `<PII_1d3_HIGH_ENTROPY_5>` will
    happily reformat it. A reformatted placeholder no longer matches
    PLACEHOLDER_RE, so it can never be restored on the way out -- and the
    mapping is per-request, so it is gone for good. That is the same class of
    unrecoverable dead placeholder fixed in 81cb975, re-entering by a different
    door.

    Walks _text_slots -- the SAME enumeration redaction wrote through -- rather
    than substituting into the raw tool_calls[].function.arguments string. That
    string is serialized JSON, and an original value containing a quote, a
    backslash or a newline would break the envelope if pasted in flat. See
    privacy/engine.py on why that envelope has to survive: Ollama Cloud rejects
    the whole request, the bad call stays in the history, and every later turn
    fails identically.

    In place, and O(1) when there is no mapping -- the common case, since a
    lane held local by a cooldown was never redacted to begin with.

    Deliberately no substring pre-filter. `"<PII_" in text` looks like a cheap
    reject, but PLACEHOLDER_RE is IGNORECASE, so it would skip a slot the
    regex would have matched. One pass over the slots costs single-digit
    milliseconds on a 390KB body; a case-sensitivity trap in a privacy path
    costs much more than that.
    """
    if not mapping:
        return
    for text, setter in _redact_text_slots(body):
        restored = rehydrate_complete(text, mapping)
        # Only write when something changed: for a tool-call argument the
        # setter re-serialises the whole parsed object, which is not free.
        if restored != text:
            setter(restored)


def _sanitize_tool_call_args(body: dict, req_id: str) -> None:
    """Guarantee every tool_calls[].function.arguments is a parseable JSON-object
    string before the body leaves this box. Mutates `body` in place.

    Ollama Cloud's OpenAI-compat endpoint rejects the ENTIRE request with
    `400 invalid tool call arguments` if any of them is an empty string, null, or
    unparseable. The local tiers accept the same body happily, so a conversation
    can run fine for hours and then break the instant it escalates to cloud --
    and because the offending call is now part of the history, every retry and
    every later turn fails identically. That is unrecoverable without editing the
    transcript, so it is worth repairing rather than propagating.

    Two sources produce these and both land here: a client emitting `""` for a
    zero-argument call, and this box's own Tier A/C redaction writing into the
    arguments string (privacy/engine.py `_text_slots`). Normalising at dispatch
    covers both, and covers every tier that receives the JSON body.

    Content is preserved where there is any -- unparseable text is wrapped rather
    than dropped, since it may be a redacted payload whose placeholders still have
    to survive to rehydration. Only the counts are logged, never the arguments.
    """
    repaired = 0
    for m in body.get("messages") or []:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function")
            if not isinstance(fn, dict):
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    if isinstance(json.loads(args), dict):
                        continue        # already valid: leave byte-identical
                except Exception:
                    pass
                # Non-empty but not a JSON object: keep the text under a key so a
                # redacted placeholder is still there to be rehydrated later.
                fn["arguments"] = json.dumps({"_raw": args}) if args.strip() else "{}"
            elif isinstance(args, dict):
                # Some clients send the object itself; the wire format wants a
                # string and upstream refuses the raw object.
                fn["arguments"] = json.dumps(args)
            else:
                fn["arguments"] = "{}"
            repaired += 1
    if repaired:
        _log_request_trace({"ev": "tool_args_repaired", "id": tracing.trace_id(),
                            "req_id": req_id, "count": repaired})


async def _claude_guarded(body: dict, model_id: str,
                          max_wall: float) -> AsyncIterator[bytes]:
    """Bucket B §3a: hold a semaphore slot for the whole Claude stream. The `claude`
    subprocess lives for the stream's lifetime, so `async with` around the async
    generator correctly bounds the number of live processes; overflow callers await
    the slot rather than erroring."""
    async with _claude_sem:
        async for frame in stream_claude(body, model_id, mapping=None,
                                         max_wall_seconds=max_wall):
            yield frame


def _rehydrate_text(s: str, reh: RehydrateStream) -> str:
    return reh.feed(s) + reh.flush()


def _sse_frame(obj: dict) -> bytes:
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def _sse_tail_frame(tail_content: str, tail_reasoning: str,
                    tool_tails: dict[int, str] | None = None) -> bytes | None:
    delta: dict = {}
    if tail_content:
        delta["content"] = tail_content
    if tail_reasoning:
        delta["reasoning"] = tail_reasoning
    # Buffered tails of streamed tool-call arguments, keyed by call index. The
    # client concatenates fragments per index, so emitting these in one final
    # frame appends each tail to the call it came from.
    for idx, tail in sorted((tool_tails or {}).items()):
        if tail:
            delta.setdefault("tool_calls", []).append(
                {"index": idx, "function": {"arguments": tail}})
    if not delta:
        return None
    return _sse_frame({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})


def _rehydrate_json_value(node, mapping: dict):
    """Restore placeholders in every string nested in a fully-formed JSON value.

    Mirrors privacy.engine._json_string_slots, which is what redacted these in
    the first place: recurse into nested objects and lists, and leave dict KEYS
    alone -- rewriting one would rename a tool parameter and break the call.
    """
    if isinstance(node, str):
        return rehydrate_complete(node, mapping)
    if isinstance(node, list):
        return [_rehydrate_json_value(v, mapping) for v in node]
    if isinstance(node, dict):
        return {k: _rehydrate_json_value(v, mapping) for k, v in node.items()}
    return node


async def _rehydrate_sse(inner, mapping: dict):
    """OpenAI-compat counterpart to _rehydrate_ndjson.

    Without this, a redacted request on the /v1 path streamed placeholders
    straight through to the caller: the NDJSON version below does
    `json.loads(line)` on `data: {...}`, which always raises, so every frame
    took the pass-through branch un-rehydrated and the user read raw
    `<PII_..._IP_PRIVATE_1>` text in the answer. That branch also dropped
    SSE's blank-line frame terminator, since it re-emitted each line with a
    single "\\n".

    `content` and `reasoning` get their OWN RehydrateStream. They interleave
    within a single response, and one shared buffer would let a partial
    placeholder split across `content` frames be "completed" by unrelated
    `reasoning` text, corrupting both.

    Lines are reassembled across chunk boundaries (the `line_buf` idiom from
    _supervised_stream). Splitting each chunk on its own is what corrupted real
    streams: a frame cut mid-JSON made `json.loads` raise, the partial line went
    out through the except-branch terminated by a single "\n", and its remainder
    followed as a bare line. With no blank line between them the client folds the
    NEXT real `data:` line into the same event, joining with "\n" per the SSE
    spec -- surfacing as "Bad control character in string literal in JSON".
    Nothing here may assume a chunk is a whole frame.
    """
    reh_c = RehydrateStream(mapping)
    reh_r = RehydrateStream(mapping)
    # One stream per tool-call index. Redaction rewrites strings INSIDE tool-call
    # arguments (privacy.engine._json_string_slots), so a placeholder reaches the
    # caller through `delta.tool_calls[].function.arguments` just as readily as
    # through prose -- and until this existed it went out verbatim, so the client
    # executed the tool with the literal `<PII_..._1>` as an argument value.
    # Per-index because arguments for different calls interleave, and one shared
    # buffer would let a partial placeholder in call 0 be "completed" by call 1.
    reh_t: dict[int, RehydrateStream] = {}
    flushed = False
    line_buf = b""

    def _emit(raw: bytes):
        """Output frames for ONE complete input line."""
        nonlocal flushed
        s = raw.strip()
        if not s:
            return
        if not s.startswith(b"data:"):
            yield raw + b"\n"
            return
        payload = s.split(b":", 1)[1].strip()
        if payload == b"[DONE]":
            if not flushed:
                flushed = True
                tail = _sse_tail_frame(reh_c.flush(), reh_r.flush(),
                                       {i: r.flush() for i, r in reh_t.items()})
                if tail:
                    yield tail
            yield b"data: [DONE]\n\n"
            return
        try:
            obj = json.loads(payload)
        except Exception:
            yield raw + b"\n"
            return
        for ch in obj.get("choices") or []:
            d = ch.get("delta")
            if not isinstance(d, dict):
                continue
            if isinstance(d.get("content"), str):
                d["content"] = reh_c.feed(d["content"])
            if isinstance(d.get("reasoning"), str):
                d["reasoning"] = reh_r.feed(d["reasoning"])
            for pos, tc in enumerate(d.get("tool_calls") or []):
                fn = tc.get("function") if isinstance(tc, dict) else None
                if not (isinstance(fn, dict) and isinstance(fn.get("arguments"), str)):
                    continue
                # `index` is what the client keys fragments by; fall back to
                # position for providers that omit it on a single call.
                idx = tc.get("index")
                if not isinstance(idx, int):
                    idx = pos
                if idx not in reh_t:
                    reh_t[idx] = RehydrateStream(mapping)
                fn["arguments"] = reh_t[idx].feed(fn["arguments"])
        yield _sse_frame(obj)

    async for chunk in inner:
        line_buf += chunk
        *complete_lines, line_buf = line_buf.split(b"\n")
        for raw in complete_lines:
            for out in _emit(raw):
                yield out

    # A trailing line with no newline after it is still a line.
    for out in _emit(line_buf):
        yield out

    # Upstream ended without a [DONE] sentinel -- still emit any buffered tail
    # rather than swallowing the end of the answer.
    if not flushed:
        tail = _sse_tail_frame(reh_c.flush(), reh_r.flush(),
                               {i: r.flush() for i, r in reh_t.items()})
        if tail:
            yield tail


async def _rehydrate_ndjson(inner, mapping: dict):
    """Wrap an Ollama NDJSON byte stream, restoring redacted originals in assistant
    text before the caller sees them (§6, trusted-cloud path). A placeholder can split
    across chunks, so one RehydrateStream buffers across the whole stream; buffered tail
    is flushed onto the terminal frame."""
    reh = RehydrateStream(mapping)
    line_buf = b""

    def _emit(line: bytes):
        if not line.strip():
            return
        try:
            obj = json.loads(line)
        except Exception:
            yield line + b"\n"
            return
        msg = obj.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            msg["content"] = reh.feed(msg["content"])
        elif isinstance(obj.get("response"), str):
            obj["response"] = reh.feed(obj["response"])
        # Ollama buffers a whole tool call and emits it as ONE frame, so these
        # arguments never split across chunks the way prose does -- substitute
        # directly rather than through `reh`, whose buffered tail would have no
        # later frame to rejoin. `arguments` is an object here (not the
        # fragmented string the /v1 path streams), so walk it.
        if isinstance(msg, dict):
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict) and "arguments" in fn:
                    fn["arguments"] = _rehydrate_json_value(fn["arguments"], mapping)
        if obj.get("done"):
            tail = reh.flush()
            if tail:
                if isinstance(msg, dict):
                    msg["content"] = (msg.get("content") or "") + tail
                else:
                    obj["response"] = (obj.get("response") or "") + tail
        yield (json.dumps(obj) + "\n").encode()

    async for chunk in inner:
        line_buf += chunk
        *complete_lines, line_buf = line_buf.split(b"\n")
        for line in complete_lines:
            for out in _emit(line):
                yield out

    for out in _emit(line_buf):
        yield out


# Outstanding proof-of-possession challenges: id -> (fp, nonce, expiry).
# In memory and single-use on purpose. This is a one-shot flow, so losing them
# on restart costs nothing, and never persisting them means a stolen disk
# yields no replayable material.
_CHALLENGES: dict[str, tuple[str, bytes, float]] = {}
_CHALLENGE_TTL_S = 120
_MAX_CHALLENGES = 64


def _reap_challenges() -> None:
    now = time.time()
    for k, (_, _, exp) in list(_CHALLENGES.items()):
        if exp < now:
            _CHALLENGES.pop(k, None)


@app.post("/capture/challenge")
async def capture_challenge(request: Request):
    """Issue a proof-of-possession challenge for an enrolled fingerprint.

    A nonce encrypted TO the client's own certificate: only the holder of the
    matching private key can read it back. Reuses the CMS path already in use
    for captures rather than introducing a signing scheme.

    Handing this out freely is fine -- it is ciphertext only that key can open.
    """
    cfg = CFG.get("capture") or {}
    if not cfg.get("enabled", False):
        return JSONResponse({"error": "capture is disabled"}, status_code=503)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "expected a JSON body"}, status_code=400)

    fp = str((payload or {}).get("fp") or "").strip().lower()
    cert = enrollment.cert_for(fp)
    if not cert:
        return JSONResponse({"error": "unknown fingerprint"}, status_code=404)

    _reap_challenges()
    if len(_CHALLENGES) >= _MAX_CHALLENGES:
        return JSONResponse({"error": "too many outstanding challenges"},
                            status_code=429)

    nonce = secrets.token_bytes(32)
    proc = await asyncio.create_subprocess_exec(
        cfg.get("openssl_bin", "/opt/homebrew/bin/openssl"), "cms", "-encrypt",
        "-binary", "-aes-256-cbc", "-outform", "DER", cert,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    blob, err = await proc.communicate(nonce)
    if proc.returncode != 0 or not blob:
        _log_request_trace({"ev": "challenge_failed", "fp": fp,
                            "detail": (err or b"").decode(errors="replace")[:200]})
        return JSONResponse({"error": "could not build a challenge"},
                            status_code=500)

    cid = secrets.token_hex(16)
    _CHALLENGES[cid] = (fp, nonce, time.time() + _CHALLENGE_TTL_S)
    return {"challenge_id": cid, "cms": base64.b64encode(blob).decode(),
            "expires_in": _CHALLENGE_TTL_S}


@app.post("/capture/backup")
async def capture_backup(request: Request):
    """Attach a backup recipient to a client, on proof it holds the key.

    Unlike primary enrolment this CANNOT be trust-on-first-use. A backup is
    added alongside the client's own certificate, so an attacker who registered
    one would receive a readable copy of everything while the victim's own
    decryption kept working perfectly -- a hijack with no symptom at all.
    """
    cfg = CFG.get("capture") or {}
    if not cfg.get("enabled", False):
        return JSONResponse({"error": "capture is disabled"}, status_code=503)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "expected a JSON body"}, status_code=400)

    p = payload or {}
    fp = str(p.get("fp") or "").strip().lower()
    cid = str(p.get("challenge_id") or "")
    nonce_b64 = str(p.get("nonce") or "")
    cert = p.get("cert") or ""
    client_ip = request.client.host if request.client else None

    def reject(detail, code=400):
        _log_request_trace({"ev": "backup_rejected", "client": client_ip,
                            "fp": fp, "detail": detail})
        return JSONResponse({"error": detail}, status_code=code)

    _reap_challenges()
    # Consume the challenge whatever happens next: one attempt per challenge,
    # so a wrong answer cannot be retried against the same nonce.
    entry = _CHALLENGES.pop(cid, None)
    if not entry:
        return reject("unknown or expired challenge", 403)
    want_fp, nonce, _exp = entry
    if want_fp != fp:
        return reject("challenge was issued for a different fingerprint", 403)

    try:
        got = base64.b64decode(nonce_b64, validate=True)
    except Exception:
        return reject("nonce is not valid base64", 403)
    if not secrets.compare_digest(got, nonce):
        return reject("challenge response did not match", 403)

    if not isinstance(cert, str):
        return reject("cert must be a PEM string")
    try:
        bfp = enrollment.set_backup(
            fp, cert.encode(), cfg.get("openssl_bin", "/opt/homebrew/bin/openssl"))
    except enrollment.EnrollError as e:
        return reject(str(e))

    _log_request_trace({"ev": "backup_registered", "client": client_ip,
                        "fp": fp, "backup_fp": bfp})
    return {"status": "registered", "fp": fp, "backup_fp": bfp}


@app.get("/capture/status")
async def capture_status(request: Request):
    """What a client needs to explain itself to its user.

    Returns counts, never the list of enrolled fingerprints -- that would hand
    any LAN host a roster of who is capturing. A caller asking about a specific
    fingerprint already knows it, so answering for that one leaks nothing.
    """
    cfg = CFG.get("capture") or {}
    log = CFG.get("logging") or {}
    out = {
        "enabled": bool(cfg.get("enabled", False)),
        # Operator-configured GLOBAL backups only. A client's own backup is
        # reported per-fingerprint below -- conflating the two made status
        # report "no backup" to a client that had just registered one, which
        # is the most dangerous possible direction for this field to be wrong.
        "global_backups": len(capture.backup_certs()),
        "enrolled": len(enrollment.enrolled()),
        "ip_recipients": len(cfg.get("recipients") or {}),
        "capture_retention_days": log.get("retention_capture_days"),
        "metadata_retention_days": log.get("retention_metadata_days"),
        "header": cfg.get("header", "x-deflector-capture"),
    }
    fp = (request.query_params.get("fp") or "").strip().lower()
    if fp:
        out["fp"] = fp
        out["known"] = enrollment.cert_for(fp) is not None
        out["has_backup"] = enrollment.backup_for(fp) is not None
        for row in enrollment.listing():
            if row.get("fp") == fp:
                out["mac"] = row.get("mac")
                out["label"] = row.get("label")
                out["first_seen"] = row.get("first_seen")
                out["backup_fp"] = row.get("backup_fp")
                out["backup_set"] = row.get("backup_set")
                break
    return out


@app.post("/capture/enroll")
async def capture_enroll(request: Request):
    """A client registers its own certificate for encrypted capture.

    Deliberately needs no token and no operator action -- see enrollment.py for
    why that is safe. The reply carries the fingerprint the client then sends
    in its capture header.
    """
    cfg = CFG.get("capture") or {}
    if not cfg.get("enabled", False):
        return JSONResponse({"error": "capture is disabled on this server"},
                            status_code=503)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "expected a JSON body"}, status_code=400)

    cert = (payload or {}).get("cert") or ""
    if not isinstance(cert, str):
        return JSONResponse({"error": "cert must be a PEM string"}, status_code=400)

    client_ip = request.client.host if request.client else None
    try:
        status, fp = enrollment.enroll(
            cert.encode(), cfg.get("openssl_bin", "/opt/homebrew/bin/openssl"),
            mac=(payload or {}).get("mac"), label=(payload or {}).get("label"))
    except enrollment.EnrollError as e:
        _log_request_trace({"ev": "enroll_rejected", "client": client_ip,
                            "detail": str(e)})
        return JSONResponse({"error": str(e)}, status_code=400)

    _log_request_trace({"ev": "enroll_ok", "client": client_ip, "status": status,
                        "fp": fp, "mac": (payload or {}).get("mac")})
    return {"fingerprint": fp, "status": status,
            "header": cfg.get("header", "x-deflector-capture")}


@app.get("/pi/models.json")
async def pi_models():
    """Thin-client model registry: single source of truth is config.yaml's
    routing.pi_clients. Clients (e.g. PAI Pi on the M4 mini) pull this instead
    of carrying a manually-maintained local copy — add a model here once.

    Models the upstream has refused are omitted (see model_cooldown): offering
    one that cannot answer is worse than not offering it, because Pi surfaces
    the failure as a generic connection error and the operator re-picks the
    same dead entry."""
    providers = {}
    for provider_id, p in (RT.get("pi_clients") or {}).items():
        providers[provider_id] = {
            "baseUrl": f"http://{PI_HOST}:{p['port']}{p['path']}",
            "api": p["api"],
            "apiKey": p["api_key"],
            "compat": p.get("compat", {}),
            "models": [
                {
                    "id": m["id"],
                    "name": m.get("name"),
                    "contextWindow": m.get("contextWindow"),
                    "maxTokens": m.get("maxTokens"),
                    # Capability declaration, not a preference. The Pi client
                    # gates its whole reasoning_effort send path on BOTH this
                    # and compat.supportsReasoningEffort:
                    #   options.reasoningEffort && model.reasoning
                    #     && compat.supportsReasoningEffort
                    # so omitting it silently drops the field, and the client
                    # cannot ask for chain-of-thought no matter what its own
                    # thinkingLevel says. Default False: a model that does not
                    # reason should not advertise that it does.
                    "reasoning": bool(m.get("reasoning", False)),
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                }
                for m in p.get("models", [])
                if not model_cooldown.is_suppressed(m["id"])
            ],
        }
    return JSONResponse({"providers": providers})


@app.post("/api/generate")
@app.post("/api/chat")
@app.post("/v1/chat/completions")
async def supervised(request: Request):
    # Timed separately from everything else: reading the request body is the
    # one step that can block indefinitely on the CLIENT (a short write against
    # its own Content-Length, an unterminated chunked body, a 100-continue
    # standoff), and it happens before any other log line in this handler --
    # so a stall here is otherwise indistinguishable from the server hanging.
    _t_body = time.time()
    body = await request.json()
    _body_s = round(time.time() - _t_body, 2)
    original_model = body.get("model", "")
    req_id = f"{int(time.time() * 1000)}-{id(body)}"
    _log_request_trace({
        "ev": "body_read", "id": getattr(request.state, "trace_id", None),
        "req_id": req_id, "dur": _body_s, "model": original_model,
        "bytes": len(json.dumps(body)), "messages": len(body.get("messages") or []),
        "tools": len(body.get("tools") or []),
        "slots": len(_redact_text_slots(body)),
    })

    # Capture decision + PRISTINE body snapshot, taken HERE and nowhere later.
    # `body` is both mutated in place (body["model"] = ...) and rebound
    # (body = decision.body) further down, so re-reading it after routing would
    # silently capture the redacted/routed version and defeat the entire point
    # of capturing the original.
    _client_ip = request.client.host if request.client else None
    # A fingerprint in the header identifies the client by its own certificate,
    # which survives DHCP; the IP map is the legacy fallback.
    _cap_fp = capture.capture_fp(request.headers)
    _certs = capture.recipient_for(_client_ip, fp=_cap_fp)
    if capture.wants_capture(request.headers, _client_ip):
        _snap, _trunc = capture.clip(
            json.dumps(body, ensure_ascii=False),
            int((CFG.get("capture") or {}).get("max_request_bytes", 524288)))
        tracing.note(cap_certs=_certs, cap_request_in=_snap,
                     cap_truncated={"request_in": _trunc},
                     cap_client=_client_ip, cap_resp=bytearray())
    elif (request.headers.get(
            (CFG.get("capture") or {}).get("header", "x-deflector-capture")) or ""
          ).strip().lower() not in ("", "0", "false", "no", "off"):
        # Genuinely asked for but not permitted -- record WHY, so a missing
        # capture file is never ambiguous later. Matches wants_capture()'s
        # truthiness exactly, so `X-Deflector-Capture: 0` is not a "skip".
        # Check `enabled` first: when capture is off globally, recipient_for()
        # returns None for every IP, which would otherwise be misreported as
        # "no_recipient" when the real reason is "disabled".
        _log_request_trace({
            "ev": "capture_skip", "id": tracing.trace_id(), "client": _client_ip,
            "reason": ("disabled" if not (CFG.get("capture") or {}).get("enabled")
                       else "unknown_fingerprint" if _cap_fp else "no_recipient"),
        })

    # Computed once, now, from the pristine incoming body -- see _InFlight above for
    # why this can't be recomputed later after routing/redaction have touched `body`.
    dedup_key = _inflight_key(original_model, body)

    # Repeat guard: same prompt persisting for this model -> stop and ask, rather
    # than keep burning tokens/compute on what looks like a stuck retry loop. Both
    # thresholds must clear so a client's own retry-with-backoff after a transient
    # failure (a handful of identical resends within seconds) sails through.
    confirmed = any(request.headers.get(h) == "1" for h in CONFIRM_HEADERS)
    n, span = _repeat_status(original_model, body)
    if n >= REPEAT_THRESHOLD and span >= REPEAT_WINDOW_S and not confirmed:
        _log_route("repeat-guard", original_model, "CONFIRM", extra={"count": n, "span_s": round(span)})
        # 400, not 429: 429 reads as "back off and retry" to well-behaved HTTP
        # clients, which would retry straight into this guard and compound the count.
        return JSONResponse(
            {
                "error": f"same prompt sent {n} times over {round(span)}s for {original_model!r}; "
                         "looks like a stuck retry loop. Resend with "
                         "X-Deflector-Confirm-Repeat: 1 to proceed anyway.",
                "repeat_guard": True,
                "count": n,
            },
            status_code=400,
        )
    if confirmed:
        _repeat_reset(original_model)

    # LifeOS special-case: lifeos-claude has no local shim on the Pi host yet.
    # This used to suggest spawning `claude` CLI directly -- i.e. instructing
    # the caller to bypass the gate below for a feature that doesn't exist.
    if original_model == RT.get("lifeos_claude_marker"):
        return JSONResponse(
            {"error": "lifeos-claude is not supported yet"},
            status_code=421,
        )

    # LifeOS gate: sensitivity + prefilter policy before normal routing. May
    # itself redact credential-shaped hits and still escalate to cloud -- see
    # lifeos_gate's docstring.
    sensitivity = request.headers.get("x-lifeos-sensitivity", "").lower()
    gated_model, lifeos_extras, lifeos_redact_decision = lifeos_gate(original_model, sensitivity, body)
    lifeos_mapping: dict = {}
    if lifeos_redact_decision is not None:
        body = lifeos_redact_decision.body
        lifeos_mapping = lifeos_redact_decision.mapping
    if gated_model != original_model:
        body["model"] = gated_model

    cfg = get_config()
    claude_model = _claude_override(request)

    # Destination for the privacy trust decision (Rule 1 needs a target).
    #
    # Resolved against the PRE-redaction body, and then REUSED at dispatch rather
    # than recomputed. It used to be computed twice, and redaction runs in
    # between: placeholders are shorter than the values they replace, so the
    # second resolve measured a smaller prompt than the first and could reach a
    # different answer. The `*-auto` lanes escalate on prompt size, and real
    # traffic sits right on that boundary -- 59,139 tokens in, over the 58,982
    # threshold, so privacy evaluated it as CLOUD and redacted it; redaction took
    # it down to 57,537, back under the threshold, so it dispatched LOCAL. The
    # request was then answered by a local model reading masked text: pure cost
    # and a degraded answer, for data that never left the box.
    #
    # Routing on the pre-redaction size slightly overestimates what is actually
    # sent to a cloud model. That is the right side to err on -- it keeps the
    # destination that redaction was decided for, so local requests are never
    # redacted and redacted requests never stay local.
    routing_body = body
    route: tuple[str, str, dict] | None = None
    if claude_model:
        destination = ANTHROPIC
    else:
        route = resolve_routing(gated_model, routing_body)
        destination = _provider_for(route[0])

    # Cross-session grounding. MUST sit exactly here -- after `destination` is
    # settled, before the gate below -- so retrieved material cannot change the
    # routing decision and cannot reach a cloud provider unredacted. See
    # _inject_retrieved for the full argument; moving this line either way
    # breaks a property, not a preference.
    if (_retrieved := _inject_retrieved(body, destination, cfg)):
        _log_route("retrieval-injected", original_model, gated_model, extra=_retrieved)

    # RULE 1 — privacy evaluation, absolute precedence (block > reroute > redact).
    decision = privacy_evaluate(body, destination, cfg)
    mapping: dict | None = None

    # A `local_cloud_reasoning` route already names a local model it escalated
    # from — if Tier B blocks the cloud hop on a secret, that local model is a
    # known-safe fallback (data never leaves the box), so drop to it instead of
    # hard-rejecting the request outright.
    lcr_entry = (RT.get("local_cloud_reasoning") or {}).get(gated_model)
    forced_local_model: str | None = None

    if isinstance(decision, Block):
        if decision.reason.startswith("secret:"):
            # Tier B now only blocks on `private_key` (everything else it finds
            # gets redacted-and-sent instead, see privacy/tier_b.py). Never
            # surface a bare 403 for this to the caller if a local model can
            # take the request instead -- prefer the model's own configured
            # local-cloud-reasoning sibling, falling back to the generic
            # LifeOS local model, and only 403 if neither is configured.
            if lcr_entry and not claude_model:
                forced_local_model = lcr_entry["local_model"]
                _log_route("privacy-secret-local-fallback", original_model, forced_local_model,
                           extra={"detail": decision.reason})
            else:
                forced_local_model = RT.get("lifeos_local_fallback")
                claude_model = None
                if forced_local_model:
                    _log_route("privacy-secret-local-fallback-generic", original_model,
                               forced_local_model, extra={"detail": decision.reason})
                else:
                    _log_route("privacy-block", original_model, "REJECT",
                               extra={"detail": decision.reason})
                    return JSONResponse(
                        {"error": f"blocked by privacy policy: {decision.reason}"},
                        status_code=403,
                    )
        else:
            # Tier A hard block (operator-declared block-list value) -- absolute,
            # every destination including local, unchanged.
            _log_route("privacy-block", original_model, "REJECT",
                       extra={"detail": decision.reason})
            return JSONResponse(
                {"error": f"blocked by privacy policy: {decision.reason}"},
                status_code=403,
            )

    if isinstance(decision, Reroute):
        # Strip restricted (Claude); re-resolve to local/trusted, re-run privacy once.
        _log_route("privacy-reroute", original_model, "local",
                   extra={"excluded": sorted(decision.exclude)})
        claude_model = None
        route = resolve_routing(gated_model, routing_body)
        destination = _provider_for(route[0])
        decision = privacy_evaluate(body, destination, cfg)
        if isinstance(decision, Block):
            return JSONResponse(
                {"error": f"blocked by privacy policy: {decision.reason}"},
                status_code=403,
            )

    if isinstance(decision, Redact):
        body = decision.body
        mapping = decision.mapping
        # Tier A/C redaction previously left NO trace anywhere: only the LifeOS
        # layer logged. "Was this request redacted?" was therefore unanswerable
        # after the fact, and an empty routing log read as "no redaction ran" --
        # which sent a stream-corruption investigation down the wrong path for
        # hours. Categories and counts only; the values are the whole point.
        _log_route("privacy-redacted", original_model, gated_model,
                   extra={"hit_categories": _redaction_categories(mapping)})

    # Merge the LifeOS-layer redaction (if any) with privacy_evaluate's own --
    # each used its own per-request nonce, so placeholder keys can't collide.
    if lifeos_mapping:
        mapping = {**lifeos_mapping, **(mapping or {})}

    # Bucket B §3b — token-preflight downshift. Estimate prompt size before a Claude
    # hop; if it won't fit the context budget, drop off Claude and fall through to
    # local/ollama-cloud routing rather than reject. The reactive output-cap abort in
    # stream_claude remains the backstop. Uses the (possibly redacted) body.
    if claude_model:
        preflight_text = build_prompt(body) + "\n" + build_system(body)
        if over_budget(preflight_text, claude_model):
            _log_route("preflight-downshift", original_model, "local",
                       extra={"claude_model": claude_model})
            claude_model = None

    # RULE 2 — Claude dispatch via the `claude` CLI (no API key on this box).
    if claude_model:
        _log_route("claude-cli", original_model, claude_model)
        inner = _claude_guarded(body, claude_model, TH["max_wall_seconds"])
        # Snapshot here too: this branch returns before the shared
        # request_upstream capture below, and it is the CLOUD-EGRESS path --
        # precisely where "prove the privacy pipeline behaved" matters most.
        # Without this, every Claude-routed capture carried a null upstream.
        _capture_upstream(body)
        return StreamingResponse(
            _traced_stream(_dedup_stream(dedup_key, inner), claude_model, req_id),
            media_type="application/x-ndjson",
        )

    # RULES 3-4 / default — existing Ollama routing + kill-supervisor.
    if forced_local_model:
        # MUST precede the _compact_for_local call below, which measures the
        # body and then hands it to a model to summarize -- see
        # _rehydrate_body_for_local on why a summarized placeholder is
        # unrecoverable.
        #
        # This branch is reached by a Tier B secret block, and also by a lifeos
        # redaction, which runs at lifeos_gate() BEFORE any destination exists
        # and rewrites `body` regardless of where it ends up. The block itself
        # contributes nothing to `mapping` -- Block carries only `reason` -- so
        # there is no blocked secret here that could be restored.
        _rehydrate_body_for_local(body, mapping)
        client_key, actual_model, extra_headers = resolve_routing(forced_local_model, body)
        # Both landings need a size check, not just the escalation one. A model
        # with its own local_cloud_reasoning entry brings its window with it;
        # the generic secret-block fallback gets one from config instead of
        # being dispatched blind, which is what it used to be.
        size_entry = lcr_entry or _generic_local_entry()
        if size_entry:
            body = await _compact_for_local(
                body, client_key, actual_model, _local_fallback_target(size_entry),
                reason="privacy-secret-local-compacted", original_model=original_model)
    else:
        # Reuse the route the privacy decision was made against. Only the Claude
        # paths leave it unresolved (the header route, and the preflight
        # downshift that drops off Claude) -- both still route on `routing_body`,
        # so the size redaction changed never reaches this decision.
        if route is None:
            route = resolve_routing(gated_model, routing_body)
        client_key, actual_model, extra_headers = route

        # lifeos_gate refuses escalation on a credential hit and rewrites the
        # model to the local fallback BEFORE privacy_evaluate runs, so this
        # lands a cloud-sized prompt on a local model that may not hold it --
        # with no lcr_entry, because the gated model is now a local one. That
        # left the request with no size check at all, and Ollama makes room by
        # silently dropping the OLDEST messages.
        #
        # Scoped to a downgrade the DEFLECTOR chose. A caller that asks for a
        # local model directly owns its own sizing; this is only for
        # destinations the caller did not pick.
        if lifeos_extras.get("lifeos_refused") and not lcr_entry:
            size_entry = _generic_local_entry()
            if size_entry:
                body = await _compact_for_local(
                    body, client_key, actual_model, _local_fallback_target(size_entry),
                    reason="lifeos-refused-local-compacted",
                    original_model=original_model)
    body["model"] = actual_model
    client = _clients[client_key]

    path = request.url.path

    # Must run after redaction (it can rewrite tool-call arguments) and before
    # the capture below, so what is captured is genuinely what was sent.
    _sanitize_tool_call_args(body, req_id)

    # What ACTUALLY leaves the box, post-routing and post-redaction. Paired
    # with the pristine snapshot above, this is what lets you prove the privacy
    # pipeline did the right thing rather than infer it.
    _capture_upstream(body)

    # Thinking policy (default-off for the configured local lanes, header-
    # overridable per request). Must run after `path` is known -- the field to
    # write depends on which API the caller is speaking.
    _apply_think_policy(body, actual_model, path, request)

    if body.get("stream", True):
        inner = _supervised_stream(client, "POST", path, body,
                                   actual_model, req_id, extra_headers)

        # Cloud quota is not a per-request failure, so a `*-auto` lane that
        # escalated should drop back to its own local tier rather than end the
        # session. Only wired up where there IS a local tier to fall back to:
        # an explicitly-requested cloud model has no implied second choice, and
        # silently answering from a different model would be a lie.
        if (lcr_entry and client_key == "cloud"
                and not forced_local_model
                and actual_model == lcr_entry.get("cloud_model")):

            async def _local_after_quota(_e=lcr_entry, _b=body, _p=path,
                                         _rq=request, _rid=req_id,
                                         _orig=original_model, _map=mapping):
                # MUST precede _compact_for_local below. That function measures
                # the body to decide whether to fold it -- placeholders are
                # shorter than what they replaced, so masked text under-counts
                # -- and then hands it to a model to summarize. A model
                # paraphrasing a placeholder reformats it beyond
                # PLACEHOLDER_RE's reach, and the mapping is per-request, so
                # the value is then unrecoverable. See _rehydrate_body_for_local.
                _rehydrate_body_for_local(_b, _map)
                fb_key, fb_model, fb_headers = resolve_routing(_e["local_model"], _b)
                fb_body = await _compact_for_local(
                    _b, fb_key, fb_model, _local_fallback_target(_e),
                    reason="cloud-fallback-compacted", original_model=_orig)
                fb_body["model"] = fb_model
                # Re-run per-model policy for the new destination: the local
                # lanes default thinking OFF while the cloud model does not, and
                # a value the caller set explicitly still wins (the policy
                # returns early on one), so this cannot override an intent.
                _apply_think_policy(fb_body, fb_model, _p, _rq)
                _sanitize_tool_call_args(fb_body, _rid)
                # Re-snapshot: what actually left the box is now this body.
                _capture_upstream(fb_body)
                # Reason string, not the function name: this fires for a silent
                # tier as well as a quota refusal now, and a routing log that
                # says "quota" for an outage would misdirect the next
                # investigation. `why` carries the actual trigger.
                _log_route("cloud-fallback", _orig, fb_model,
                           extra={"cloud_model": _e["cloud_model"],
                                  "why": (tracing.get() or {}).get(
                                      "fallback_trigger", "unknown")})
                tracing.note(quota_fallback=fb_model)
                async for chunk in _supervised_stream(
                        _clients[fb_key], "POST", _p, fb_body,
                        fb_model, _rid, fb_headers):
                    yield chunk

            inner = _quota_fallback_stream(inner, _local_after_quota)

        if mapping:
            # Pick the rehydrator that matches the wire format -- the NDJSON one
            # cannot parse `data: {...}` frames and would stream placeholders
            # through to the caller verbatim.
            inner = (_rehydrate_sse(inner, mapping)
                     if path.rstrip("/").startswith("/v1/")
                     else _rehydrate_ndjson(inner, mapping))
        return StreamingResponse(
            _traced_stream(_dedup_stream(dedup_key, inner), actual_model, req_id),
            media_type=_stream_media_type(path))

    async def _run() -> tuple[dict, int]:
        _inc(actual_model)
        try:
            r = await client.post(path, json=body,
                                  headers=extra_headers if extra_headers else {})
        except httpx.TransportError as exc:
            # Same failure the streaming path handles, minus the ability to
            # salvage the turn: no bytes have been sent, so this can still be
            # an honest HTTP error. Uncaught it became a bare 500 with no body
            # (measured: 60.1s then "Internal Server Error"), which says
            # nothing about which tier failed or why.
            #
            # 502, not 500: the failure is upstream of this proxy, and a caller
            # deciding whether to retry needs to know the difference.
            #
            # Deliberately does NOT retry against the local tier the way the
            # streaming path does. That would mean duplicating fallback,
            # compaction, think-policy and argument sanitisation onto a second
            # code path for a case no client here hits -- Pi streams. Worth
            # doing when something actually needs it.
            # Always "silent" here, unlike the streaming path. There is no
            # partial-progress signal to read -- the response is buffered, so
            # the caller received nothing either way -- and a tier that failed
            # to complete a buffered response is not usefully distinguishable
            # from one that never started.
            silent = True
            tracing.note(upstream_error_type=type(exc).__name__,
                         upstream_silent=silent)
            _log_request_trace({
                "ev": "upstream_transport", "id": tracing.trace_id(),
                "req_id": req_id, "model": actual_model,
                "error": type(exc).__name__, "silent": silent,
            })
            if client is _clients.get("cloud"):
                secs = CFG["upstream"]["cloud_silent_cooldown_s"]
                if model_cooldown.record_unavailable(
                        actual_model, secs, type(exc).__name__):
                    _log_route("model-cooldown-silent", actual_model, actual_model,
                               extra={"error": type(exc).__name__, "secs": secs})
            return ({"error": f"upstream {actual_model} did not respond "
                              f"({type(exc).__name__})"}, 502)
        finally:
            _dec(actual_model)
        resp = r.json()
        m = resp.get("message")
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            m["content"] = strip_cot(m["content"])
        elif isinstance(resp.get("response"), str):
            resp["response"] = strip_cot(resp["response"])
        if mapping:
            reh = RehydrateStream(mapping)
            m = resp.get("message")
            if isinstance(m, dict) and isinstance(m.get("content"), str):
                m["content"] = _rehydrate_text(m["content"], reh)
            elif isinstance(resp.get("response"), str):
                resp["response"] = _rehydrate_text(resp["response"], reh)
        return resp, r.status_code

    resp, status = await _dedup_result(dedup_key, _run)
    _serialized = json.dumps(resp, ensure_ascii=False)

    # Emit `complete` here too, so EVERY supervised request has one regardless
    # of streaming. Without this a successful `stream: false` request looks
    # identical to a genuine stall (body_read, then nothing) -- which is not
    # hypothetical: it made the triage tool report healthy 200s as stalls.
    _ctx = tracing.get() or {}
    _log_request_trace({
        "ev": "complete", "id": _ctx.get("id"), "req_id": req_id,
        "model": actual_model,
        "outcome": f"kill:{_ctx['kill_reason']}" if _ctx.get("kill_reason")
                   else ("ok" if status < 400 else f"http:{status}"),
        "ttfb": None,                       # buffered: no first-byte to measure
        "dur": round(time.time() - _ctx.get("t0", time.time()), 2),
        "bytes_out": len(_serialized), "chunks": 1, "streamed": False,
    })

    # Non-streaming requests never build a _traced_stream, so capture has to be
    # completed here or a `stream: false` request would accept the capture
    # header and then silently produce nothing at all -- no blob, and no skip
    # reason either, which is exactly the ambiguity the skip logging prevents.
    _finish_capture(actual_model, _serialized, len(_serialized))
    return JSONResponse(resp, status_code=status)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def passthrough(path: str, request: Request):
    body = await request.body()
    r = await _clients["main"].request(
        request.method,
        f"/{path}",
        content=body,
        headers={k: v for k, v in request.headers.items()
                 if k.lower() != "host"},
        params=request.query_params,
    )
    # Proxy raw bytes + upstream content-type. Do NOT r.json() — some Ollama endpoints
    # (e.g. NDJSON streams) return multi-document bodies that json.loads rejects.
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type"),
    )


async def _retention_loop():
    """Seal legacy logs once, then prune on an interval.

    In-process rather than a LaunchDaemon: the log files are user-owned, so no
    root is needed, and keeping the policy next to the config that drives it
    avoids a second place to update. If the service is down it is not writing
    logs either, so nothing accumulates unattended.

    Never allowed to take down the service -- a failing sweep logs and retries
    on the next tick.
    """
    log_cfg = CFG["logging"]
    interval = log_cfg.get("retention_sweep_interval_s", 3600)
    meta_days = log_cfg.get("retention_metadata_days", 14)
    cap_days = log_cfg.get("retention_capture_days", 7)

    try:
        sealed = await asyncio.to_thread(retention.seal_legacy_logs, LOG_DIR)
        if sealed:
            _log_request_trace({"ev": "retention_seal", "sealed": sealed})
    except Exception as exc:
        _log_request_trace({"ev": "retention_error", "phase": "seal",
                            "exc": type(exc).__name__})

    while True:
        try:
            # to_thread: scandir/unlink are blocking syscalls and must not run
            # on the event loop.
            res = await asyncio.to_thread(
                retention.sweep_once, LOG_DIR, meta_days, cap_days)
            if res["removed"]:
                _log_request_trace({"ev": "retention_sweep",
                                    "removed": len(res["removed"]),
                                    "foreign_untouched": res["foreign_untouched"]})
        except Exception as exc:
            _log_request_trace({"ev": "retention_error", "phase": "sweep",
                                "exc": type(exc).__name__})
        await asyncio.sleep(interval)


def _warn_unclassified_providers() -> list[str]:
    """Name any upstream whose provider label has no trust classification.

    A WARNING, not a fatal error, deliberately. ~/.agentstop/provider-trust.yaml
    is optional (privacy/config.py), so on a fresh checkout every label is
    unclassified -- refusing to boot would break new installs and CI over a
    condition that is already safe by construction: an unclassified label is
    not in `trusted` and not in `restricted`, so it takes the cloud path and
    gets fully redacted. Loud, not fatal.

    Returns the offending labels so this is testable without reading logs.
    """
    cfg = get_config()
    unclassified = sorted({
        label for key, label in PROVIDER_LABELS.items()
        if label != LOCAL and label not in cfg.trusted and label not in cfg.restricted
    })
    for label in unclassified:
        _log_request_trace({
            "ev": "provider_unclassified", "provider": label,
            "detail": ("no trust class in provider-trust.yaml; treated as an "
                       "untrusted cloud destination and fully redacted"),
        })
    return unclassified


@app.on_event("startup")
async def _start_retention():
    _warn_unclassified_providers()
    app.state.retention_task = asyncio.create_task(_retention_loop())
    # Validate certs / openssl once here rather than per request; any problem
    # disables capture entirely (fail closed) with a single logged reason.
    capture.configure(CFG.get("capture") or {}, _log_request_trace)
    await capture.start_workers()


@app.on_event("shutdown")
async def _close():
    task = getattr(app.state, "retention_task", None)
    if task:
        task.cancel()
    await capture.stop_workers()
    for c in _clients.values():
        await c.aclose()
