# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Routing and the privacy decision must agree on one destination.

Regression origin: `resolve_routing` was called twice -- once to pick the
privacy destination, once to dispatch -- with redaction running in between.
Placeholders are shorter than the values they replace, so the second call
measured a smaller prompt than the first. The `*-auto` lanes escalate on prompt
size and real Pi traffic sits right on that boundary, so requests were evaluated
as CLOUD, redacted, and then dispatched to a LOCAL model, which answered from
masked text: pure cost and a degraded answer for data that never left the box.

Observed on 2026-09-08, e.g. 59,139 tokens in (threshold 58,982) -> redacted to
57,537 -> ran on the local model. 4 of 17 redacted requests that day flipped.

Offline. Run: .venv/bin/python -m pytest tests/test_route_redaction_consistency.py -q
"""

import json

import pytest
from fastapi.testclient import TestClient

import main
from budget import estimate_prompt_tokens
from privacy.config import PrivacyConfig

CLOUD_MODEL = "test-cloud:cloud"
LOCAL_MODEL = "test-local"
CONTEXT_TOKENS = 1000          # threshold lands at 45% == 450 tokens
BLOB = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MHF3ZXJ0eXVpb3A+Kw=="   # trips Tier B entropy


def _body(n_blobs: int) -> dict:
    return {"model": "test-auto", "stream": True, "messages": [
        {"role": "user", "content": "audit these build artifacts:\n" + "\n".join(
            f"artifact {i}: {BLOB}" for i in range(n_blobs))}]}


@pytest.fixture
def dispatched(monkeypatch):
    """Record what actually got dispatched upstream, and stub the upstream out."""
    seen: dict = {}

    monkeypatch.setitem(main.RT, "local_cloud_reasoning", {
        "test-auto": {"local_model": LOCAL_MODEL, "cloud_model": CLOUD_MODEL,
                      "context_tokens": CONTEXT_TOKENS, "threshold_pct": 45}})
    monkeypatch.setitem(main.PROVIDER_LABELS, "cloud", main.OLLAMA_CLOUD)
    monkeypatch.setattr(main, "get_config", lambda: PrivacyConfig(
        trusted=frozenset({main.OLLAMA_CLOUD}),
        restricted=frozenset({main.ANTHROPIC})))

    def fake_stream(client, method, path, body, model, req_id, headers=None):
        seen["model"] = model
        seen["body"] = json.loads(json.dumps(body))   # snapshot before mutation

        async def gen():
            yield b""
        return gen()

    monkeypatch.setattr(main, "_supervised_stream", fake_stream)
    return seen


def _post(body: dict):
    with TestClient(main.app) as c:
        return c.post("/v1/chat/completions", json=body)


def _sent_text(seen: dict) -> str:
    return seen["body"]["messages"][0]["content"]


class TestPremise:
    """The bug only exists where redaction moves a prompt across the threshold.
    If these stop holding, the tests below are vacuous rather than passing."""

    def test_body_straddles_the_escalation_threshold(self):
        from main import _extract_body_text
        from privacy.engine import privacy_evaluate

        cfg = PrivacyConfig(trusted=frozenset({main.OLLAMA_CLOUD}))
        body = _body(10)
        before = estimate_prompt_tokens(_extract_body_text(body))
        decision = privacy_evaluate(body, main.OLLAMA_CLOUD, cfg)
        after = estimate_prompt_tokens(_extract_body_text(decision.body))
        threshold = CONTEXT_TOKENS * 0.45

        assert before > threshold, "premise: unredacted body must escalate to cloud"
        assert after < threshold, "premise: redaction must drag it back under"


class TestDestinationIsStable:
    def test_redaction_does_not_downgrade_the_route_to_local(self, dispatched):
        # THE test. Evaluated as cloud, so it must still be dispatched to cloud.
        assert _post(_body(10)).status_code == 200
        assert dispatched["model"] == CLOUD_MODEL

    def test_what_was_sent_is_actually_redacted(self, dispatched):
        # The other half: it went to cloud, so it must not carry the raw values.
        _post(_body(10))
        assert BLOB not in _sent_text(dispatched)
        assert "HIGH_ENTROPY" in _sent_text(dispatched)


class TestLocalStaysUnredacted:
    """The operator-facing invariant: local inference is never redacted."""

    def test_small_prompt_routes_local_and_keeps_its_raw_values(self, dispatched):
        assert _post(_body(2)).status_code == 200
        assert dispatched["model"] == LOCAL_MODEL
        assert BLOB in _sent_text(dispatched), "local path must not redact"
