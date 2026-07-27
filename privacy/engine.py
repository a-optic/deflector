# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Privacy evaluation — Rule 1 (v4.0 §3).

Runs on every inbound request before a provider is committed. Four detector tiers
feed one decision. Outcome precedence is independent of tier numbering:

    block (403)  >  reroute  >  redact  >  proceed

Tiers B-D run only on the cloud path (destination != local). The local path runs
Tier A block-action entries only, so `action: block` values never reach even a local
model, and otherwise proceeds unredacted — local data never leaves the box.
"""

from __future__ import annotations

import copy
import secrets
from dataclasses import dataclass, field

from privacy.config import LOCAL, PrivacyConfig
from privacy.tier_a import Hit, tier_a_match, tier_a_redact
from privacy.tier_b import tier_b_scan
from privacy.tier_c import tier_c_redact
from privacy.tier_d import tier_d_llm_rewrite


# --- Decision types ------------------------------------------------------------

@dataclass
class Block:
    reason: str  # detector / entry id; safe to log (never the matched value)


@dataclass
class Reroute:
    exclude: frozenset[str]  # restricted providers to drop, then re-resolve routing


@dataclass
class Redact:
    body: dict
    mapping: dict = field(default_factory=dict)  # placeholder -> original, per-request


@dataclass
class Proceed:
    body: dict


Decision = Block | Reroute | Redact | Proceed


# --- Scannable-text plumbing ---------------------------------------------------

def _text_slots(body: dict):
    """Yield (text, setter) for every user/system content string in the body.

    Setters mutate `body` in place, so redaction can write placeholders back into the
    exact fields they came from without reserializing the whole request.
    """
    slots = []

    if isinstance(body.get("prompt"), str):
        slots.append((body["prompt"], lambda v, b=body: b.__setitem__("prompt", v)))

    if isinstance(body.get("system"), str):
        slots.append((body["system"], lambda v, b=body: b.__setitem__("system", v)))

    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            slots.append((c, lambda v, mm=m: mm.__setitem__("content", v)))
        elif isinstance(c, list):
            for seg in c:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    slots.append((seg["text"], lambda v, ss=seg: ss.__setitem__("text", v)))

    return slots


def _extract_scannable_text(body: dict) -> str:
    return "\n".join(t for t, _ in _text_slots(body))


def _first_block_id(a_hits: list[Hit]) -> str:
    for h in a_hits:
        if h.entry.action == "block":
            return h.entry.id
    return "unknown"


# --- Orchestration (§3.1) ------------------------------------------------------

def privacy_evaluate(body: dict, destination: str, cfg: PrivacyConfig,
                     tier_d_enabled: bool = False) -> Decision:
    text = _extract_scannable_text(body)
    is_cloud = cfg.is_cloud(destination)

    # --- Hard blocks first (win over everything) ---
    a_hits = tier_a_match(text, cfg.redact_list)
    if any(h.entry.action == "block" for h in a_hits):
        return Block(reason=_first_block_id(a_hits))          # 403, any destination
    if is_cloud:
        secret = tier_b_scan(text)
        if secret:
            return Block(reason=f"secret:{secret}")           # 403, any provider

    # --- Reroute: restricted destination carrying protected identity ---
    if is_cloud and cfg.is_restricted(destination) and a_hits:
        return Reroute(exclude=cfg.restricted)                # re-resolve routing

    # --- Redaction: trusted cloud destination ---
    if is_cloud:                                              # trusted (e.g. ollama-cloud)
        work = copy.deepcopy(body)
        mapping: dict = {}
        counters: dict = {}
        nonce = secrets.token_hex(3)
        changed = False
        for original, setter in _text_slots(work):
            hits = tier_a_match(original, cfg.redact_list)
            t = tier_a_redact(original, hits, mapping, nonce, counters) if hits else original
            t = tier_c_redact(t, mapping, nonce, counters)
            if tier_d_enabled:
                t = tier_d_llm_rewrite(t)                     # residual, no reverse-map
            if t != original:
                setter(t)
                changed = True
        if changed or mapping:
            return Redact(body=work, mapping=mapping)

    return Proceed(body=body)                                 # local path or no hits
