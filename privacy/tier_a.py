# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Tier A — operator static list (v4.0 §3.2).

Highest-trust tier: exact values the operator declared. Zero false negatives on
the declared set; breadth is controlled by adding aliases, never loose matching.

Compilation: all `values` across all entries -> one case-insensitive alternation
regex, longest-first (so "Jane Doe" matches before the "J. Doe" alias),
boundary-guarded so a value never matches inside a larger word. Patterns and input
are both NFKC-normalized and whitespace-collapsed, so "Jane  Doe" or a newline
split still matches.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class CompiledEntry:
    id: str
    placeholder: str
    action: str  # "auto" | "block"
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class Hit:
    entry: CompiledEntry
    spans: list[tuple[int, int]]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s)


def compile_tier_a(entries) -> list[CompiledEntry]:
    compiled: list[CompiledEntry] = []
    for e in entries:
        # Blank/whitespace-only values are dropped rather than compiled. A blank
        # contributes an EMPTY branch to the alternation below, and
        # `(?<!\w)(?:...|)(?!\w)` then matches zero-width at essentially every
        # position in every scanned request -- observed in production as a
        # redact-list entry with one blank value peppering every cloud-bound
        # prompt with placeholders that map to "", which in turn fed Tier B's
        # entropy detector enough placeholder-dense text to re-wrap whole runs
        # and corrupt the prompt. An operator typo must not be able to do that.
        vals = sorted(
            {n for v in e.get("values", []) if (n := _norm(str(v)).strip())},
            key=len, reverse=True,
        )
        if not vals:
            continue
        alt = "|".join(re.escape(v) for v in vals)
        pat = re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE)
        compiled.append(CompiledEntry(
            id=e["id"],
            placeholder=e["placeholder"],
            action=e.get("action", "auto"),
            pattern=pat,
        ))
    return compiled


def tier_a_match(text: str, compiled) -> list[Hit]:
    """Match against the whitespace-collapsed, NFKC-normalized text.

    Spans index into `_norm(text)`, so redaction must operate on the same
    normalized string (see tier_a_redact).
    """
    norm = _norm(text)
    hits: list[Hit] = []
    for c in compiled:
        spans = [m.span() for m in c.pattern.finditer(norm)]
        if spans:
            hits.append(Hit(entry=c, spans=spans))
    return hits


def tier_a_redact(text: str, hits, mapping: dict, nonce: str,
                  counters: dict | None = None) -> str:
    """Replace matched spans with nonce-prefixed placeholders, descending offset so
    earlier spans keep their positions. Operates on the normalized text (spans came
    from tier_a_match, which normalizes).

    `counters` (placeholder -> count) may be shared across multiple fields of one
    request so placeholder numbering stays unique and monotonic per request."""
    norm = _norm(text)
    repls = sorted(
        ((s, e, h.entry) for h in hits for (s, e) in h.spans),
        key=lambda t: t[0], reverse=True,
    )
    if counters is None:
        counters = {}
    out = norm
    for start, end, entry in repls:
        if end <= start:
            # Zero-width match: substituting here would inject a placeholder
            # mapping to "" without removing any text -- pure corruption, and
            # unbounded (one per position). compile_tier_a now drops the blank
            # values that cause this, but never splice on one regardless.
            continue
        counters[entry.placeholder] = counters.get(entry.placeholder, 0) + 1
        ph = f"<PII_{nonce}_{entry.placeholder}_{counters[entry.placeholder]}>"
        mapping[ph] = norm[start:end]
        out = out[:start] + ph + out[end:]
    return out
