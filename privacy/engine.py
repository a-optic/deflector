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

Tier B is block-only for `private_key` (its regex only anchors the PEM header, not
the key body, so redaction can't guarantee the whole key is masked); every other
Tier B detector redacts-and-proceeds, folded into the same per-slot redact loop as
Tier A/C.
"""

from __future__ import annotations

import copy
import json
import secrets
from dataclasses import dataclass, field

from privacy.config import LOCAL, PrivacyConfig
from privacy.tier_a import Hit, tier_a_match, tier_a_redact
from privacy.tier_b import tier_b_has_block, tier_b_redact, tier_b_scan
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

def _json_arg_setter(container, key, fn: dict, root):
    """Setter that writes one redacted string back into the parsed arguments and
    re-serialises the whole object, so every slot sharing `root` composes."""
    def _set(v):
        container[key] = v
        fn["arguments"] = json.dumps(root)
    return _set


def _json_string_slots(root, fn: dict):
    """(text, setter) for every string value nested anywhere in parsed tool-call
    arguments.

    Recurses rather than only walking the top level: a secret pasted into a tool
    call is just as likely to sit inside a nested object or a list element, and
    the previous whole-string scan did cover those. Dict KEYS are deliberately
    not exposed -- redacting one would rename a tool parameter and break the call
    schema, and a secret used as a parameter *name* is not a real shape here.
    """
    out = []

    def walk(node):
        if isinstance(node, dict):
            items = node.items()
        elif isinstance(node, list):
            items = enumerate(node)
        else:
            return
        for key, value in items:
            if isinstance(value, str):
                out.append((value, _json_arg_setter(node, key, fn, root)))
            else:
                walk(value)

    walk(root)
    return out


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
        # openai-completions-compat tool_calls: arguments is a JSON string and
        # routinely carries fetched page / file content -- same secret-shaped
        # risk as any other turn, so it needs to be scannable/redactable too.
        #
        # It must also still PARSE as JSON afterwards. Ollama Cloud rejects the
        # whole request with `400 invalid tool call arguments` when it does not,
        # and because the offending call stays in the conversation history, every
        # later turn carries it and fails identically -- an unrecoverable session,
        # from one redaction. So redact the string values *inside* the parsed
        # object and re-serialise, which cannot corrupt the envelope, instead of
        # rewriting the raw string and hoping the placeholder happens to be safe.
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function")
            if not (isinstance(fn, dict) and isinstance(fn.get("arguments"), str)):
                continue
            try:
                parsed = json.loads(fn["arguments"])
            except Exception:
                parsed = None
            if isinstance(parsed, (dict, list)):
                slots.extend(_json_string_slots(parsed, fn))
            else:
                # Not JSON at all (or a bare scalar) -- nothing to keep well-formed,
                # so fall back to redacting it as the plain string it already is.
                slots.append((fn["arguments"], lambda v, ff=fn: ff.__setitem__("arguments", v)))

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

    # --- Hard blocks first (win over everything), but only off-box ---
    #
    # Both block checks sit inside the is_cloud gate. A block exists to stop a
    # value LEAVING; on the local path nothing leaves, so a 403 there refuses a
    # request that was never a disclosure. That friction is not free: it is what
    # made `action: block` unusable for anything the operator actually needs to
    # discuss. Client names are the case in point -- SOUL.md says they must never
    # reach a cloud endpoint, which is exactly `block`, but under the old ordering
    # setting it would also have made client work impossible on the local models.
    #
    # Consequence, stated plainly: `ssn: block` no longer refuses a local request
    # containing an SSN. It still 403s every cloud destination, which is the only
    # place the value could have gone.
    a_hits = tier_a_match(text, cfg.redact_list)
    if is_cloud:
        if any(h.entry.action == "block" for h in a_hits):
            return Block(reason=_first_block_id(a_hits))      # 403, any cloud provider
        b_id = tier_b_has_block(tier_b_scan(text))
        if b_id:
            return Block(reason=f"secret:{b_id}")             # 403, any provider

    # --- Reroute: restricted destination carrying protected identity ---
    if is_cloud and cfg.is_restricted(destination) and a_hits:
        return Reroute(exclude=cfg.restricted)                # re-resolve routing

    # --- Redaction: trusted cloud destination ---
    #
    # Private addressing is left intact for a TRUSTED destination only. RFC1918
    # cannot identify anyone -- millions of networks use 10.10.1.x -- the external
    # IP is never shared, and the infrastructure sits behind a VPN, so the provider
    # does not see a real address at the network layer either. Masking it corrupted
    # prompts about this very stack for no privacy gain: 400 of 424 IPv4 tokens
    # (94%) across 15.6 MB of real sessions were private or loopback.
    #
    # Deliberately keyed on is_trusted() rather than is_cloud, so `unknown-remote`
    # -- a vendor with no classification -- keeps the strict behaviour. This is
    # is_trusted()'s first caller: until now the trusted list only suppressed a
    # startup warning and had no effect on any request.
    allow_private_ips = cfg.is_trusted(destination)
    if is_cloud:                                              # trusted (e.g. ollama-cloud)
        work = copy.deepcopy(body)
        mapping: dict = {}
        counters: dict = {}
        nonce = secrets.token_hex(3)
        changed = False
        for original, setter in _text_slots(work):
            hits = tier_a_match(original, cfg.redact_list)
            t = tier_a_redact(original, hits, mapping, nonce, counters) if hits else original
            b_hits = tier_b_scan(t)
            t = tier_b_redact(t, b_hits, mapping, nonce, counters) if b_hits else t
            t = tier_c_redact(t, mapping, nonce, counters,
                              allow_private_ips=allow_private_ips)
            if tier_d_enabled:
                t = tier_d_llm_rewrite(t)                     # residual, no reverse-map
            if t != original:
                setter(t)
                changed = True
        if changed or mapping:
            return Redact(body=work, mapping=mapping)

    return Proceed(body=body)                                 # local path or no hits
