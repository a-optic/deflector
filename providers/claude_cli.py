# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Claude provider via the `claude` CLI (Claude Code) — v4.0 §5/§6, CLI transport.

The box has no ANTHROPIC_API_KEY; the operator authenticates through Claude Code
instead. So the "anthropic" destination is served by spawning `claude -p` rather than
calling the Messages API. This reshapes the spec's payload translation (§5) and SSE
transform (§6):

  §5 to_anthropic_body  -> build_prompt(): flatten the Ollama body to one text prompt.
  §6 SSE -> NDJSON       -> transform_line(): map claude stream-json events to Ollama
                            NDJSON frames.

Tools are DISABLED (empty allowlist, default permission-mode) so a client prompt is
answered as pure text — Claude Code's Bash/Edit/Write agent tools never fire, there
are no permission prompts, and there are no filesystem/shell side effects. It behaves
like a chat completion, which is what an Ollama-compatible endpoint promises.

Privacy note: "anthropic" is a RESTRICTED destination. A request carrying operator
static identity is rerouted OFF Claude by Rule 1 before it ever reaches here, so this
path normally has an empty rehydrate mapping. Rehydration is still wired for
uniformity and is a no-op when the mapping is empty.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from typing import AsyncIterator

from privacy.rehydrate import RehydrateStream

CLAUDE_BIN = shutil.which("claude") or "claude"

# X-Deflector-Mode header value -> claude CLI --model alias.
MODEL_ALIAS = {
    "claude-haiku-4-5": "haiku",
    "claude-sonnet-5":  "sonnet",
    "claude-opus-4-8":  "opus",
}

# Anthropic cost-tier output caps (§4 budget table), keyed by allowlisted model id.
OUTPUT_CAP = {
    "claude-haiku-4-5": 60_000,
    "claude-sonnet-5":  120_000,
    "claude-opus-4-8":  120_000,
}


def _join_segments(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(seg.get("text", "") for seg in content
                       if isinstance(seg, dict) and isinstance(seg.get("text"), str))
    return ""


DEFAULT_SYSTEM = "You are a helpful assistant. Answer directly and concisely."


def build_system(body: dict) -> str:
    """Collect the request's system content (top-level `system` + any system-role
    messages) to pass via --system-prompt, REPLACING Claude Code's ~12k-token default
    harness prompt. Falls back to a minimal default when the request has none."""
    parts: list[str] = []
    sys = body.get("system")
    if isinstance(sys, str) and sys.strip():
        parts.append(sys.strip())
    for m in body.get("messages") or []:
        if (m.get("role") or "").strip().lower() == "system":
            t = _join_segments(m.get("content")).strip()
            if t:
                parts.append(t)
    return "\n\n".join(parts) if parts else DEFAULT_SYSTEM


def build_prompt(body: dict) -> str:
    """Flatten user/assistant turns (+ legacy prompt) into one text transcript.
    System content is handled separately by build_system (proper system role), so it
    is NOT folded in here.

    A chat proxy over a single-prompt CLI: prior turns become tagged context and the
    final user turn is the live query.
    """
    parts: list[str] = []
    for m in body.get("messages") or []:
        role = (m.get("role") or "user").strip().lower()
        if role == "system":
            continue
        text = _join_segments(m.get("content")).strip()
        if text:
            parts.append(f"[{role.capitalize()}]\n{text}")
    if isinstance(body.get("prompt"), str) and body["prompt"].strip():
        parts.append(body["prompt"].strip())
    return "\n\n".join(parts)


def _frame(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def transform_line(line: str, state: dict, rehydrate: RehydrateStream) -> list[bytes]:
    """Map one claude stream-json line to zero+ Ollama NDJSON frames. Pure function;
    `state` is mutated to track dedupe/usage across the stream.

    Emitted Ollama shape mirrors /api/chat streaming:
      {"model":M,"message":{"role":"assistant","content":TXT},"done":false}
    reasoning rides on a `thinking` field so downstream can show or ignore it.
    """
    line = line.strip()
    if not line:
        return []
    try:
        ev = json.loads(line)
    except Exception:
        return []

    etype = ev.get("type")
    model = state.get("model", "")

    if etype == "assistant":
        out: list[bytes] = []
        for block in ev.get("message", {}).get("content", []) or []:
            bt = block.get("type")
            if bt == "text":
                txt = block.get("text", "")
                if not txt or txt in state["seen_text"]:
                    continue
                state["seen_text"].add(txt)
                state["out_chars"] += len(txt)
                out.append(_frame({"model": model,
                                   "message": {"role": "assistant",
                                               "content": rehydrate.feed(txt)},
                                   "done": False}))
            elif bt == "thinking":
                th = block.get("thinking", "")
                if not th or th in state["seen_text"]:
                    continue
                state["seen_text"].add(th)
                out.append(_frame({"model": model,
                                   "message": {"role": "assistant", "content": "",
                                               "thinking": rehydrate.feed(th)},
                                   "done": False}))
        return out

    if etype == "result":
        usage = ev.get("usage", {}) or {}
        tail = rehydrate.flush()
        frames: list[bytes] = []
        # If nothing streamed (e.g. tools-only turn), fall back to the final result text.
        if not state["seen_text"] and ev.get("result"):
            tail = rehydrate.feed(ev["result"]) + tail
        if tail:
            frames.append(_frame({"model": model,
                                  "message": {"role": "assistant", "content": tail},
                                  "done": False}))
        frames.append(_frame({"model": model, "done": True,
                              "prompt_eval_count": usage.get("input_tokens", 0),
                              "eval_count": usage.get("output_tokens", 0)}))
        state["done"] = True
        return frames

    # system/init, system/thinking_tokens, rate_limit_event, etc. -> not forwarded.
    return []


async def stream_claude(
    ollama_body: dict,
    model_id: str,
    mapping: dict | None = None,
    max_wall_seconds: float = 900.0,
) -> AsyncIterator[bytes]:
    """Spawn `claude -p`, translate its stream-json to Ollama NDJSON, enforce a wall /
    output-token budget, and kill the subprocess on breach with a synthetic terminal
    frame (§4 abort semantics adapted to a subprocess)."""
    alias = MODEL_ALIAS.get(model_id, "sonnet")
    cap = OUTPUT_CAP.get(model_id, 80_000)
    prompt = build_prompt(ollama_body)
    system = build_system(ollama_body)
    rehydrate = RehydrateStream(mapping or {})
    state = {"model": model_id, "seen_text": set(), "out_chars": 0, "done": False}

    # Harness-bloat strip (measured ~18k -> ~0.2k context tokens per request):
    #   --tools ""                              drop all built-in tool SCHEMAS (not just
    #                                           permission) -> pure text, no prompts
    #   --system-prompt <system>                REPLACE Claude Code's ~12k default prompt
    #   --exclude-dynamic-system-prompt-sections drop remaining dynamic sections
    # NOTE: --bare also strips, but breaks OAuth ("Not logged in"); do not use it.
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json", "--verbose",
        "--model", alias,
        "--tools", "",
        "--system-prompt", system,
        "--exclude-dynamic-system-prompt-sections",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    start = time.time()

    async def _terminate():
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

    try:
        async for raw in proc.stdout:
            for frame in transform_line(raw.decode(errors="replace"), state, rehydrate):
                yield frame
            if state["done"]:
                break

            if time.time() - start > max_wall_seconds:
                await _terminate()
                yield _frame({"done": True, "abort_reason": "wall_seconds"})
                return
            # ~4 chars/token heuristic for the live cap (real count arrives in result).
            if state["out_chars"] / 4 > cap:
                await _terminate()
                yield _frame({"done": True, "abort_reason": "budget"})
                return
    finally:
        if proc.returncode is None:
            await _terminate()
