# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
LifeOS sensitivity pre-filter.

Deterministic scan for private-data signals. Runs BEFORE any LLM call
that could escalate to cloud tier D/E. Zero LLM involvement — pure regex.

Hits classified by category. Any hit on a `personal`, `public`, or `mixed`
sensitivity payload MUST block escalation and force fallback to tier C.

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
