# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retrieved memories must never reach a cloud provider unredacted.

Open Brain holds UNREDACTED personal material. Injecting it into a prompt is
therefore a way for that material to reach a cloud model without passing the
privacy pipeline -- unless the injection happens on the correct side of the
gate.

The design puts it between `destination = _provider_for(...)` and
`decision = privacy_evaluate(...)`:

  after routing   -- retrieval cannot change where the request goes. The *-auto
                     lanes escalate on prompt SIZE, so injecting earlier could
                     push a request past the threshold and CAUSE a cloud hop.
  before the gate -- cloud-bound content is redacted like the rest of the body.

The first test class below is the one that matters: it proves the property
end to end rather than asserting the call sits on a particular line.

Offline. Run: .venv/bin/python -m pytest tests/test_retrieval_injection.py -q
"""

import json

import pytest
from fastapi.testclient import TestClient

import main
import open_brain
from open_brain import Hit
from privacy.config import PrivacyConfig

# Trips Tier B's entropy detector, so the gate demonstrably acts on it.
SECRET = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MHF3ZXJ0eXVpb3A+Kw=="
CLOUD_MODEL = "test-cloud:cloud"
LOCAL_MODEL = "test-local"


def _hit(content, source="brain-capture", tags=(), distance=0.2):
    return Hit(id="m1", source=source, content=content, tags=tuple(tags),
               distance=distance)


@pytest.fixture
def harness(monkeypatch):
    """Force a destination, stub retrieval, capture what actually dispatched."""
    seen: list[dict] = []
    searched: list[dict] = []

    monkeypatch.setitem(main.PROVIDER_LABELS, "cloud", main.OLLAMA_CLOUD)
    monkeypatch.setattr(main, "get_config", lambda: PrivacyConfig(
        trusted=frozenset({main.OLLAMA_CLOUD}),
        restricted=frozenset({main.ANTHROPIC})))

    def fake_stream(client, method, path, body, model, req_id, headers=None):
        seen.append({"model": model, "body": json.loads(json.dumps(body))})

        async def gen():
            yield b""
        return gen()

    monkeypatch.setattr(main, "_supervised_stream", fake_stream)

    def configure(*, enabled=True, hits=(), cloud=True, **overrides):
        rcfg = {
            "enabled": enabled,
            "base_url": "http://ob.invalid",
            "embed_url": "http://emb.invalid",
            "embed_model": "m",
            "timeout_s": 5,
            "local": {"max_results": 5, "max_distance": 0.70,
                      "max_injected_tokens": 4000},
            "cloud": {"max_results": 2, "max_distance": 0.55,
                      "max_injected_tokens": 1500,
                      "deny_tags": ["private"], "deny_sources": ["health-score"]},
        }
        for key, value in overrides.items():
            rcfg[key] = value
        monkeypatch.setitem(main.CFG, "retrieval", rcfg)

        def fake_search(query, **kw):
            searched.append(kw | {"query": query})
            return list(hits)

        monkeypatch.setattr(open_brain, "search", fake_search)
        monkeypatch.setattr(main.open_brain, "search", fake_search)
        # The model id decides the destination: a :cloud model routes to the
        # cloud client, anything else stays local.
        return CLOUD_MODEL if cloud else LOCAL_MODEL

    def post(model, content="what did we decide about retries"):
        with TestClient(main.app) as c:
            c.post("/v1/chat/completions", json={
                "model": model, "stream": True,
                "messages": [{"role": "user", "content": content}]})
        return seen, searched

    configure.post = post
    return configure


class TestTheStructuralGuarantee:
    """The whole point of the phase."""

    def test_cloud_bound_retrieved_content_is_redacted(self, harness):
        model = harness(hits=[_hit(f"deploy key is {SECRET}")], cloud=True)
        seen, _ = harness.post(model)

        assert seen, "nothing dispatched"
        sent = json.dumps(seen[0]["body"])
        # The memory reached the prompt...
        assert "deploy key is" in sent
        # ...but the gate caught the secret inside it on the way out.
        assert SECRET not in sent, "unredacted memory content reached the cloud tier"
        assert "<PII_" in sent

    def test_local_bound_retrieved_content_keeps_real_values(self, harness):
        # Local is exempt from redaction by design, and Phase 2 rehydrates
        # anything a diverted cloud attempt had masked.
        model = harness(hits=[_hit(f"deploy key is {SECRET}")], cloud=False)
        seen, _ = harness.post(model)
        sent = json.dumps(seen[0]["body"])
        assert SECRET in sent
        assert "<PII_" not in sent

    def test_injection_is_a_labelled_system_message_at_the_front(self, harness):
        model = harness(hits=[_hit("an earlier note")], cloud=False)
        seen, _ = harness.post(model)
        first = seen[0]["body"]["messages"][0]
        assert first["role"] == "system"
        assert "retrieved from earlier sessions" in first["content"]
        assert "not part of this conversation" in first["content"]


class TestCloudGuardrails:
    def test_deny_tags_apply_to_cloud(self, harness):
        model = harness(hits=[_hit("x")], cloud=True)
        _, searched = harness.post(model)
        assert searched[0]["exclude_tags"] == ("private",)

    def test_deny_tags_do_not_apply_to_local(self, harness):
        model = harness(hits=[_hit("x")], cloud=False)
        _, searched = harness.post(model)
        assert searched[0]["exclude_tags"] == ()

    def test_denied_source_is_dropped_for_cloud(self, harness):
        model = harness(hits=[_hit("vitals", source="health-score")], cloud=True)
        seen, _ = harness.post(model)
        assert "vitals" not in json.dumps(seen[0]["body"])

    def test_same_source_is_allowed_for_local(self, harness):
        model = harness(hits=[_hit("vitals", source="health-score")], cloud=False)
        seen, _ = harness.post(model)
        assert "vitals" in json.dumps(seen[0]["body"])

    def test_cloud_uses_the_tighter_budget(self, harness):
        model = harness(hits=[_hit("x")], cloud=True)
        _, searched = harness.post(model)
        assert searched[0]["limit"] == 2
        assert searched[0]["max_distance"] == 0.55

    def test_local_uses_the_looser_budget(self, harness):
        model = harness(hits=[_hit("x")], cloud=False)
        _, searched = harness.post(model)
        assert searched[0]["limit"] == 5
        assert searched[0]["max_distance"] == 0.70


class TestBudgetAndQuery:
    def test_token_cap_truncates_by_whole_memories(self, harness):
        big = "word " * 3000            # comfortably over the 1500-token cloud cap
        model = harness(hits=[_hit(big), _hit("second note")], cloud=True)
        seen, _ = harness.post(model)
        sent = json.dumps(seen[0]["body"])
        assert "second note" not in sent, "cap did not stop at the first memory"

    def test_a_memory_over_the_cap_injects_nothing(self, harness):
        model = harness(hits=[_hit("word " * 3000)], cloud=True)
        seen, _ = harness.post(model)
        assert "retrieved from earlier sessions" not in json.dumps(seen[0]["body"])

    def test_query_is_the_last_user_turn_not_the_whole_history(self, harness):
        # Embedding the whole conversation returns what the session already
        # talked about, which is the opposite of useful.
        model = harness(hits=[_hit("x")], cloud=False)
        with TestClient(main.app) as c:
            c.post("/v1/chat/completions", json={
                "model": model, "stream": True, "messages": [
                    {"role": "user", "content": "an older question about kafka"},
                    {"role": "assistant", "content": "answered"},
                    {"role": "user", "content": "the live question about retries"}]})
        _, searched = harness.post(model)
        assert "retries" in searched[0]["query"]
        assert "kafka" not in searched[0]["query"]


class TestFailsOpen:
    def test_disabled_leaves_the_body_untouched(self, harness):
        model = harness(enabled=False, hits=[_hit("should not appear")], cloud=False)
        seen, searched = harness.post(model)
        assert not searched, "searched despite being disabled"
        assert "should not appear" not in json.dumps(seen[0]["body"])
        assert len(seen[0]["body"]["messages"]) == 1

    def test_no_hits_leaves_the_body_untouched(self, harness):
        model = harness(hits=[], cloud=False)
        seen, _ = harness.post(model)
        assert len(seen[0]["body"]["messages"]) == 1

    def test_a_raising_search_does_not_fail_the_request(self, harness, monkeypatch):
        model = harness(hits=[_hit("x")], cloud=False)

        def boom(*a, **kw):
            raise RuntimeError("open brain is down")

        monkeypatch.setattr(main.open_brain, "search", boom)
        seen, _ = harness.post(model)
        assert seen, "request died because retrieval did"
        assert len(seen[0]["body"]["messages"]) == 1

    def test_blank_query_skips_retrieval(self, harness):
        model = harness(hits=[_hit("x")], cloud=False)
        _, searched = harness.post(model, content="   ")
        assert not searched
