# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""What a `*-auto` lane compacts down to when it answers locally.

The budget is the local window MINUS room for the reply. Ollama's context
covers prompt and generation together, so a prompt that fills the window leaves
the answer nowhere to go, and Ollama makes room by silently dropping the OLDEST
messages -- the same loss compaction exists to manage, taken blindly and logged
nowhere.

It is deliberately NOT the lane's 45% escalation threshold. Measured on
qwen3.6:35b-a3b with an 83,030 token agent transcript:

    answer it directly            155.7s  (148.5s of that is prefill)
    summarize it, then answer     204.6s  (175.2s + 29.4s)

31% slower, every turn, because Pi resends full history and no summary is
cached. Prefill dominates and the summarizer must prefill the whole fold, so
summarize+answer cannot beat answer alone at any target value. Compaction is a
fit mechanism, not a speed one -- these tests pin that decision so it is not
quietly re-litigated into a per-turn tax.

Offline. Run: .venv/bin/python -m pytest test_local_fallback_target.py -q
"""

import main

LANES = main.RT["local_cloud_reasoning"]


class TestItLeavesRoomForTheReply:
    def test_target_is_the_window_minus_the_reserve(self):
        entry = {"context_tokens": 131072}
        reserve = main.RT["local_reply_reserve_tokens"]
        assert main._local_fallback_target(entry) == 131072 - reserve

    def test_every_configured_lane_gets_a_usable_budget(self):
        for name, entry in LANES.items():
            target = main._local_fallback_target(entry)
            assert 0 < target < entry["context_tokens"], name

    def test_a_lane_may_override_the_reserve(self):
        entry = {"context_tokens": 131072, "reply_reserve_tokens": 1000}
        assert main._local_fallback_target(entry) == 130072

    def test_never_returns_a_nonpositive_budget(self):
        # A misconfigured reserve larger than the window must not produce a
        # target that makes every prompt "too big" forever.
        assert main._local_fallback_target(
            {"context_tokens": 1000, "reply_reserve_tokens": 99999}) >= 1


class TestItIsNotTheEscalationThreshold:
    """The measurement said compacting this hard costs 31% per turn."""

    def test_target_is_well_above_the_45_percent_threshold(self):
        for name, entry in LANES.items():
            threshold = entry["context_tokens"] * entry.get("threshold_pct", 75) / 100
            assert main._local_fallback_target(entry) > threshold, name

    def test_a_typical_escalating_prompt_is_not_compacted(self):
        # ~90k tokens is what a real Pi session looked like when it escalated.
        # It fits the local window alongside a reply, so it must pass through
        # untouched rather than paying a summarizer hop.
        for name, entry in LANES.items():
            assert 90_000 < main._local_fallback_target(entry), name

    def test_a_prompt_that_would_overflow_the_window_is_compacted(self):
        # The case compaction genuinely exists for: prompt plus reply exceeds
        # what the model can hold, so something must give and it should be a
        # deliberate summary rather than Ollama's silent truncation.
        for name, entry in LANES.items():
            assert entry["context_tokens"] - 1 > main._local_fallback_target(entry), name


class TestReserveMatchesTheModelThatWillAnswer:
    """Each lane must reserve room for the reply ITS OWN local model advertises.

    A single global reserve was wrong in both directions. `pi-qwen3.6-128k`
    advertises 32000 maxTokens, so 16384 would have let a 114,688-token prompt
    plus a full-size reply overflow 131,072 and be silently truncated by Ollama.
    Raising the global value to cover it instead would make the code lane, whose
    model advertises 16000, compact earlier than it needs to -- and compaction
    costs a whole summarizer prefill every turn.
    """

    def _catalogue(self):
        return {m["id"]: m
                for p in main.CFG["routing"]["pi_clients"].values()
                for m in (p.get("models") or [])}

    def test_each_lane_reserves_at_least_its_local_model_max_tokens(self):
        cat = self._catalogue()
        checked = 0
        for name, entry in LANES.items():
            advertised = cat.get(entry["local_model"], {}).get("maxTokens")
            if advertised is None:
                continue
            reserve = entry["context_tokens"] - main._local_fallback_target(entry)
            assert reserve >= advertised, f"{name}: reserve {reserve} < {advertised}"
            checked += 1
        assert checked, "no lane's local model is in the catalogue -- test is vacuous"

    def test_prompt_plus_a_full_reply_fits_the_window(self):
        cat = self._catalogue()
        for name, entry in LANES.items():
            advertised = cat.get(entry["local_model"], {}).get("maxTokens", 0)
            assert (main._local_fallback_target(entry) + advertised
                    <= entry["context_tokens"]), name


class TestGenericSecretBlockFallback:
    """The second instance of the same bug, found after the first was fixed.

    A Tier B secret block on a model with no local_cloud_reasoning entry forces
    `lifeos_local_fallback`. The dispatch guard was `if lcr_entry:`, and that
    fallback has no such entry by definition -- so a large session carrying a
    private key was dispatched with no size check at all, and Ollama makes room
    by silently dropping the OLDEST messages.
    """

    def test_it_has_a_window_to_compact_against(self):
        entry = main._generic_local_entry()
        assert entry is not None, "no window configured for the generic fallback"
        assert entry["context_tokens"] > 0

    def test_it_leaves_room_for_the_reply(self):
        entry = main._generic_local_entry()
        target = main._local_fallback_target(entry)
        assert target < entry["context_tokens"]

    def test_prompt_plus_a_full_reply_fits(self):
        cat = {m["id"]: m
               for p in main.CFG["routing"]["pi_clients"].values()
               for m in (p.get("models") or [])}
        model = main.RT["lifeos_local_fallback"]
        advertised = cat.get(model, {}).get("maxTokens", 0)
        assert advertised, f"{model} not in the client catalogue -- test is vacuous"
        entry = main._generic_local_entry()
        assert main._local_fallback_target(entry) + advertised <= entry["context_tokens"]

    def test_window_tracks_the_server_not_the_advertised_catalogue(self):
        # The catalogue advertises 128000 to clients; the server actually holds
        # OLLAMA_CONTEXT_LENGTH. Compacting against the smaller number would fold
        # earlier than necessary, and every fold costs a summarizer prefill.
        assert main._generic_local_entry()["context_tokens"] >= 131072

    def test_degrades_to_the_old_behaviour_when_unconfigured(self, monkeypatch):
        # Better to skip the check than to compact against a guessed size.
        monkeypatch.setitem(main.RT, "lifeos_local_fallback_context_tokens", None)
        assert main._generic_local_entry() is None


class TestForcedLocalDispatchActuallyCompacts:
    """End to end, because the unit tests above did not pin the WIRING.

    Two different routes force a request onto a local model, and the first
    version of this fix covered only one of them. Mutation testing is what
    exposed that: reverting the dispatch left every unit test above green.

    * `lifeos_gate` refuses escalation on a credential hit and rewrites the
      model to the local fallback BEFORE privacy_evaluate runs, so the request
      never reaches the Tier B block branch at all. This is the route a PEM key
      actually takes on a `lifeos-*` model.
    * A Tier B block reaches the `forced_local_model` branch instead, but only
      for a model `lifeos_gate` does not intercept first.

    Both land a cloud-sized prompt on a local model with no lcr_entry, so both
    need the size check.
    """

    def _drive(self, monkeypatch, window, model="lifeos-cloud-code"):
        from fastapi.testclient import TestClient
        from privacy.config import PrivacyConfig

        compacted: list[int] = []
        dispatched: list[str] = []

        monkeypatch.setitem(main.RT, "lifeos_local_fallback_context_tokens", window)
        monkeypatch.setitem(main.PROVIDER_LABELS, "cloud", main.OLLAMA_CLOUD)
        monkeypatch.setattr(main, "get_config", lambda: PrivacyConfig(
            trusted=frozenset({main.OLLAMA_CLOUD}),
            restricted=frozenset({main.ANTHROPIC})))

        async def fake_compact(body, summarize, **kw):
            compacted.append(len(body.get("messages") or []))
            return body

        monkeypatch.setattr(main, "compact_messages", fake_compact)

        def fake_stream(client, method, path, body, model, req_id, headers=None):
            dispatched.append(model)

            async def gen():
                yield b""
            return gen()

        monkeypatch.setattr(main, "_supervised_stream", fake_stream)

        # A PEM header is the only block-action Tier B detector, and the block
        # only fires for a cloud destination -- hence the lifeos cloud lane.
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"
        with TestClient(main.app) as c:
            c.post("/v1/chat/completions", json={
                "model": model, "stream": True, "messages": [
                    {"role": "user", "content": f"deploy this\n{key}"},
                    {"role": "assistant", "content": "understood"},
                    {"role": "user", "content": "now ship it"}]})
        return compacted, dispatched

    def test_an_oversized_lifeos_refusal_is_compacted(self, monkeypatch):
        # Window of 1 token makes any body oversized, so this asserts the path
        # is REACHED rather than re-testing the arithmetic.
        compacted, dispatched = self._drive(monkeypatch, window=1)
        assert dispatched == [main.RT["lifeos_local_fallback"]], dispatched
        assert compacted, "secret-block fallback dispatched with no size check"

    def test_a_body_that_fits_is_left_alone(self, monkeypatch):
        compacted, dispatched = self._drive(monkeypatch, window=10**9)
        assert dispatched == [main.RT["lifeos_local_fallback"]]
        assert not compacted, "spent a summarizer hop on a body that already fits"

    def test_an_oversized_tier_b_block_fallback_is_compacted(self, monkeypatch):
        # `nemotron-3-super:cloud` has no lifeos prefix, so the gate leaves it
        # alone and the PEM header survives to privacy_evaluate, which blocks
        # it and forces the generic local fallback.
        compacted, dispatched = self._drive(
            monkeypatch, window=1, model="nemotron-3-super:cloud")
        assert dispatched == [main.RT["lifeos_local_fallback"]], dispatched
        assert compacted, "Tier B block fallback dispatched with no size check"

    def test_tier_b_block_body_that_fits_is_left_alone(self, monkeypatch):
        compacted, _ = self._drive(
            monkeypatch, window=10**9, model="nemotron-3-super:cloud")
        assert not compacted
