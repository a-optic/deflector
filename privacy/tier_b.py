# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Tier B — secret hard-block (v4.0 §3.3).

Deterministic, sub-millisecond, all cloud destinations. Two detectors OR'd:
named patterns + Shannon entropy. Any hit -> caller returns 403. Log the detector
name only, never the matched value.

The entropy path carries an operator allowlist for known-safe high-entropy tokens
(git SHAs, UUIDs) to keep false positives down.
"""

from __future__ import annotations

import math
import re

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "private_key":   re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    "aws_akid":      re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "gh_token":      re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "slack_token":   re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "openai_key":    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "jwt":           re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "generic_kv":    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passwd|password|token|bearer)\b\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9/+._-]{12,}"
    ),
}

# Known-safe high-entropy shapes verified against a local corpus during pre-flight.
_ENTROPY_ALLOWLIST: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE),                    # git SHA (hex only)
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
               r"[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE),           # UUID
)


def _shannon(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in
                (s.count(ch) for ch in set(s)))


def _high_entropy(tok: str) -> bool:
    if len(tok) < 20 or _shannon(tok) < 4.0:
        return False
    if any(p.match(tok) for p in _ENTROPY_ALLOWLIST):
        return False
    classes = sum(bool(re.search(p, tok)) for p in
                  (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return classes >= 3


def tier_b_scan(text: str) -> str | None:
    """Return the detector name of the first secret found, else None."""
    for name, pat in SECRET_PATTERNS.items():
        if pat.search(text):
            return name
    for tok in re.split(r"[\s'\"`]+", text):
        if _high_entropy(tok):
            return "high_entropy"
    return None
