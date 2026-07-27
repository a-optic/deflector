# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Tier C — Presidio PII redaction (v4.0 §3.4).

Trusted cloud path only. Microsoft Presidio (spaCy NER + built-in recognizers).
Runs AFTER Tier A, so operator-declared values are already placeholders and never
double-processed. Best-effort PII reduction, not a guarantee — the guarantee lives
in Tiers A and B.

Presidio is a heavy optional dependency. If it is not installed, Tier C degrades to
a no-op and the caller logs that PII reduction was unavailable — the request still
proceeds with Tier A + B protection intact. Install to enable:
    .venv/bin/pip install presidio-analyzer && python -m spacy download en_core_web_lg
"""

from __future__ import annotations

ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
            "US_SSN", "LOCATION", "IP_ADDRESS", "IBAN_CODE"]

_analyzer = None
_available: bool | None = None


def available() -> bool:
    """True iff Presidio imported and an analyzer engine initialized."""
    global _analyzer, _available
    if _available is not None:
        return _available
    try:
        from presidio_analyzer import AnalyzerEngine
        _analyzer = AnalyzerEngine()  # init is slow; cached module-level
        _available = True
    except Exception:
        _analyzer = None
        _available = False
    return _available


def tier_c_redact(text: str, mapping: dict, nonce: str,
                  counters: dict | None = None) -> str:
    """Redact generic PII into nonce placeholders, writing into the shared mapping.
    No-op (returns text unchanged) when Presidio is unavailable.

    `counters` may be shared across fields of one request for unique numbering."""
    if not available():
        return text
    results = sorted(
        _analyzer.analyze(text=text, entities=ENTITIES, language="en"),
        key=lambda r: r.start, reverse=True,
    )
    if counters is None:
        counters = {}
    out = text
    for r in results:
        counters[r.entity_type] = counters.get(r.entity_type, 0) + 1
        ph = f"<PII_{nonce}_{r.entity_type}_{counters[r.entity_type]}>"
        mapping[ph] = text[r.start:r.end]
        out = out[:r.start] + ph + out[r.end:]
    return out
