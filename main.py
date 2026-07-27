# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import asyncio
import collections
import json
import os
import pathlib
import threading
import time
from typing import AsyncIterator

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from budget import over_budget
from lifeos.prefilter import scan as _lifeos_scan, summarize as _lifeos_summarize
from privacy import Block, Redact, Reroute, privacy_evaluate
from privacy.config import LOCAL, get_config
from privacy.rehydrate import RehydrateStream
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

_clients = {
    "main":  httpx.AsyncClient(base_url=CFG["upstream"]["main"],
                               timeout=CFG["upstream"]["timeout_s"]),
    "tasks": httpx.AsyncClient(base_url=CFG["upstream"]["tasks"],
                               timeout=CFG["upstream"]["timeout_s"]),
    "cloud": httpx.AsyncClient(base_url=CFG["upstream"]["cloud"],
                               timeout=CFG["upstream"]["cloud_timeout_s"]),
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
    with _lock:
        return _active.get(RT["hermes_model"], 0) > 0


def _jarvis_concurrent() -> int:
    with _lock:
        return sum(_active.get(m, 0) for m in RT["jarvis_models"])


def _read_pressure() -> dict:
    try:
        return json.loads(pathlib.Path(RT["pressure_flag"]).read_text())
    except Exception:
        return {"use_cloud_tasks": False, "pressure_pct": 0}


def resolve_routing(model: str) -> tuple[str, str, dict]:
    cloud_headers = {
        "Authorization": f"Bearer {os.environ.get('OLLAMA_API_KEY', '')}"
    }

    if model.startswith(RT["jarvis_cloud_prefix"]):
        if _hermes_active():
            _log_route("jarvis-cloud-blocked-by-hermes", model, RT["jarvis_cloud_fallback"])
            return "tasks", RT["jarvis_cloud_fallback"], {}
        if _jarvis_concurrent() >= 2:
            _log_route("jarvis-cloud-concurrent", model, RT["jarvis_cloud_model"])
            return "cloud", RT["jarvis_cloud_model"], cloud_headers
        _log_route("jarvis-cloud-single-instance-local", model, RT["jarvis_cloud_fallback"])
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
    with open(LOG_DIR / "routing.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def _log_lifeos_escalation(rec: dict) -> None:
    with open(LOG_DIR / "lifeos-escalations.jsonl", "a") as f:
        f.write(json.dumps({"ts": time.time(), **rec}) + "\n")


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
    return "\n".join(parts)


def lifeos_gate(model: str, sensitivity: str, body: dict) -> tuple[str, dict]:
    """
    Enforce LifeOS sensitivity + prefilter policy on `lifeos-cloud-*` requests.

    Returns (effective_model, log_extras). If policy blocks escalation, returns
    the local fallback model. Caller then feeds effective_model into normal routing.
    """
    prefixes = RT.get("lifeos_prefixes") or {}
    if model not in prefixes:
        return model, {}

    fallback = RT.get("lifeos_local_fallback")
    if sensitivity == "private":
        _log_lifeos_escalation({
            "skill_model": model, "decision": "refused-private",
            "sensitivity": sensitivity, "fallback": fallback,
        })
        _log_route("lifeos-refused-private", model, fallback,
                   extra={"sensitivity": sensitivity})
        return fallback, {"lifeos_refused": "private"}

    hits = _lifeos_scan(_extract_body_text(body))
    if hits:
        cats = _lifeos_summarize(hits)
        _log_lifeos_escalation({
            "skill_model": model, "decision": "refused-prefilter",
            "sensitivity": sensitivity, "hits": cats, "fallback": fallback,
        })
        _log_route("lifeos-refused-prefilter", model, fallback,
                   extra={"sensitivity": sensitivity, "hit_categories": cats})
        return fallback, {"lifeos_refused": "prefilter", "hits": cats}

    resolved = prefixes[model]
    _log_lifeos_escalation({
        "skill_model": model, "decision": "escalated",
        "sensitivity": sensitivity, "routed": resolved,
    })
    return resolved, {"lifeos_escalated_to": resolved}


def _log_kill(reason: str, meta: dict):
    if not CFG["logging"]["log_kills"]:
        return
    rec = {"ts": time.time(), "reason": reason, **meta}
    with open(LOG_DIR / "kills.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


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

    _inc(model)
    try:
        async with client.stream(method, path, json=body,
                                 headers=extra_headers or None) as upstream:
            async for chunk in upstream.aiter_bytes():
                yield chunk

                for line in chunk.split(b"\n"):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    piece = (obj.get("response") or
                             obj.get("message", {}).get("content", ""))
                    if piece:
                        new_toks = piece.split()
                        token_buf.extend(new_toks)
                        total_tokens += len(new_toks)

                now = time.time()
                elapsed = now - start

                if total_tokens > TH["max_tokens_per_request"]:
                    _log_kill("max_tokens", {"id": req_id, "tokens": total_tokens})
                    yield b'\n{"agentstop_terminated":"max_tokens"}\n'
                    await upstream.aclose()
                    return

                if elapsed > TH["max_wall_seconds"]:
                    _log_kill("max_wall_seconds", {"id": req_id, "elapsed": elapsed})
                    yield b'\n{"agentstop_terminated":"max_wall_seconds"}\n'
                    await upstream.aclose()
                    return

                ratio = _ngram_repeat_ratio(
                    token_buf, TH["ngram_window"], TH["ngram_size"]
                )
                if ratio > TH["ngram_overlap_ratio"]:
                    _log_kill("ngram_loop", {"id": req_id,
                                              "ratio": ratio, "tokens": total_tokens})
                    yield b'\n{"agentstop_terminated":"ngram_loop"}\n'
                    await upstream.aclose()
                    return

                # Stall detection only fires AFTER generation has started.
                # Zero-token elapsed time = model still cold-loading, not stalled.
                if total_tokens > 0 and elapsed > TH["stall_grace_s"]:
                    tps = total_tokens / elapsed if elapsed else 0
                    if tps < TH["min_tokens_per_second"]:
                        _log_kill("stall", {"id": req_id,
                                             "tps": tps, "elapsed": elapsed})
                        yield b'\n{"agentstop_terminated":"stall"}\n'
                        await upstream.aclose()
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


async def _rehydrate_ndjson(inner, mapping: dict):
    """Wrap an Ollama NDJSON byte stream, restoring redacted originals in assistant
    text before the caller sees them (§6, trusted-cloud path). A placeholder can split
    across chunks, so one RehydrateStream buffers across the whole stream; buffered tail
    is flushed onto the terminal frame."""
    reh = RehydrateStream(mapping)
    async for chunk in inner:
        for line in chunk.split(b"\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except Exception:
                yield line + b"\n"
                continue
            msg = obj.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                msg["content"] = reh.feed(msg["content"])
            elif isinstance(obj.get("response"), str):
                obj["response"] = reh.feed(obj["response"])
            if obj.get("done"):
                tail = reh.flush()
                if tail:
                    if isinstance(msg, dict):
                        msg["content"] = (msg.get("content") or "") + tail
                    else:
                        obj["response"] = (obj.get("response") or "") + tail
            yield (json.dumps(obj) + "\n").encode()


@app.post("/api/generate")
@app.post("/api/chat")
@app.post("/v1/chat/completions")
async def supervised(request: Request):
    body = await request.json()
    original_model = body.get("model", "")
    req_id = f"{int(time.time() * 1000)}-{id(body)}"

    # LifeOS special-case: lifeos-claude signals Pi to invoke `claude` CLI directly.
    if original_model == RT.get("lifeos_claude_marker"):
        return JSONResponse(
            {"error": "lifeos-claude is not routed through Deflector; spawn `claude` CLI on Pi host"},
            status_code=421,
        )

    # LifeOS gate: sensitivity + prefilter policy before normal routing.
    sensitivity = request.headers.get("x-lifeos-sensitivity", "").lower()
    gated_model, _lifeos_extras = lifeos_gate(original_model, sensitivity, body)
    if gated_model != original_model:
        body["model"] = gated_model

    cfg = get_config()
    claude_model = _claude_override(request)

    # Tentative destination for the privacy trust decision (Rule 1 needs a target).
    if claude_model:
        destination = ANTHROPIC
    else:
        _ck, _am, _eh = resolve_routing(gated_model)
        destination = OLLAMA_CLOUD if _ck == "cloud" else LOCAL

    # RULE 1 — privacy evaluation, absolute precedence (block > reroute > redact).
    decision = privacy_evaluate(body, destination, cfg)
    mapping: dict | None = None

    if isinstance(decision, Block):
        _log_route("privacy-block", original_model, "REJECT",
                   extra={"reason": decision.reason})
        return JSONResponse(
            {"error": f"blocked by privacy policy: {decision.reason}"},
            status_code=403,
        )

    if isinstance(decision, Reroute):
        # Strip restricted (Claude); re-resolve to local/trusted, re-run privacy once.
        _log_route("privacy-reroute", original_model, "local",
                   extra={"excluded": sorted(decision.exclude)})
        claude_model = None
        _ck, _am, _eh = resolve_routing(gated_model)
        destination = OLLAMA_CLOUD if _ck == "cloud" else LOCAL
        decision = privacy_evaluate(body, destination, cfg)
        if isinstance(decision, Block):
            return JSONResponse(
                {"error": f"blocked by privacy policy: {decision.reason}"},
                status_code=403,
            )

    if isinstance(decision, Redact):
        body = decision.body
        mapping = decision.mapping

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
        return StreamingResponse(
            _claude_guarded(body, claude_model, TH["max_wall_seconds"]),
            media_type="application/x-ndjson",
        )

    # RULES 3-4 / default — existing Ollama routing + kill-supervisor.
    client_key, actual_model, extra_headers = resolve_routing(gated_model)
    body["model"] = actual_model
    # Suppress qwen3 thinking on hermes lane — Hermes' tool-heavy prompts
    # burn tokens on CoT and leave content empty. Local only; cloud model unaffected.
    if actual_model == RT["hermes_model"]:
        body["think"] = False
    client = _clients[client_key]

    path = request.url.path

    if body.get("stream", True):
        inner = _supervised_stream(client, "POST", path, body,
                                   actual_model, req_id, extra_headers)
        if mapping:
            inner = _rehydrate_ndjson(inner, mapping)
        return StreamingResponse(inner, media_type="application/x-ndjson")

    _inc(actual_model)
    try:
        r = await client.post(path, json=body,
                              headers=extra_headers if extra_headers else {})
    finally:
        _dec(actual_model)
    resp = r.json()
    if mapping:
        reh = RehydrateStream(mapping)
        m = resp.get("message")
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            m["content"] = _rehydrate_text(m["content"], reh)
        elif isinstance(resp.get("response"), str):
            resp["response"] = _rehydrate_text(resp["response"], reh)
    return JSONResponse(resp, status_code=r.status_code)


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


@app.on_event("shutdown")
async def _close():
    for c in _clients.values():
        await c.aclose()
