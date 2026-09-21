# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Model cooldown: hide a model the upstream has refused, for the right length.

The distinction under test is that the refusals are NOT equivalent. Verbatim
from ollama.com: 402 "requires a subscription or extra usage" and 429 "you have
reached your session usage limit" are account state that clears; 410
"qwen3-coder:480b was retired at 2026-07-15" is permanent. A single timer for
all three would return a retired model to the dropdown every day forever.

Offline. Run: .venv/bin/python -m pytest test_model_cooldown.py -q
"""

import json
import time

import pytest

import model_cooldown as mc

CFG = {402: 86400, 429: 86400, 410: "permanent"}


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    # Never touch the operator's real ~/.agentstop/model-cooldowns.json.
    monkeypatch.setattr(mc, "STATE_PATH", tmp_path / "model-cooldowns.json")
    monkeypatch.setattr(mc, "_cache", None)
    monkeypatch.setattr(mc, "_mtime", None)
    yield


class TestDurationsDifferByStatus:
    def test_quota_refusal_expires(self):
        assert mc.record("m:cloud", 429, "session usage limit", CFG)
        assert mc.is_suppressed("m:cloud")
        assert not mc.is_suppressed("m:cloud", now=time.time() + 86401)

    def test_payment_refusal_expires(self):
        assert mc.record("m:cloud", 402, "requires a subscription", CFG)
        assert mc.is_suppressed("m:cloud", now=time.time() + 86399)
        assert not mc.is_suppressed("m:cloud", now=time.time() + 86401)

    def test_retirement_never_expires(self):
        # The whole point: a year later it is still gone.
        assert mc.record("qwen3-coder:480b-cloud", 410, "was retired at ...", CFG)
        assert mc.is_suppressed("qwen3-coder:480b-cloud",
                                now=time.time() + 365 * 86400)

    def test_unlisted_status_is_ignored(self):
        # A 500 is a blip, not a verdict about the model.
        assert mc.record("m:cloud", 500, "internal error", CFG) is False
        assert not mc.is_suppressed("m:cloud")


class TestPersistence:
    def test_survives_a_restart(self, tmp_path, monkeypatch):
        mc.record("m:cloud", 410, "retired", CFG)
        monkeypatch.setattr(mc, "_cache", None)   # simulate a fresh process
        monkeypatch.setattr(mc, "_mtime", None)
        assert mc.is_suppressed("m:cloud")

    def test_corrupt_state_does_not_suppress_everything(self, monkeypatch):
        mc.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        mc.STATE_PATH.write_text("{not json")
        monkeypatch.setattr(mc, "_cache", None)
        monkeypatch.setattr(mc, "_mtime", None)
        # Must fail towards "everything available", never "everything hidden".
        assert not mc.is_suppressed("m:cloud")
        assert mc.active() == {}

    def test_state_file_is_not_world_readable(self):
        mc.record("m:cloud", 410, "retired", CFG)
        assert oct(mc.STATE_PATH.stat().st_mode)[-3:] == "600"

    def test_written_state_is_valid_json(self):
        mc.record("m:cloud", 429, "limit", CFG)
        doc = json.loads(mc.STATE_PATH.read_text())
        assert doc["m:cloud"]["status"] == 429


class TestClearing:
    def test_clear_one(self):
        mc.record("a", 410, "x", CFG)
        mc.record("b", 410, "x", CFG)
        assert mc.clear("a") == 1
        assert not mc.is_suppressed("a")
        assert mc.is_suppressed("b")

    def test_clear_unknown_is_not_an_error(self):
        assert mc.clear("nope") == 0

    def test_clear_all(self):
        mc.record("a", 410, "x", CFG)
        mc.record("b", 402, "x", CFG)
        assert mc.clear() == 2
        assert mc.active() == {}


class TestDropdownIntegration:
    def test_suppressed_model_leaves_the_catalog(self):
        import main
        from fastapi.testclient import TestClient

        mc.record("qwen3-coder:480b-cloud", 410, "was retired", CFG)
        with TestClient(main.app) as c:
            ids = [m["id"] for m in
                   c.get("/pi/models.json").json()["providers"]["agentstop"]["models"]]
        assert "qwen3-coder:480b-cloud" not in ids
        assert "pi-qwen3.6-128k" in ids          # everything else still offered

    def test_routing_aliases_are_not_suppressed_with_their_target(self):
        # lifeos-cloud-* fall back to a local model, so they still work when
        # the cloud target is refusing -- hiding them would be wrong.
        import main
        from fastapi.testclient import TestClient

        mc.record("nemotron-3-super:cloud", 429, "limit", CFG)
        with TestClient(main.app) as c:
            ids = [m["id"] for m in
                   c.get("/pi/models.json").json()["providers"]["agentstop"]["models"]]
        assert "nemotron-3-super:cloud" not in ids
        assert "lifeos-cloud-code-auto" in ids


class TestSilentTierHasNoStatusToKeyOff:
    """On 2026-09-08 ollama.com accepted connections and returned zero bytes.

    `record` looks a status up in the refusal table, and there is no status
    here, so silence needs its own entry point rather than a fake number.
    """

    def test_silence_suppresses_for_the_given_window(self):
        assert mc.record_unavailable("m:cloud", 300, "ReadTimeout")
        assert mc.is_suppressed("m:cloud")
        assert mc.is_suppressed("m:cloud", now=time.time() + 299)
        assert not mc.is_suppressed("m:cloud", now=time.time() + 301)

    def test_origin_stays_distinguishable_in_the_state_file(self):
        mc.record_unavailable("m:cloud", 300, "ReadTimeout")
        entry = json.loads(mc.STATE_PATH.read_text())["m:cloud"]
        assert entry["status"] == "silent"
        assert entry["detail"] == "ReadTimeout"

    def test_never_permanent(self):
        # A tier being unreachable is a short-lived claim, unlike a 410.
        mc.record_unavailable("m:cloud", 300, "x")
        assert json.loads(mc.STATE_PATH.read_text())["m:cloud"]["until"] is not None

    def test_refuses_nonsense_input(self):
        assert not mc.record_unavailable("", 300, "x")
        assert not mc.record_unavailable("m:cloud", 0, "x")
        assert not mc.is_suppressed("m:cloud")

    def test_is_suppressed_does_not_care_which_origin(self):
        # The whole point of sharing the state file: every consumer treats a
        # silent tier and a refused one alike without branching.
        mc.record("a:cloud", 429, "limit", CFG)
        mc.record_unavailable("b:cloud", 300, "ReadTimeout")
        assert mc.is_suppressed("a:cloud") and mc.is_suppressed("b:cloud")
        assert set(mc.active()) == {"a:cloud", "b:cloud"}

    def test_a_later_real_refusal_overwrites_the_silent_entry(self):
        mc.record_unavailable("m:cloud", 300, "ReadTimeout")
        mc.record("m:cloud", 410, "was retired at ...", CFG)
        assert mc.is_suppressed("m:cloud", now=time.time() + 10**6)
