# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Privacy engine tests — v4.0 §7 Phase 2, items 1-8.
Run: .venv/bin/python -m pytest privacy/tests/test_privacy.py -q

CREDENTIAL-SHAPED FIXTURES BELOW ARE DELIBERATE. They are meant to look like real
keys, because a fixture no scanner would flag proves nothing about a detector.
GitHub push protection blocks pushes containing them; that block is the system
working, and is resolved as "used in tests" -- never by sanitising the fixture.
Splitting the literal or swapping in an obviously-fake value weakens coverage
silently, since the suite still passes. See CONTRIBUTING.md, "Credential-shaped
test fixtures are deliberate".
"""

import json

import pytest

from privacy.config import LOCAL, PrivacyConfig
from privacy.tier_a import compile_tier_a, tier_a_match
from privacy.tier_b import _high_entropy, tier_b_has_block, tier_b_redact, tier_b_scan
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


# 1. Tier A block — action:block value 403s on every CLOUD destination.
@pytest.mark.parametrize("dest", ["anthropic", "ollama-cloud", "unknown-remote"])
def test_tier_a_block_every_cloud_destination(dest):
    d = privacy_evaluate(_body("my ssn is 123-45-6789 ok"), dest, _cfg())
    assert isinstance(d, Block) and d.reason == "ssn"


# 1b. ...but NOT on local. A block stops a value LEAVING; nothing leaves on the
# local path, so a 403 there refuses a request that was never a disclosure.
#
# This is what makes `action: block` usable for client names: SOUL.md says they
# must never reach a cloud endpoint, and under the old ordering setting that
# would also have made client work impossible on the operator's own models.
def test_tier_a_block_does_not_fire_on_local():
    d = privacy_evaluate(_body("my ssn is 123-45-6789 ok"), LOCAL, _cfg())
    assert not isinstance(d, Block), "a local request cannot disclose anything"
    assert isinstance(d, Proceed)
    # And it is genuinely untouched -- not blocked, not redacted.
    assert "123-45-6789" in d.body["messages"][0]["content"]


# 1c. Client context: the operator's own hard rule, now actually enforced.
#
# ~/.hermes/SOUL.md carries `PRIVACY RULES (HARD) — NEVER send to any cloud
# endpoint: ... Deloitte or client context`. Until 2026-09-10 nothing enforced it:
# the `employer` and `clients` entries existed but sat at `action: auto`, which
# only MASKS the name for a trusted provider and lets the request proceed.
#
# Hermetic on purpose -- this builds its own config rather than reading the
# operator's live redact-list, so what they happen to have filled in is not this
# file's business. (The same lesson as test_privacy_destination.py's startup tests.)
def _client_cfg():
    return PrivacyConfig(
        trusted=frozenset(["ollama-cloud"]),
        restricted=frozenset(["anthropic"]),
        redact_list=compile_tier_a([
            {"id": "clients", "placeholder": "ORGANIZATION", "action": "block",
             "values": ["Initech Consolidated"]},
        ]),
    )


@pytest.mark.parametrize("dest", ["ollama-cloud", "anthropic", "unknown-remote"])
def test_client_name_never_reaches_any_cloud_destination(dest):
    d = privacy_evaluate(_body("the Initech Consolidated review slipped"),
                         dest, _client_cfg())
    assert isinstance(d, Block) and d.reason == "clients"


def test_client_name_is_fine_on_local():
    # The whole point of pairing `block` with the local exemption: the operator
    # still has to be able to do the work somewhere.
    d = privacy_evaluate(_body("the Initech Consolidated review slipped"),
                         LOCAL, _client_cfg())
    assert isinstance(d, Proceed)
    assert "Initech Consolidated" in d.body["messages"][0]["content"]


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


# 4. Precedence — private key + declared name bound for Claude -> Block (block>reroute).
# (A leaked API token no longer blocks -- Tier B redacts everything except full
# private-key material now, see tests 9-12 below -- so precedence needs a fixture
# that's still actually a hard block to be testing what it says it tests.)
def test_precedence_block_over_reroute():
    body = _body(f"{NAME} key -----BEGIN OPENSSH PRIVATE KEY-----")
    d = privacy_evaluate(body, "anthropic", _cfg())
    assert isinstance(d, Block) and d.reason == "secret:private_key"


# 5. Tier A matching — double space / mixed case matches; superstring does not.
def test_tier_a_matching_normalization():
    c = compile_tier_a(ENTRIES)
    assert tier_a_match(f"ping {_FIRST.lower()}  {_LAST.lower()} now", c)  # double space, lowercase
    assert tier_a_match(f"{_FIRST}\n{_LAST}", c)                           # newline split
    assert not tier_a_match(f"{_FIRST}a {_LAST}son waved", c)              # superstring, no match


# 6. Tier B entropy — random secret flagged high_entropy; git SHA/UUID allowlisted.
def test_tier_b_entropy():
    hits = tier_b_scan("tok Zk7Qp2Rf9Xy4Lm1Nb8Vc3Wd6Ss0Aa")
    assert any(h.entry.id == "high_entropy" for h in hits)
    assert tier_b_scan("commit 9f3a1c2b4d5e6f708192a3b4c5d6e7f809a1b2c3") == []  # SHA
    assert tier_b_scan("uuid 550e8400-e29b-41d4-a716-446655440000") == []       # UUID


# 9. Tier B redact — non-entropy named secret on trusted cloud gets masked, not blocked.
def test_tier_b_redact_trusted():
    d = privacy_evaluate(_body("aws key AKIAABCDEFGHIJKLMNOP"), "ollama-cloud", _cfg())
    assert isinstance(d, Redact)
    out = d.body["messages"][0]["content"]
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    ph = next(iter(d.mapping))
    assert ph in out
    assert d.mapping[ph] == "AKIAABCDEFGHIJKLMNOP"


# 10. Tier B private_key stays a hard block on cloud destinations (locks in the
# operator's explicit decision so an accidental flip to "redact" gets caught).
@pytest.mark.parametrize("dest", ["anthropic", "ollama-cloud"])
def test_tier_b_private_key_still_blocks(dest):
    d = privacy_evaluate(_body("-----BEGIN RSA PRIVATE KEY-----"), dest, _cfg())
    assert isinstance(d, Block) and d.reason == "secret:private_key"


# 11. Tier B overlap merge — a generic_kv match containing an aws_akid match
# collapses to exactly one placeholder, not two overlapping/corrupted ones.
def test_tier_b_overlap_merge():
    hits = tier_b_scan("api_key: AKIAABCDEFGHIJKLMNOP")
    mapping: dict = {}
    out = tier_b_redact("api_key: AKIAABCDEFGHIJKLMNOP", hits, mapping, "n1")
    assert out.count("<PII_") == 1
    assert "AKIAABCDEFGHIJKLMNOP" not in out


# 12. Local destination — Tier B never runs at all, secret passes through untouched
# ("local data never leaves the box" invariant, unaffected by the redact change).
def test_tier_b_local_never_redacted():
    d = privacy_evaluate(_body("aws key AKIAABCDEFGHIJKLMNOP"), LOCAL, _cfg())
    assert isinstance(d, Proceed)
    assert "AKIAABCDEFGHIJKLMNOP" in d.body["messages"][0]["content"]


# 13. tier_b_has_block — unit-level: block only on private_key, not on other detectors.
def test_tier_b_has_block_unit():
    assert tier_b_has_block(tier_b_scan("-----BEGIN PGP PRIVATE KEY-----")) == "private_key"
    assert tier_b_has_block(tier_b_scan("aws key AKIAABCDEFGHIJKLMNOP")) is None


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


# 14-16. Blank values in the operator redact-list must never compile into a
# zero-width-matching pattern. Found in production: one entry with a blank
# value contributed an EMPTY branch to its alternation, so it matched at
# essentially every position of every cloud-bound request, spraying
# placeholders that mapped to "" and corrupting the prompt (which then gave
# Tier B's entropy detector enough placeholder-dense text to re-wrap runs).
_BLANK_ENTRY = [{"id": "home_address", "values": ["221B Baker Street", "   "],
                 "placeholder": "LOCATION", "action": "auto"}]


def test_blank_value_does_not_compile_to_zero_width():
    for c in compile_tier_a(_BLANK_ENTRY):
        assert not c.pattern.search("")
        m = c.pattern.search("nothing sensitive in this sentence at all")
        assert m is None or m.end() > m.start()


def test_blank_value_entry_still_matches_its_real_value():
    compiled = compile_tier_a(_BLANK_ENTRY)
    assert tier_a_match("mail it to 221B Baker Street today", compiled)


def test_entry_with_only_blank_values_is_dropped_entirely():
    assert compile_tier_a([{"id": "empty", "values": ["", "  "],
                            "placeholder": "X", "action": "auto"}]) == []


def test_clean_text_is_untouched_by_blank_valued_entry():
    cfg = PrivacyConfig(trusted=frozenset(["ollama-cloud"]),
                        restricted=frozenset(["anthropic"]),
                        redact_list=compile_tier_a(_BLANK_ENTRY))
    text = "My proxy is at host-a and the mini is host-b. Nothing secret here."
    d = privacy_evaluate(_body(text), "ollama-cloud", cfg)
    out = d.body["messages"][0]["content"] if isinstance(d, Redact) else text
    assert "<PII_" not in out
    assert out == text


# --- tool_call arguments must survive redaction as parseable JSON ----------------
# Regression cases for the incident these were written after: `arguments` was
# redacted as an opaque string, and Ollama Cloud answers `400 invalid tool call
# arguments` to anything that no longer parses. The bad call then lives in the
# conversation history, so every later turn fails identically -- the session is
# unrecoverable, from one redaction.

def _tool_body(arguments: str) -> dict:
    return {"model": "x", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "fetch", "arguments": arguments}}]},
    ]}


def _args_of(body: dict) -> str:
    return body["messages"][1]["tool_calls"][0]["function"]["arguments"]


def test_tool_call_arguments_redacted_and_still_valid_json():
    body = _tool_body(json.dumps({"note": f"ping {NAME} now", "depth": 3}))
    d = privacy_evaluate(body, "ollama-cloud", _cfg())
    assert isinstance(d, Redact)
    out = _args_of(d.body)
    parsed = json.loads(out)          # the whole point: still parses
    assert NAME not in out
    assert parsed["depth"] == 3       # non-string values untouched
    # Look the placeholder up BY VALUE rather than taking whichever key
    # happens to be first. Tier C (Presidio) is active in this environment and
    # can contribute additional mapping entries, and `next(iter(...))` would
    # then pick one that is not in `note` at all -- a test that fails for a
    # reason unrelated to what it is checking.
    ph = next(k for k, v in d.mapping.items() if v == NAME)
    assert ph in parsed["note"]


def test_tool_call_arguments_redacted_when_nested():
    body = _tool_body(json.dumps({"outer": {"items": [f"tell {NAME}"]}}))
    d = privacy_evaluate(body, "ollama-cloud", _cfg())
    assert isinstance(d, Redact)
    out = _args_of(d.body)
    assert NAME not in out
    assert NAME not in json.loads(out)["outer"]["items"][0]


def test_tool_call_arguments_still_block_on_secret():
    # Detection coverage must not regress: a hard-block value inside nested
    # arguments still has to 403 rather than slip through the new parse path.
    body = _tool_body(json.dumps({"outer": {"v": "ssn 123-45-6789"}}))
    d = privacy_evaluate(body, "ollama-cloud", _cfg())
    assert isinstance(d, Block) and d.reason == "ssn"


def test_tool_call_arguments_non_json_falls_back_to_string_redaction():
    body = _tool_body(f"not json, {NAME} here")
    d = privacy_evaluate(body, "ollama-cloud", _cfg())
    assert isinstance(d, Redact)
    assert NAME not in _args_of(d.body)


def test_tool_call_arguments_rehydrate_round_trip():
    body = _tool_body(json.dumps({"note": f"ping {NAME}"}))
    d = privacy_evaluate(body, "ollama-cloud", _cfg())
    ph = next(iter(d.mapping))
    assert RehydrateStream(d.mapping).feed(ph) == NAME


# 13. Tier B entropy — ordinary filesystem paths are not secrets.
#
# Regression origin: a path clears the raw entropy shape trivially (mixed case,
# digits, dots, dashes, slashes, well over 20 chars), so every path in a
# cloud-routed prompt was masked. The agent's own tool calls are mostly paths,
# so `ls`, `cd` and `edit` arrived carrying placeholders instead of targets --
# 90 masked commands in a single Pi session on 2026-09-08, and because `<` opens
# a redirection in bash they failed as parse errors that named nothing.
_REAL_PATHS = [
    # Usernames and project directories here are generic on purpose -- this file
    # is published. What the fixtures must preserve is SHAPE, not provenance:
    # a word-like home segment, and a directory carrying a trailing digit
    # (AI-Sandbox2), which is what pins the alphabet rule's tolerance for a
    # single digit in an otherwise word-like segment. See
    # test_tier_b_segment_alphabet_rule, where a digit-dense segment IS
    # secret-shaped -- these two cases sit either side of that boundary.
    "/Users/operator/.pi/LIFEOS/PULSE/modules/local-intelligence.ts",
    "/Users/operator/Documents/AI-Sandbox2/local-intelligence",
    "~/.agentstop/logs/requests-2026-09-08.jsonl",
    "node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
    "src/components/UserProfile/index.test.tsx",
    # CamelCase source filenames: 4.06 entropy over three character classes,
    # which no entropy threshold can separate from a real token (a live `ghp_`
    # token measures 4.14). The alphabet is what separates them.
    "./pai/Releases/v5.0.0/.claude/hooks/StopFailureHandler.hook.ts",
    "app/Handlers/RebuildArchSummary.ts",
]


def test_tier_b_entropy_allows_ordinary_paths():
    for p in _REAL_PATHS:
        assert tier_b_scan(p) == [], p


def test_tier_b_entropy_still_flags_secrets_at_path_positions():
    """The point of decomposing by segment rather than just looking for a slash.

    base64's alphabet includes `/`, so "contains a slash" would have let a raw
    secret through by accident. Each of these has one dense high-entropy run,
    and that run is what must still be caught."""
    for s in ("Zm9vYmFy/YmF6cXV4MTIzNDU2Nzg5MHF3ZXJ0eXVpb3A+Kw==",
              "https://api.example.com/v1/Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MHF3",
              "s3://my-bucket/backups/Zk7Qp2Rf9Xy4Lm1Nb8Vc3Wd6Ss0Aa",
              "/etc/creds/Zk7Qp2Rf9Xy4Lm1Nb8Vc3Wd6Ss0Aa"):
        hits = tier_b_scan(s)
        assert any(h.entry.id == "high_entropy" for h in hits), s


def test_tier_b_entropy_path_with_allowlisted_segment_still_allowed():
    # A UUID-named directory must not disqualify the whole path.
    assert tier_b_scan("/var/lib/objects/550e8400-e29b-41d4-a716-446655440000/blob") == []


def test_tier_b_bare_secret_unaffected_by_the_path_carve_out():
    # Guard against the carve-out widening: no slash, so it cannot apply at all.
    hits = tier_b_scan("Zk7Qp2Rf9Xy4Lm1Nb8Vc3Wd6Ss0Aa")
    assert any(h.entry.id == "high_entropy" for h in hits)


def test_tier_b_path_carve_out_needs_two_segments():
    # A single dense run with one trailing slash is not a path.
    assert _high_entropy("Zk7Qp2Rf9Xy4Lm1Nb8Vc3Wd6Ss0Aa/")


def test_named_patterns_still_win_inside_a_path():
    """The carve-out only silences the ENTROPY detector. A named secret sitting
    in a path is matched by its own pattern regardless of shape."""
    hits = tier_b_scan("/tmp/staging/AKIAABCDEFGHIJKLMNOP")
    assert any(h.entry.id == "aws_akid" for h in hits)


def test_tier_b_segment_alphabet_rule():
    """A path segment of letters and punctuation is a name; digits or base64
    padding mean it came from a machine alphabet."""
    from privacy.tier_b import _segment_is_secret_shaped as secret

    assert not secret("StopFailureHandler.hook.ts")
    assert not secret("RhetoricalFigures.md")
    assert secret("YmF6cXV4MTIzNDU2Nzg5MHF3ZXJ0eXVpb3A+Kw==")   # base64 padding
    assert secret("Zk7Qp2Rf9Xy4Lm1Nb8Vc3Wd6Ss0Aa")              # digits


def test_tier_b_path_carve_out_does_not_reach_bare_tokens():
    """The alphabet rule applies only INSIDE a multi-segment path. A bare
    letters-and-dots token with no slash must be unaffected by it."""
    assert _high_entropy("Kjh.Gfd.Sao.Poi.Uyt.Rew.Qmn.Bvc.Xza")
