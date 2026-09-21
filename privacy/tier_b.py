# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Tier B — secret detector (v4.0 §3.3).

Deterministic, sub-millisecond, all cloud destinations. Two detectors OR'd:
named patterns + Shannon entropy. Each detector has an `action`: "redact"
(mask the matched span with a placeholder and let the request proceed, same
placeholder/mapping scheme as Tier A/C) or "block" (caller must reject or
divert, never send). Only `private_key` is `action="block"` today -- its
regex anchors just the PEM header line, not the key body, so a redaction
can't guarantee the whole key is masked. Every other detector is a precisely
bounded match (a whole token/value), so redaction removes exactly what was
flagged.

The entropy path carries an operator allowlist for known-safe high-entropy tokens
(git SHAs, UUIDs) to keep false positives down, and excludes ordinary filesystem
paths -- which clear the raw shape trivially -- by decomposing them and re-testing
each segment, so a secret sitting at a path-like position is still caught.

Security invariant (unchanged): callers must log `hit.entry.id` only, never the
matched text. The `mapping` dict a caller builds during redaction legitimately
holds real values in memory for the request's lifetime -- same as Tier A/C
already do -- that's a different boundary than "never write it to a log".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretPattern:
    id: str
    pattern: "re.Pattern[str] | None"  # None for the entropy pseudo-detector
    action: str = "redact"             # "redact" | "block"


SECRET_PATTERNS: dict[str, SecretPattern] = {
    "private_key":   SecretPattern("private_key",
                          re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
                          action="block"),
    "aws_akid":      SecretPattern("aws_akid",
                          re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    "gh_token":      SecretPattern("gh_token",
                          re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    "slack_token":   SecretPattern("slack_token",
                          re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    "anthropic_key": SecretPattern("anthropic_key",
                          re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    "openai_key":    SecretPattern("openai_key",
                          re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    "jwt":           SecretPattern("jwt",
                          re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    "generic_kv":    SecretPattern("generic_kv", re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passwd|password|token|bearer)\b\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9/+._-]{12,}")),
}

_ENTROPY_ENTRY = SecretPattern("high_entropy", pattern=None, action="redact")

# Equivalent tokenization to the old `re.split(r"[\s'\"`]+", text)`, but as a
# positive match so offsets survive for redaction.
_TOKEN_RE = re.compile(r"[^\s'\"`]+")

# Known-safe high-entropy shapes verified against a local corpus during pre-flight.
_ENTROPY_ALLOWLIST: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE),                    # git SHA (hex only)
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
               r"[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE),           # UUID
)


@dataclass(frozen=True)
class Hit:
    entry: SecretPattern
    spans: list[tuple[int, int]]


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in
                (s.count(ch) for ch in set(s)))


def _entropy_shaped(tok: str) -> bool:
    """The raw secret shape: long, high-entropy, mixed character classes.

    Split out from `_high_entropy` so the path check below can ask it about an
    individual path SEGMENT without recursing back through the allowlists.
    """
    if len(tok) < 20 or _shannon(tok) < 4.0:
        return False
    classes = sum(bool(re.search(p, tok)) for p in
                  (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 3


def _allowlisted(tok: str) -> bool:
    return any(p.match(tok) for p in _ENTROPY_ALLOWLIST)


# Characters a human-written identifier stays inside. Anything beyond this set --
# and any digit -- says the run was drawn from a machine alphabet.
_IDENTIFIER_CHARS = re.compile(r"^[A-Za-z._-]+$")


def _segment_is_secret_shaped(seg: str) -> bool:
    """Whether one path segment looks like a secret rather than a name.

    Stricter than `_entropy_shaped` alone, because CamelCase source filenames
    clear that bar on letters and dots alone: `StopFailureHandler.hook.ts`
    measures 4.06 entropy across three character classes, indistinguishable by
    those metrics from a token. Entropy cannot separate them -- real filenames
    here run 4.02-4.21 and a live `ghp_` token measures 4.14, so any threshold
    that cleared the filenames would also clear the token.

    What does separate them is the alphabet. Secrets are drawn from base64, hex
    or token alphabets and so carry digits, or `+` `/` `=` padding. A CamelCase
    or snake_case name is letters, dots, dashes and underscores. So a segment
    made only of identifier characters is treated as a name.

    The residual risk is a secret that is 20+ characters of letters and
    punctuation with no digit at all -- roughly 0.8% of random base64 strings,
    and only reachable when it also sits inside a multi-segment path. Tier A and
    the named Tier B patterns are unaffected and still cover it; this narrows
    only the entropy backstop, and only within a path.
    """
    if not _entropy_shaped(seg) or _allowlisted(seg):
        return False
    return not _IDENTIFIER_CHARS.match(seg)


def _looks_like_path(tok: str) -> bool:
    """True for an ordinary filesystem path, false for anything hiding a secret.

    A path of any depth clears the raw entropy shape trivially -- mixed case,
    digits, dots, dashes and slashes over 20+ characters -- so every path in a
    cloud-routed prompt was being masked as a secret. That is not a cosmetic
    false positive: the agent's own tool calls are mostly paths, so `ls`, `cd`
    and `edit` arrived carrying placeholders instead of targets. 90 masked
    commands in one session on 2026-09-08.

    The test is deliberately NOT "does it contain a slash" -- base64 uses `/` in
    its alphabet, so that alone would let a secret through by accident. Instead
    decompose the path and re-apply the raw shape test to each SEGMENT: an
    ordinary path is a sequence of unremarkable, word-like names, whereas a
    secret is one dense high-entropy run. So this admits

        /Users/operator/.pi/LIFEOS/PULSE/modules/local-intelligence.ts

    while still flagging a secret that merely sits at a path-like position

        https://api.example.com/v1/Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MHF3ZXJ0
        Zm9vYmFy/YmF6cXV4MTIzNDU2Nzg5MHF3ZXJ0eXVpb3A+Kw==

    because the offending run is itself one segment. A segment that is a known-
    safe shape (git SHA, UUID) does not disqualify the path, which is what keeps
    ordinary content-addressed and UUID-named paths readable.

    Conservative by construction: a path carrying a genuinely random-looking
    segment stays masked. That is the right failure direction -- it costs
    legibility on one path, where the alternative risks leaking a secret.
    """
    if "/" not in tok:
        return False
    segments = [s for s in tok.split("/") if s]
    if len(segments) < 2:
        return False
    return not any(_segment_is_secret_shaped(s) for s in segments)


def _high_entropy(tok: str) -> bool:
    if not _entropy_shaped(tok):
        return False
    if _allowlisted(tok):
        return False
    if _looks_like_path(tok):
        return False
    return True


def tier_b_scan(text: str) -> list[Hit]:
    """Return every secret-shaped hit in `text`, grouped by detector, spans included.

    Empty list = clean. See module docstring for the log/mapping security
    invariant this function's callers must uphold.
    """
    hits: list[Hit] = []
    for sp in SECRET_PATTERNS.values():
        spans = [m.span() for m in sp.pattern.finditer(text)]
        if spans:
            hits.append(Hit(entry=sp, spans=spans))
    ent_spans = [m.span() for m in _TOKEN_RE.finditer(text) if _high_entropy(m.group())]
    if ent_spans:
        hits.append(Hit(entry=_ENTROPY_ENTRY, spans=ent_spans))
    return hits


def tier_b_has_block(hits: list[Hit]) -> str | None:
    """Return the id of the first block-action detector present, else None."""
    for h in hits:
        if h.entry.action == "block":
            return h.entry.id
    return None


def _merge_spans(
    repls: list[tuple[int, int, SecretPattern]],
) -> list[tuple[int, int, SecretPattern]]:
    """Collapse overlapping spans from different detectors into one, so redaction
    never double-slices the same region (e.g. `generic_kv`'s `token: <value>` fully
    containing an `openai_key` match, or a JWT also tripping `high_entropy`). When a
    named-pattern hit overlaps a `high_entropy` hit, keep the named pattern's id --
    it's the more specific, more useful log signal."""
    ordered = sorted(repls, key=lambda t: (t[0], -(t[1] - t[0])))
    merged: list[tuple[int, int, SecretPattern]] = []
    for start, end, entry in ordered:
        if merged and start < merged[-1][1]:
            ps, pe, pentry = merged[-1]
            keep = pentry if pentry.id != "high_entropy" else entry
            merged[-1] = (ps, max(pe, end), keep)
        else:
            merged.append((start, end, entry))
    return merged


def tier_b_redact(text: str, hits: list[Hit], mapping: dict, nonce: str,
                  counters: dict | None = None) -> str:
    """Replace redact-action spans with nonce-prefixed placeholders, same scheme as
    `tier_a.tier_a_redact`. Block-action hits (private_key) must be handled by the
    caller via `tier_b_has_block` before this is ever reached -- this function
    silently skips them if passed in anyway, it never redacts a block-action hit."""
    if counters is None:
        counters = {}
    flat = [(s, e, h.entry) for h in hits for (s, e) in h.spans if h.entry.action == "redact"]
    merged = _merge_spans(flat)
    out = text
    for start, end, entry in sorted(merged, key=lambda t: t[0], reverse=True):
        ph_cat = entry.id.upper()
        counters[ph_cat] = counters.get(ph_cat, 0) + 1
        ph = f"<PII_{nonce}_{ph_cat}_{counters[ph_cat]}>"
        mapping[ph] = text[start:end]
        out = out[:start] + ph + out[end:]
    return out
