# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""A cloud tier answering with nothing gets cooled down; a local one does not.

Without the cooldown, every turn re-escalates into the dead endpoint and pays
a full cloud_read_timeout_s before it can fall back. That is what made the
2026-09-08 outage unusable rather than merely degraded: six requests, 60s
each, and the client retried the identical prompt until the repeat-guard
rejected it.

The two gates are what this file is really about. `client is _clients["cloud"]`
keeps a local timeout from suppressing the very tier the fallback depends on.
`silent` keeps a tier that generated for minutes and then dropped from being
judged on one reset.

Offline. Run: .venv/bin/python -m pytest tests/test_cloud_silent_cooldown.py -q
"""

import asyncio

import pytest

import main
import model_cooldown as mc


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "STATE_PATH", tmp_path / "model-cooldowns.json")
    monkeypatch.setattr(mc, "_cache", None)
    monkeypatch.setattr(mc, "_mtime", None)
    yield


class _Stream:
    def __init__(self, exc, chunks, raise_on_open):
        self._exc, self._chunks, self._open = exc, chunks, raise_on_open
        self.status_code = 200

    async def __aenter__(self):
        if self._open:
            raise self._exc
        return self

    async def __aexit__(self, *e):
        return False

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c
        raise self._exc

    async def aclose(self):
        pass


class _Client:
    def __init__(self, exc, chunks=(), raise_on_open=True):
        self._a = (exc, list(chunks), raise_on_open)

    def stream(self, method, path, json=None, headers=None):
        return _Stream(*self._a)


def _drive(client, model="nemotron-3-super:cloud"):
    main.tracing.start("t-cool")

    async def go():
        return [c async for c in main._supervised_stream(
            client, "POST", "/api/chat", {"model": model}, model, "req-1", {})]

    return asyncio.run(go())


def _as_cloud(monkeypatch, client):
    """Make `client` the object identity `_clients["cloud"]` checks against."""
    monkeypatch.setitem(main._clients, "cloud", client)
    return client


class TestTheCloudGate:
    def test_silent_cloud_tier_is_cooled_down(self, monkeypatch):
        c = _as_cloud(monkeypatch, _Client(main.httpx.ReadTimeout("t")))
        _drive(c)
        assert mc.is_suppressed("nemotron-3-super:cloud")

    def test_silent_local_tier_is_not(self, monkeypatch):
        # The gate that matters most: suppressing a local model would take the
        # fallback target offline along with the tier it was covering for.
        _as_cloud(monkeypatch, _Client(main.httpx.ReadTimeout("t")))
        other = _Client(main.httpx.ReadTimeout("t"))       # not the cloud client
        _drive(other, model="qwen3.6:35b-a3b")
        assert not mc.is_suppressed("qwen3.6:35b-a3b")

    def test_connect_error_to_cloud_also_counts(self, monkeypatch):
        c = _as_cloud(monkeypatch, _Client(main.httpx.ConnectError("refused")))
        _drive(c)
        assert mc.is_suppressed("nemotron-3-super:cloud")


class TestTheSilenceGate:
    def test_failure_after_real_output_does_not_cool_down(self, monkeypatch):
        # The model just proved it works; one reset is not evidence it is down.
        c = _as_cloud(monkeypatch, _Client(
            main.httpx.ReadError("reset"),
            chunks=[b'{"message":{"content":"hi"},"done":false}\n'],
            raise_on_open=False))
        _drive(c)
        assert not mc.is_suppressed("nemotron-3-super:cloud")


class TestItUsesTheConfiguredWindow:
    def test_cooldown_lapses_after_the_configured_seconds(self, monkeypatch):
        import time
        c = _as_cloud(monkeypatch, _Client(main.httpx.ReadTimeout("t")))
        _drive(c)
        secs = main.CFG["upstream"]["cloud_silent_cooldown_s"]
        assert mc.is_suppressed("nemotron-3-super:cloud", now=time.time() + secs - 1)
        assert not mc.is_suppressed("nemotron-3-super:cloud", now=time.time() + secs + 1)


class TestEndToEndWithRouting:
    def test_a_silent_cloud_tier_holds_the_lane_local_next_turn(self, monkeypatch):
        """The whole point, joined up: the second turn does not stall at all."""
        lane = "lifeos-cloud-code-auto"
        entry = main.RT["local_cloud_reasoning"][lane]
        threshold = entry["context_tokens"] * entry["threshold_pct"] / 100
        body = {"messages": [{"role": "user", "content": "word " * int(threshold * 2)}]}

        # Turn 1 escalates, because nothing is suppressed yet.
        assert main.resolve_routing(lane, body)[1] == entry["cloud_model"]

        c = _as_cloud(monkeypatch, _Client(main.httpx.ReadTimeout("t")))
        _drive(c, model=entry["cloud_model"])

        # Turn 2 stays local, so it never opens a connection to the dead tier.
        assert main.resolve_routing(lane, body)[1] == entry["local_model"]
