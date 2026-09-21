# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tier C must not redact inside a placeholder an earlier tier wrote.

Tier C runs on text Tiers A and B have already rewritten, and spaCy's NER
tags the INSIDE of a placeholder as an entity: `PII_a4c126_HIGH_ENTROPY_290`
scores 0.85 as a LOCATION. The angle brackets fall outside the match, so
redacting it produced `<<PII_a4c126_LOCATION_2>>` and stored the Tier B
placeholder's own text -- minus its brackets -- as the "original value".

That is unrecoverable, not untidy. Rehydration is one re.sub pass, so
restoring the inner value reconstitutes `<PII_a4c126_HIGH_ENTROPY_290>` in a
region the substitution has already moved past. The caller receives a dead
placeholder that no later pass will fix -- which is the `<PII_..._HIGH_ENTROPY_599>`
symptom that started this investigation.

It was random per request because whether NER tags a given placeholder depends
on the nonce, and the nonce is secrets.token_hex(3). That is why it surfaced as
a ~1-in-3 flake rather than a reproducible failure.

Needs Presidio. Skips cleanly without it, like the tier itself.
Run: .venv/bin/python -m pytest privacy/test_tier_c_placeholders.py -q

CREDENTIAL-SHAPED FIXTURES BELOW ARE DELIBERATE. They are meant to look like real
keys, because a fixture no scanner would flag proves nothing about a detector.
GitHub push protection blocks pushes containing them; that block is the system
working, and is resolved as "used in tests" -- never by sanitising the fixture.
Splitting the literal or swapping in an obviously-fake value weakens coverage
silently, since the suite still passes. See CONTRIBUTING.md, "Credential-shaped
test fixtures are deliberate".
"""

import pytest

from privacy.rehydrate import PLACEHOLDER_RE, rehydrate_complete
from privacy.tier_b import tier_b_redact, tier_b_scan
from privacy.tier_c import available, tier_c_redact

pytestmark = pytest.mark.skipif(not available(), reason="Presidio not installed")

BLOB = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MHF3ZXJ0eXVpb3A+Kw=="
# The nonce is load-bearing: this is the value observed producing the failure,
# and a different one may not trip NER at all.
NONCE = "a4c126"


def _b_then_c(text: str):
    mapping: dict = {}
    counters: dict = {}
    t = tier_b_redact(text, tier_b_scan(text), mapping, NONCE, counters)
    b_entries = len(mapping)
    t = tier_c_redact(t, mapping, NONCE, counters)
    return t, mapping, b_entries


class TestPlaceholdersSurviveTierC:
    def _text(self):
        # 291 entries so HIGH_ENTROPY_290 exists -- the span NER actually flags.
        return "\n".join(f"artifact {i}: {BLOB}" for i in range(291))

    def test_tier_c_adds_nothing_when_only_placeholders_remain(self):
        _, mapping, b_entries = _b_then_c(self._text())
        assert len(mapping) == b_entries, "Tier C redacted inside a placeholder"

    def test_no_doubled_brackets(self):
        out, _, _ = _b_then_c(self._text())
        assert "<<PII" not in out

    def test_one_rehydration_pass_is_enough(self):
        # The property that actually matters. Two passes always worked; the
        # caller only ever does one.
        text = self._text()
        out, mapping, _ = _b_then_c(text)
        once = rehydrate_complete(out, mapping)
        assert PLACEHOLDER_RE.findall(once) == []
        assert once == text

    def test_no_mapping_value_is_a_placeholder_body(self):
        # The shape that made this hard to spot: the stored value was
        # `PII_a4c126_HIGH_ENTROPY_290` WITHOUT brackets, so a check for
        # "does any value look like a placeholder" came back clean.
        _, mapping, _ = _b_then_c(self._text())
        for value in mapping.values():
            assert not value.startswith("PII_"), value


class TestTierCStillDoesItsJob:
    """The fix drops overlapping detections; it must not drop real ones."""

    def test_plain_pii_is_still_redacted(self):
        mapping: dict = {}
        src = "Contact Sarah Chen at sarah@example.com or 555-123-4567 in Denver."
        out = tier_c_redact(src, mapping, NONCE, {})
        assert "Sarah Chen" not in out and "sarah@example.com" not in out
        assert len(mapping) >= 3
        assert rehydrate_complete(out, mapping) == src

    def test_pii_adjacent_to_a_placeholder_is_still_redacted(self):
        # Only the placeholder span is protected, not the rest of the line.
        mapping: dict = {}
        out = tier_c_redact(
            f"token <PII_{NONCE}_HIGH_ENTROPY_1> belongs to Sarah Chen in Denver",
            mapping, NONCE, {})
        assert f"<PII_{NONCE}_HIGH_ENTROPY_1>" in out      # untouched
        assert "Sarah Chen" not in out                     # still caught
        assert "Denver" not in out
