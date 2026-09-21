# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
LifeOS sensitivity pre-filter.

Deterministic scan for private-data signals. Runs BEFORE any LLM call
that could escalate to cloud tier D/E. Zero LLM involvement — pure regex.

Hits classified by category, each with a `has_block`/`redact` disposition.
Only `cred_ssh_priv` still forces a hard fallback to the local model, because
its regex anchors the PEM header rather than the key body, so masking the
match cannot guarantee the whole key is masked. Every other category --
including `ip_private` and `hostname_lab` -- is redacted with a placeholder
in place and the request still escalates to cloud. See BLOCK_CATEGORIES below
for the audit-log measurements behind that split.

Never called for `private` sensitivity (those never escalate at all).

Categories:
  ip_private      - RFC1918 IPs (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)
  hostname_lab    - internal subdomains of the configured internal domain
                    (private_ref.INTERNAL_DOMAIN; lab/vpn/homelab/internal)
  cred_jwt        - JWT tokens (eyJ...)
  cred_github_pat - GitHub personal access tokens (ghp_, gho_, ghu_, ghs_, ghr_)
  cred_aws        - AWS access keys (AKIA...)
  cred_slack      - Slack tokens (xox[abpr]-...)
  cred_openai     - OpenAI / Anthropic API key shapes (sk-..., sk-ant-...)
  cred_ssh_priv   - SSH private key headers
  cred_base64_lg  - suspicious base64 blobs > 20 chars in secret-shaped contexts
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from private_ref import INTERNAL_DOMAIN


@dataclass(frozen=True)
class Hit:
    category: str
    match: str
    start: int
    end: int


# Compiled once at import.
_PATTERNS: dict[str, re.Pattern[str]] = {
    # RFC1918 private ranges. Word-boundary anchored to reduce false-positives
    # on version strings like 10.10.10.10.10 (rare) or 192.168.x.x hostnames.
    "ip_private": re.compile(
        r"\b("
        r"10\.(?:\d{1,3}\.){2}\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r")\b"
    ),
    # Internal subdomains (lab/vpn/homelab/internal) of the configured internal domain.
    # Domain comes from private_ref so no operator-specific value is baked into the engine.
    "hostname_lab": re.compile(
        r"\b([a-z0-9-]+\.)*(lab|vpn|homelab|internal)\." + re.escape(INTERNAL_DOMAIN) + r"\b",
        re.IGNORECASE,
    ),
    # JWT: three base64url segments separated by dots. Header always starts eyJ.
    "cred_jwt": re.compile(
        r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"
    ),
    "cred_github_pat": re.compile(
        r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"
    ),
    "cred_aws": re.compile(
        r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"
    ),
    "cred_slack": re.compile(
        r"\bxox[abpsr]-[A-Za-z0-9-]{10,}\b"
    ),
    # sk-ant-... (Anthropic) or sk-... (OpenAI-style). Length >= 20 to reduce noise.
    "cred_openai": re.compile(
        r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b"
    ),
    "cred_ssh_priv": re.compile(
        r"-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY-----"
    ),
    # Suspicious long base64 blobs following "secret", "token", "key", "password"
    # within ~24 chars. Catches leaked env-var-like blobs while ignoring random b64.
    "cred_base64_lg": re.compile(
        r"(?:secret|token|key|password|passwd|auth)[\"'\s:=]{1,24}"
        r"([A-Za-z0-9+/=_-]{20,})",
        re.IGNORECASE,
    ),
}


def scan(text: str) -> list[Hit]:
    """Return every hit found in `text`. Empty list = safe to escalate."""
    hits: list[Hit] = []
    for category, pattern in _PATTERNS.items():
        for m in pattern.finditer(text):
            # For cred_base64_lg the capture group is the blob; report just that.
            span = m.span(1) if pattern.groups >= 1 and category == "cred_base64_lg" else m.span()
            match_text = text[span[0]:span[1]]
            hits.append(Hit(category=category, match=match_text, start=span[0], end=span[1]))
    return hits


def is_safe(text: str) -> bool:
    """True iff no hits. Convenience wrapper."""
    return not scan(text)


def summarize(hits: Iterable[Hit]) -> dict[str, int]:
    """Count hits per category. For audit log."""
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.category] = counts.get(h.category, 0) + 1
    return counts


# The one category that stays a hard fallback-to-local trigger rather than
# being redacted: cred_ssh_priv's regex only anchors the PEM header, not the
# key body, so masking the match cannot guarantee the whole key is masked
# (same reasoning as privacy/tier_b.py's private_key). Everything else is
# redacted-and-escalated.
#
# ip_private and hostname_lab used to be blocking too, on the theory that a
# masked internal address still leaks "this touches a home network". Measured
# against the actual audit log, that theory cost far more than it bought:
# of 645 prefilter refusals, 642 (99.5%) were triggered by ip_private ALONE
# with no credential anywhere in the request, and only 3 involved a real
# credential -- each of which also carried a credential-shaped hit that would
# have been caught on its own. Net effect was a ~98% refusal rate that made
# the cloud lanes unreachable for ordinary homelab work, while the operator
# had already accepted redact-and-send for API keys, JWTs and GitHub tokens
# -- strictly higher-value secrets than an RFC1918 address that is the same
# handful of values on every home network. Blocking the weak signal while
# redacting the strong one was the inconsistency; these now redact.
BLOCK_CATEGORIES: frozenset[str] = frozenset({"cred_ssh_priv"})


def has_block(hits: Iterable[Hit]) -> str | None:
    """Return the category of the first block-disposition hit, else None."""
    for h in hits:
        if h.category in BLOCK_CATEGORIES:
            return h.category
    return None


def _merge_hits(hits: list[Hit]) -> list[Hit]:
    """Collapse overlapping spans from different categories into one, so redaction
    never double-slices the same region (cred_base64_lg can overlap cred_jwt/
    cred_aws/cred_openai on the same labeled blob)."""
    ordered = sorted(hits, key=lambda h: (h.start, -(h.end - h.start)))
    merged: list[Hit] = []
    for h in ordered:
        if merged and h.start < merged[-1].end:
            p = merged[-1]
            merged[-1] = Hit(category=p.category, match=p.match,
                              start=p.start, end=max(p.end, h.end))
        else:
            merged.append(h)
    return merged


def redact(text: str, hits: list[Hit], mapping: dict, nonce: str,
          counters: dict | None = None) -> str:
    """Replace redactable spans with nonce-prefixed placeholders -- same scheme as
    privacy/tier_a.py's tier_a_redact. Hits whose category is in BLOCK_CATEGORIES
    are skipped; callers must handle those via has_block() before calling this.

    Placeholders are value-stable within a request: the same matched string
    always gets the same placeholder, rather than a fresh number per
    occurrence. This matters most for the category that dominates real
    traffic -- internal IPs, ~5.4 per refused request historically, and the
    same host is typically named many times across a conversation. Numbering
    per-occurrence would hand the model
    `<PII_x_IP_PRIVATE_1>`/`<PII_x_IP_PRIVATE_7>`/`<PII_x_IP_PRIVATE_12>` for
    one machine and invite it to reason about a network of distinct hosts
    that does not exist. `mapping` is shared across every text slot of a
    request, so the reverse index derived from it keeps placeholders stable
    across slots too, not just within one.

    Keyed on the matched value alone rather than (category, value): if one
    string somehow matched two categories its placeholder keeps whichever
    category was seen first, which is cosmetic -- rehydration is by
    placeholder and stays correct either way.
    """
    if counters is None:
        counters = {}
    redactable = [h for h in hits if h.category not in BLOCK_CATEGORIES]
    merged = _merge_hits(redactable)
    seen = {v: k for k, v in mapping.items()}
    out = text
    for h in sorted(merged, key=lambda h: h.start, reverse=True):
        ph = seen.get(h.match)
        if ph is None:
            ph_cat = h.category.upper()
            counters[ph_cat] = counters.get(ph_cat, 0) + 1
            ph = f"<PII_{nonce}_{ph_cat}_{counters[ph_cat]}>"
            mapping[ph] = h.match
            seen[h.match] = ph
        out = out[:h.start] + ph + out[h.end:]
    return out
