# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Claude-override header tests: X-Deflector-Mode (new) with X-AgentStop-Mode (legacy)
backward-compat. Offline. Run: .venv/bin/python -m pytest test_headers.py -q
"""

from main import _claude_override


def _req(headers: dict):
    # main.py reads request.headers.get("x-...") with lowercase keys.
    lc = {k.lower(): v for k, v in headers.items()}
    return type("R", (), {"headers": lc})()


def test_new_header_routes():
    assert _claude_override(_req({"X-Deflector-Mode": "claude-haiku-4-5"})) == "claude-haiku-4-5"


def test_legacy_header_still_accepted():
    assert _claude_override(_req({"X-AgentStop-Mode": "claude-sonnet-5"})) == "claude-sonnet-5"


def test_new_header_wins_over_legacy():
    r = _req({"X-Deflector-Mode": "claude-haiku-4-5", "X-AgentStop-Mode": "claude-sonnet-5"})
    assert _claude_override(r) == "claude-haiku-4-5"


def test_opus_needs_optin_new():
    assert _claude_override(_req({"X-Deflector-Mode": "claude-opus-4-8"})) is None
    r = _req({"X-Deflector-Mode": "claude-opus-4-8", "X-Deflector-Opus": "1"})
    assert _claude_override(r) == "claude-opus-4-8"


def test_opus_optin_legacy():
    r = _req({"X-AgentStop-Mode": "claude-opus-4-8", "X-AgentStop-Opus": "1"})
    assert _claude_override(r) == "claude-opus-4-8"


def test_unknown_and_missing_fall_through():
    assert _claude_override(_req({})) is None
    assert _claude_override(_req({"X-Deflector-Mode": "gpt-4"})) is None
