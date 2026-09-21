# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Routing must not escalate to a cloud model the upstream has refused.

`model_cooldown` has always recorded refusals, but `is_suppressed` was read
only by the /pi/models.json dropdown builder -- never by `resolve_routing`. So
a refused model was hidden from the operator's list and still routed to. The
410 case is the sharp one: the model is retired for good, so every escalation
past that point was a guaranteed failure with nothing in the UI to explain it.

The `*-auto` lanes are exactly the ones that can absorb this, because they name
a local model to fall back to.

Offline. Run: .venv/bin/python -m pytest test_routing_cooldown.py -q
"""

import time

import pytest

import main
import model_cooldown as mc

LANE = "lifeos-cloud-code-auto"
ENTRY = main.RT["local_cloud_reasoning"][LANE]
CFG = {402: 86400, 429: 86400, 410: "permanent"}


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    # Never touch the operator's real ~/.agentstop/model-cooldowns.json.
    monkeypatch.setattr(mc, "STATE_PATH", tmp_path / "model-cooldowns.json")
    monkeypatch.setattr(mc, "_cache", None)
    monkeypatch.setattr(mc, "_mtime", None)
    yield


def _over_threshold_body() -> dict:
    """A body big enough that the lane wants to escalate."""
    threshold = ENTRY["context_tokens"] * ENTRY["threshold_pct"] / 100
    # ~4 chars/token, doubled so the estimator's safety margin cannot land us
    # under the line and make the test vacuous.
    return {"messages": [{"role": "user", "content": "word " * int(threshold * 2)}]}


def _under_threshold_body() -> dict:
    return {"messages": [{"role": "user", "content": "hello"}]}


class TestPremise:
    """Guard the fixtures themselves: if the bodies stop straddling the
    threshold, every assertion below passes for the wrong reason."""

    def test_big_body_would_escalate_when_nothing_is_suppressed(self):
        key, model, _ = main.resolve_routing(LANE, _over_threshold_body())
        assert (key, model) == ("cloud", ENTRY["cloud_model"])

    def test_small_body_stays_local_regardless(self):
        _, model, _ = main.resolve_routing(LANE, _under_threshold_body())
        assert model == ENTRY["local_model"]


class TestSuppressedCloudModelKeepsTheLaneLocal:
    def test_retired_model_is_routed_around(self):
        # The pre-existing bug: hidden from the dropdown, still routed to.
        mc.record(ENTRY["cloud_model"], 410, "was retired at ...", CFG)
        key, model, _ = main.resolve_routing(LANE, _over_threshold_body())
        assert model == ENTRY["local_model"]
        assert key != "cloud"

    def test_quota_refusal_is_routed_around(self):
        mc.record(ENTRY["cloud_model"], 429, "session usage limit", CFG)
        _, model, _ = main.resolve_routing(LANE, _over_threshold_body())
        assert model == ENTRY["local_model"]

    def test_the_local_model_is_fully_resolved_not_just_named(self):
        # The branch recurses through resolve_routing, so the caller gets a
        # usable client key for the local tier rather than the lane's own name.
        mc.record(ENTRY["cloud_model"], 429, "limit", CFG)
        key, model, headers = main.resolve_routing(LANE, _over_threshold_body())
        assert key in ("main", "tasks")
        assert model == ENTRY["local_model"]
        assert not headers          # no cloud Authorization on a local route


class TestCooldownLapses:
    def test_timed_cooldown_releases_the_lane_back_to_cloud(self):
        mc.record(ENTRY["cloud_model"], 429, "limit", CFG)
        assert not mc.is_suppressed(ENTRY["cloud_model"], now=time.time() + 86401)
        # is_suppressed is what routing consults, so once it lapses the lane
        # escalates again with no further bookkeeping.
        mc.clear(ENTRY["cloud_model"])
        _, model, _ = main.resolve_routing(LANE, _over_threshold_body())
        assert model == ENTRY["cloud_model"]


class TestUnrelatedModelsAreUnaffected:
    def test_suppressing_a_different_model_does_not_hold_the_lane_local(self):
        mc.record("some-other:cloud", 410, "retired", CFG)
        _, model, _ = main.resolve_routing(LANE, _over_threshold_body())
        assert model == ENTRY["cloud_model"]
