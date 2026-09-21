# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The `lifeos-claude` marker must refuse without suggesting a bypass.

Nothing routes this marker through the Deflector yet -- there is no local
shim on the Pi host to spawn `claude` with tools enabled (see config.yaml's
comment on why it isn't offered in the Pi dropdown). The 421 used to say
"spawn `claude` CLI on Pi host", instructing the caller to route around the
gate below for a feature that doesn't exist. It still refuses; it no longer
suggests how to work around the refusal.

Offline. Run: .venv/bin/python -m pytest test_lifeos_claude_marker.py -q
"""

import pytest
from fastapi.testclient import TestClient

import main

MARKER = main.RT["lifeos_claude_marker"]


@pytest.fixture(autouse=True)
def no_real_upstream(monkeypatch):
    # An ordinary model falls through to a genuine dispatch; stub the
    # upstream call so this file never depends on a live Ollama instance.
    async def fake_stream(*args, **kwargs):
        yield b""
    monkeypatch.setattr(main, "_supervised_stream", fake_stream)


def _post(model: str):
    with TestClient(main.app) as c:
        return c.post("/v1/chat/completions", json={
            "model": model, "messages": [{"role": "user", "content": "hi"}]})


class TestStillRefuses:
    def test_the_marker_gets_421(self):
        assert _post(MARKER).status_code == 421

    def test_an_ordinary_model_is_unaffected(self):
        # Guards against a typo that makes the check fire on everything.
        assert _post("not-the-marker").status_code != 421


class TestNoLongerSuggestsABypass:
    def test_message_does_not_mention_spawning_claude(self):
        # The marker's own name unavoidably contains "claude" -- the thing
        # that had to go was the instruction to work around the gate.
        body = _post(MARKER).json()
        assert "spawn" not in body["error"].lower()
        assert "pi host" not in body["error"].lower()
