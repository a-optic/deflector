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

import ipaddress

from privacy.rehydrate import PLACEHOLDER_RE

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


def _is_non_identifying_ip(token: str) -> bool:
    """True for an address that cannot identify anyone: RFC1918, loopback,
    link-local, CGNAT, multicast, reserved.

    Presidio does not make this distinction -- measured, `10.10.1.60`,
    `127.0.0.1` and `34.36.133.15` all score exactly 0.6. Across 15.6 MB of real
    session content, 400 of 424 IPv4 tokens (94%) were private or loopback, so
    the detector spends almost all of its effort masking addresses that millions
    of networks share.

    Parsing rather than pattern-matching on purpose: `10.10.1.60` is private and
    `10.10.1.600` is not an address at all, and only a parser gets both right.
    """
    try:
        ip = ipaddress.ip_address(token.strip())
    except ValueError:
        return False                       # not an address; leave it to the caller
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast)


def tier_c_redact(text: str, mapping: dict, nonce: str,
                  counters: dict | None = None,
                  allow_private_ips: bool = False) -> str:
    """Redact generic PII into nonce placeholders, writing into the shared mapping.
    No-op (returns text unchanged) when Presidio is unavailable.

    `counters` may be shared across fields of one request for unique numbering.

    `allow_private_ips` leaves RFC1918/loopback/link-local addresses in place. The
    caller sets it only for a TRUSTED destination -- see privacy_evaluate. It is
    off by default so an unclassified or restricted provider keeps the old
    behaviour without the caller having to remember."""
    if not available():
        return text
    results = sorted(
        _analyzer.analyze(text=text, entities=ENTITIES, language="en"),
        key=lambda r: r.start, reverse=True,
    )
    # Never redact inside a placeholder an earlier tier already wrote.
    #
    # Tier C runs on text Tiers A and B have already rewritten, and spaCy's NER
    # happily tags the INSIDE of a placeholder as an entity: measured,
    # `PII_a4c126_HIGH_ENTROPY_290` is a LOCATION at 0.85 confidence. The
    # brackets sit outside the match, so redacting it produced
    # `<<PII_a4c126_LOCATION_2>>` and stored the Tier B placeholder's own text
    # -- minus its brackets -- as the "original value".
    #
    # That is unrecoverable rather than merely untidy. Rehydration is a single
    # re.sub pass, so restoring the inner value reconstitutes
    # `<PII_a4c126_HIGH_ENTROPY_290>` in a region the substitution has already
    # moved past, and the caller receives a dead placeholder no later pass will
    # fix. It is also random per request: whether NER tags a given placeholder
    # depends on the nonce, which is secrets.token_hex(3).
    #
    # Dropping overlapping results rather than re-numbering them is right on
    # the merits too -- the span is a placeholder this box wrote, not user
    # data, so there is nothing there left to protect.
    protected = [m.span() for m in PLACEHOLDER_RE.finditer(text)]
    results = [r for r in results
               if not any(r.start < pe and ps < r.end for ps, pe in protected)]
    # Drop non-identifying addresses when the destination is trusted. Same shape
    # as the placeholder guard above: exclude a class of match that clears the
    # detector's shape test but carries no signal.
    if allow_private_ips:
        results = [r for r in results
                   if not (r.entity_type == "IP_ADDRESS"
                           and _is_non_identifying_ip(text[r.start:r.end]))]
    if counters is None:
        counters = {}
    out = text
    for r in results:
        counters[r.entity_type] = counters.get(r.entity_type, 0) + 1
        ph = f"<PII_{nonce}_{r.entity_type}_{counters[r.entity_type]}>"
        mapping[ph] = text[r.start:r.end]
        out = out[:r.start] + ph + out[r.end:]
    return out
