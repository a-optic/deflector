# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""OpenJarvis-labeled work must never reach cloud, under any condition.

`resolve_routing`'s `jarvis_cloud_prefix` branch used to escalate to cloud
when Hermes was idle and Jarvis had 2+ concurrent requests. OpenJarvis is the
harness client-side dispatch reserves for declared-private work, and neither
harness forwards `X-LifeOS-Sensitivity` -- so the Deflector had no way to know
whether an escalating request was actually private. The branch now always
returns the local fallback, unconditionally, regardless of Hermes activity or
Jarvis concurrency. Re-enabling escalation should be a reviewed code change,
not something that quietly reactivates itself.

Offline. Run: .venv/bin/python -m pytest tests/test_jarvis_cloud_disabled.py -q
"""

import pytest

import main

MODEL = main.RT["jarvis_cloud_prefix"] + "-anything"
FALLBACK = main.RT["jarvis_cloud_fallback"]


@pytest.fixture(autouse=True)
def isolate_active(monkeypatch):
    # `_active` is module-level state; a leaked count from another test
    # (or the concurrency conditions this suite exists to prove no longer
    # matter) must not leak into these assertions.
    monkeypatch.setattr(main, "_active", {})
    yield


class TestPremise:
    def test_fallback_is_configured_and_not_the_cloud_model(self):
        # If these ever collapse to the same value, the assertions below stop
        # distinguishing "stayed local" from "escalated."
        assert FALLBACK
        assert FALLBACK != main.RT["jarvis_cloud_model"]


class TestAlwaysStaysLocal:
    def test_hermes_idle_low_concurrency(self):
        key, model, headers = main.resolve_routing(MODEL, {})
        assert (key, model) == ("tasks", FALLBACK)
        assert not headers

    def test_hermes_idle_high_concurrency_used_to_escalate_here(self):
        main._inc(main.RT["jarvis_models"][0])
        main._inc(main.RT["jarvis_models"][0])
        key, model, headers = main.resolve_routing(MODEL, {})
        assert (key, model) == ("tasks", FALLBACK)
        assert not headers

    def test_hermes_active_also_stays_local(self):
        main._inc(main.RT["hermes_model"])
        key, model, headers = main.resolve_routing(MODEL, {})
        assert (key, model) == ("tasks", FALLBACK)
        assert not headers

    def test_never_returns_the_cloud_client_key(self):
        for _ in range(5):
            key, _, _ = main.resolve_routing(MODEL, {})
            assert key != "cloud"
