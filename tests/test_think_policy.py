# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""_apply_think_policy tests: default-off for configured local lanes, with a
per-request header override, and the right field per API path (Ollama honors
`think` only on /api/*, `reasoning_effort` only on /v1 -- writing the wrong
one is a silent no-op, so the path split is the whole point).
Offline. Run: .venv/bin/python -m pytest tests/test_think_policy.py -q
"""

import main

V1 = "/v1/chat/completions"
NATIVE = "/api/chat"

OFF_MODEL = next(iter(main.THINK_OFF_MODELS))
ON_MODEL = "some-model-not-in-the-list:cloud"


def _req(headers: dict | None = None):
    lc = {k.lower(): v for k, v in (headers or {}).items()}
    return type("R", (), {"headers": lc})()


def _apply(body, model, path, headers=None):
    main._apply_think_policy(body, model, path, _req(headers))
    return body


# --- defaults ------------------------------------------------------------

def test_v1_default_off_uses_reasoning_effort():
    body = _apply({}, OFF_MODEL, V1)
    assert body["reasoning_effort"] == "none"
    assert "think" not in body  # native-only field must not be set on /v1


def test_native_default_off_uses_think():
    body = _apply({}, OFF_MODEL, NATIVE)
    assert body["think"] is False
    assert "reasoning_effort" not in body


def test_model_not_in_list_is_untouched():
    assert _apply({}, ON_MODEL, V1) == {}
    assert _apply({}, ON_MODEL, NATIVE) == {}


# --- header override -----------------------------------------------------

def test_header_raises_thinking_on_v1():
    body = _apply({}, OFF_MODEL, V1, {"X-Deflector-Think": "high"})
    assert body["reasoning_effort"] == "high"


def test_header_can_raise_a_model_that_defaults_on():
    body = _apply({}, ON_MODEL, V1, {"X-Deflector-Think": "medium"})
    assert body["reasoning_effort"] == "medium"


def test_header_off_suppresses_a_model_not_in_the_list():
    body = _apply({}, ON_MODEL, V1, {"X-Deflector-Think": "off"})
    assert body["reasoning_effort"] == "none"


def test_header_on_native_path_sets_boolean_true():
    body = _apply({}, OFF_MODEL, NATIVE, {"X-Deflector-Think": "high"})
    assert body["think"] is True


def test_header_is_case_insensitive_and_trimmed():
    body = _apply({}, OFF_MODEL, V1, {"X-Deflector-Think": "  HIGH "})
    assert body["reasoning_effort"] == "high"


def test_legacy_header_still_accepted():
    body = _apply({}, OFF_MODEL, V1, {"X-AgentStop-Think": "low"})
    assert body["reasoning_effort"] == "low"


# --- explicit body wins over everything ----------------------------------

def test_explicit_reasoning_effort_not_overridden_by_default():
    body = _apply({"reasoning_effort": "high"}, OFF_MODEL, V1)
    assert body["reasoning_effort"] == "high"


def test_explicit_reasoning_effort_not_overridden_by_header():
    body = _apply({"reasoning_effort": "high"}, OFF_MODEL, V1,
                  {"X-Deflector-Think": "off"})
    assert body["reasoning_effort"] == "high"


def test_explicit_think_false_not_overridden():
    body = _apply({"think": False}, OFF_MODEL, NATIVE, {"X-Deflector-Think": "high"})
    assert body["think"] is False


def test_suppression_applies_on_both_paths():
    # The original form of this test pinned `hermes` specifically, because it
    # used to be a hardcoded special case and the refactor to config-driven
    # suppression had to not lose it. What actually needed guarding is the
    # BOTH-PATHS behaviour -- the old hardcoded `think = False` silently did
    # nothing for /v1 callers. That is asserted here against whichever model is
    # configured, so it survives policy changes to the list itself.
    off = OFF_MODEL
    assert _apply({}, off, V1)["reasoning_effort"] == "none"
    assert _apply({}, off, NATIVE)["think"] is False


def test_hermes_lane_is_deliberately_not_suppressed():
    """Policy reversal 2026-09-20, backed by measurement.

    hermes was suppressed for a measured latency win (18.8s -> 7.5s on a ~44KB
    agent payload). Nobody had measured the ACCURACY side of that trade. On a
    4-problem multi-step reasoning set, suppressing thinking cost hermes
    1.00 -> 0.50 -- it was silently paying half its reasoning accuracy for the
    speedup. Same for the other two Qwen3.6 lanes.

    Only models measuring ZERO accuracy cost stay in think_off_models. If you
    are re-adding hermes here, re-run that reasoning benchmark first and record
    the number, because the latency argument alone already proved insufficient.
    """
    hermes = main.RT["hermes_model"]
    assert hermes not in main.THINK_OFF_MODELS
    # and the per-request lever still works for callers who DO want it off
    assert _apply({}, hermes, V1, {"X-Deflector-Think": "off"})["reasoning_effort"] == "none"


class TestPiModelsCatalog:
    """GET /pi/models.json must carry the capability flags the thin client
    gates on, because the serializer hand-picks fields: anything added to
    config.yaml that it does not name is silently dropped, with no error.

    That is not hypothetical. `reasoning` was missing here while the Pi client
    required BOTH `model.reasoning` and `compat.supportsReasoningEffort` before
    it would send reasoning_effort at all -- so every response came back with
    no chain-of-thought and it looked like the proxy was stripping it.
    """

    def _catalog(self):
        from fastapi.testclient import TestClient
        with TestClient(main.app) as c:
            r = c.get("/pi/models.json")
        assert r.status_code == 200
        return r.json()["providers"]["agentstop"]

    def test_compat_allows_client_to_send_reasoning_effort(self):
        # False here makes think_off_models the ONLY behavior -- the client
        # loses every way to override it.
        assert self._catalog()["compat"]["supportsReasoningEffort"] is True

    def test_every_model_declares_reasoning(self):
        # Present on all of them, not just the ones that reason: the client
        # reads it as a tri-state and a missing key is not the same as False.
        for m in self._catalog()["models"]:
            assert isinstance(m.get("reasoning"), bool), m["id"]

    def test_thinking_models_are_marked(self):
        by_id = {m["id"]: m for m in self._catalog()["models"]}
        for mid in main.THINK_OFF_MODELS:
            if mid in by_id:
                # A model we bother suppressing by default self-evidently
                # reasons -- so it must be able to be asked to do it again.
                assert by_id[mid]["reasoning"] is True, mid

    def test_llama4_not_marked_reasoning(self):
        # Verified against the live model: it answers normally with no
        # reasoning_effort but returns 400 when the field is sent, so marking
        # it would break the model rather than enrich it.
        by_id = {m["id"]: m for m in self._catalog()["models"]}
        if "llama4:latest" in by_id:
            assert by_id["llama4:latest"]["reasoning"] is False


class TestOffWordNormalization:
    """`off` in the request body is a trap worth defusing.

    Ollama accepts none / minimal / low / medium / high / xhigh / max and hard
    400s on "off" -- which is both the most natural word to reach for and the
    exact vocabulary this proxy's own X-Deflector-Think header uses. Verified
    against the live model: reasoning_effort "off" returns upstream_error_400
    while every other level streams normally.
    """

    def test_body_off_is_rewritten_to_none(self):
        for word in ("off", "OFF", " Off ", "false", "no", "0"):
            body = {"reasoning_effort": word}
            _apply(body, ON_MODEL, V1)
            assert body["reasoning_effort"] == "none", word

    def test_real_levels_pass_through_untouched(self):
        # Not a gradient in practice, but never this function's call to flatten.
        for word in ("minimal", "low", "medium", "high", "xhigh", "max"):
            body = {"reasoning_effort": word}
            _apply(body, ON_MODEL, V1)
            assert body["reasoning_effort"] == word

    def test_explicit_body_still_wins_over_the_default_off_list(self):
        body = {"reasoning_effort": "high"}
        _apply(body, OFF_MODEL, V1)
        assert body["reasoning_effort"] == "high"

    def test_native_think_field_untouched(self):
        body = {"think": True}
        _apply(body, OFF_MODEL, NATIVE)
        assert body["think"] is True
