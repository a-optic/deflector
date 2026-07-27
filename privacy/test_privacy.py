# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Privacy engine tests — v4.0 §7 Phase 2, items 1-8.
Run: .venv/bin/python -m pytest privacy/test_privacy.py -q
"""

import pytest

from privacy.config import LOCAL, PrivacyConfig
from privacy.tier_a import compile_tier_a, tier_a_match
from privacy.tier_b import tier_b_scan
from privacy import tier_c
from privacy.engine import Block, Proceed, Redact, Reroute, privacy_evaluate
from privacy.rehydrate import RehydrateStream
from private_ref import OPERATOR_NAMES

# Operator identity fixtures come from private_ref (real on-box, neutral placeholders on a
# public clone / CI) so no real name is hardcoded here. NAME is the canonical variant;
# tests that exercise name normalization assume a two-token "First Last" shape.
NAME = OPERATOR_NAMES[0]
_FIRST, _LAST = NAME.split()[0], NAME.split()[1]

ENTRIES = [
    {"id": "owner_name", "values": list(OPERATOR_NAMES),
     "placeholder": "PERSON", "action": "auto"},
    {"id": "ssn", "values": ["123-45-6789"], "placeholder": "US_SSN", "action": "block"},
]


def _cfg():
    return PrivacyConfig(
        trusted=frozenset(["ollama-cloud"]),
        restricted=frozenset(["anthropic"]),
        redact_list=compile_tier_a(ENTRIES),
    )


def _body(text: str) -> dict:
    return {"model": "x", "messages": [{"role": "user", "content": text}]}


# 1. Tier A block — action:block value 403s on every destination including local.
@pytest.mark.parametrize("dest", ["anthropic", "ollama-cloud", LOCAL])
def test_tier_a_block_all_destinations(dest):
    d = privacy_evaluate(_body("my ssn is 123-45-6789 ok"), dest, _cfg())
    assert isinstance(d, Block) and d.reason == "ssn"


# 2. Tier A reroute — declared name bound for Anthropic re-resolves off cloud.
def test_tier_a_reroute_restricted():
    d = privacy_evaluate(_body(f"call {NAME} please"), "anthropic", _cfg())
    assert isinstance(d, Reroute) and "anthropic" in d.exclude


# 3. Tier A redact+rehydrate — trusted cloud carries placeholder; mapping restores.
def test_tier_a_redact_trusted():
    d = privacy_evaluate(_body(f"email {NAME} today"), "ollama-cloud", _cfg())
    assert isinstance(d, Redact)
    out = d.body["messages"][0]["content"]
    assert NAME not in out
    ph = next(iter(d.map if hasattr(d, "map") else d.mapping))
    assert ph in out
    assert d.mapping[ph] == NAME


# 4. Precedence — leaked key + declared name bound for Claude -> Block (block>reroute).
def test_precedence_block_over_reroute():
    body = _body(f"{NAME} key sk-ant-abcdefghij0123456789xyz")
    d = privacy_evaluate(body, "anthropic", _cfg())
    assert isinstance(d, Block) and d.reason.startswith("secret:")


# 5. Tier A matching — double space / mixed case matches; superstring does not.
def test_tier_a_matching_normalization():
    c = compile_tier_a(ENTRIES)
    assert tier_a_match(f"ping {_FIRST.lower()}  {_LAST.lower()} now", c)  # double space, lowercase
    assert tier_a_match(f"{_FIRST}\n{_LAST}", c)                           # newline split
    assert not tier_a_match(f"{_FIRST}a {_LAST}son waved", c)              # superstring, no match


# 6. Tier B entropy — random secret 403s; git SHA on allowlist passes.
def test_tier_b_entropy():
    assert tier_b_scan("tok Zk7Qp2Rf9Xy4Lm1Nb8Vc3Wd6Ss0Aa") == "high_entropy"
    assert tier_b_scan("commit 9f3a1c2b4d5e6f708192a3b4c5d6e7f809a1b2c3") is None  # SHA
    assert tier_b_scan("uuid 550e8400-e29b-41d4-a716-446655440000") is None       # UUID


# 7. Tier C — Presidio redacts an email/phone not in the static list (skip if absent).
def test_tier_c_presidio():
    if not tier_c.available():
        pytest.skip("Presidio not installed")
    d = privacy_evaluate(_body("reach me at jane.doe@example.com"), "ollama-cloud", _cfg())
    assert isinstance(d, Redact)
    out = d.body["messages"][0]["content"]
    assert "jane.doe@example.com" not in out


# 8. Split-placeholder stream — placeholder split across two deltas rehydrates whole.
def test_split_placeholder_rehydrate():
    mapping = {"<PII_a7f3_PERSON_1>": NAME}
    r = RehydrateStream(mapping)
    out = r.feed("Hello <PII_a7f3_PER")
    out += r.feed("SON_1>, welcome")
    out += r.flush()
    assert out == f"Hello {NAME}, welcome"
    assert "<PII_" not in out
