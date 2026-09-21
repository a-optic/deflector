# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""A dead upstream on `stream: false` must be an honest error, not a bare 500.

`_run` wrapped `client.post` in a try/finally with no `except`, so a transport
failure became an unhandled exception. Measured against the real service on
2026-09-08: the cloud lane returned "Internal Server Error" after exactly
60.1s, which says nothing about which tier failed or why, and reads to a
caller as "the proxy is broken" rather than "the provider is down".

Pi streams, so this is the quieter half of the same bug -- but leaving the two
paths disagreeing about what a dead upstream means is how the next
investigation gets sent down the wrong road.

Offline. Run: .venv/bin/python -m pytest test_non_streaming_transport.py -q
"""

import pytest
from fastapi.testclient import TestClient

import main
import model_cooldown as mc

CLOUD_LANE = "lifeos-cloud-code"          # resolves to a :cloud model


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "STATE_PATH", tmp_path / "model-cooldowns.json")
    monkeypatch.setattr(mc, "_cache", None)
    monkeypatch.setattr(mc, "_mtime", None)
    yield


class _DeadClient:
    """Stands in for the cloud httpx client: connects, never answers."""

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    async def post(self, path, json=None, headers=None):
        self.calls += 1
        raise self._exc

    async def aclose(self):
        # The app's shutdown handler closes every client in _clients.
        pass


def _post(monkeypatch, exc, model=CLOUD_LANE):
    dead = _DeadClient(exc)
    monkeypatch.setitem(main._clients, "cloud", dead)
    with TestClient(main.app) as c:
        r = c.post("/v1/chat/completions", json={
            "model": model, "stream": False,
            "messages": [{"role": "user", "content": "say hello"}],
        })
    return r, dead


def test_dead_upstream_is_a_502_not_a_500(monkeypatch):
    r, _ = _post(monkeypatch, main.httpx.ReadTimeout("timed out"))
    assert r.status_code == 502


def test_the_body_names_the_tier_and_the_failure(monkeypatch):
    # The 500 said nothing at all. A caller deciding whether to retry, and an
    # operator reading a report of this, both need to know which of the two it
    # was and where it happened.
    r, _ = _post(monkeypatch, main.httpx.ReadTimeout("timed out"))
    err = r.json()["error"]
    assert "ReadTimeout" in err
    assert "cloud" in err                 # the resolved model id carries the tier


def test_connect_error_is_handled_the_same_way(monkeypatch):
    r, _ = _post(monkeypatch, main.httpx.ConnectError("refused"))
    assert r.status_code == 502
    assert "ConnectError" in r.json()["error"]


def test_a_silent_cloud_tier_is_cooled_down_here_too(monkeypatch):
    # Otherwise the two paths would disagree about whether the tier is usable,
    # and a non-streaming caller would keep paying the timeout.
    _post(monkeypatch, main.httpx.ReadTimeout("t"))
    assert mc.active(), "no model was cooled down"


def test_it_does_not_silently_answer_from_a_different_model(monkeypatch):
    # Deliberately no local retry on this path: an explicitly-requested cloud
    # model has no implied second choice, and the streaming fallback exists
    # only for lanes that name one.
    r, dead = _post(monkeypatch, main.httpx.ReadTimeout("t"))
    assert dead.calls == 1
    assert "choices" not in r.json()
