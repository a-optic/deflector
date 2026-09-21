# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""A body diverted from cloud onto the local tier must arrive unmasked.

Redaction runs against the destination the privacy decision was made for. When
that decision said CLOUD and the request is later diverted to LOCAL -- a tier
refusal, a silent tier, or a Tier B secret block -- the body still carries
placeholders the local model never needed. Local is exempt from redaction by
design (privacy/engine.py short-circuits it to Proceed), so nothing is
protected by leaving them in.

The sharp edge is _compact_for_local: it asks a model to summarize that body,
and a model paraphrasing `<PII_1d3_HIGH_ENTROPY_5>` reformats it. A reformatted
placeholder no longer matches PLACEHOLDER_RE and the mapping is per-request, so
it can never be restored -- the unrecoverable dead placeholder fixed in
81cb975, re-entering through the summarizer.

Offline. Run: .venv/bin/python -m pytest tests/test_local_fallback_rehydration.py -q
"""

import json

import pytest

import main
from privacy.rehydrate import PLACEHOLDER_RE

BLOB = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MHF3ZXJ0eXVpb3A+Kw=="       # Tier B entropy
QUOTED = 'he said "hello"\nand a \\ backslash'                    # JSON-hostile


def _mapping(*pairs) -> dict:
    return {ph: real for ph, real in pairs}


class TestPlainContent:
    def test_placeholder_in_a_message_is_restored(self):
        body = {"messages": [{"role": "user", "content": f"ls <PII_ab1_X_1>"}]}
        main._rehydrate_body_for_local(body, _mapping(("<PII_ab1_X_1>", "/etc/hosts")))
        assert body["messages"][0]["content"] == "ls /etc/hosts"

    def test_prompt_and_system_slots_are_covered(self):
        body = {"prompt": "<PII_ab1_X_1>", "system": "<PII_ab1_X_1>", "messages": []}
        main._rehydrate_body_for_local(body, _mapping(("<PII_ab1_X_1>", "/tmp/x")))
        assert body["prompt"] == "/tmp/x" and body["system"] == "/tmp/x"

    def test_list_segment_content_is_restored(self):
        # The multimodal-ish shape: content is a list of {"type","text"} segs.
        body = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "open <PII_ab1_X_1>"}]}]}
        main._rehydrate_body_for_local(body, _mapping(("<PII_ab1_X_1>", "/etc/hosts")))
        assert body["messages"][0]["content"][0]["text"] == "open /etc/hosts"

    def test_unknown_placeholder_is_left_alone_not_dropped(self):
        body = {"messages": [{"role": "user", "content": "<PII_zz9_X_9>"}]}
        main._rehydrate_body_for_local(body, _mapping(("<PII_ab1_X_1>", "/x")))
        assert body["messages"][0]["content"] == "<PII_zz9_X_9>"


class TestToolCallArgumentsStayValidJson:
    """The reason this walks _text_slots instead of substituting into the raw
    `arguments` string. privacy/engine.py: a malformed arguments string makes
    Ollama Cloud reject the whole request, the bad call stays in the history,
    and every later turn fails identically."""

    def _body(self):
        return {"messages": [{"role": "assistant", "tool_calls": [
            {"function": {"name": "bash",
                          "arguments": json.dumps({"command": "cat <PII_ab1_X_1>"})}}]}]}

    def test_value_is_restored_inside_the_envelope(self):
        body = self._body()
        main._rehydrate_body_for_local(body, _mapping(("<PII_ab1_X_1>", "/etc/hosts")))
        args = body["messages"][0]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(args) == {"command": "cat /etc/hosts"}

    def test_a_value_containing_quotes_and_backslashes_does_not_break_json(self):
        # The failure a flat string substitution would produce. Restoring
        # `he said "hello"\nand a \ backslash` into an already-serialised JSON
        # string unescaped yields something json.loads cannot read.
        body = self._body()
        main._rehydrate_body_for_local(body, _mapping(("<PII_ab1_X_1>", QUOTED)))
        args = body["messages"][0]["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(args)                    # must not raise
        assert parsed["command"] == f"cat {QUOTED}"

    def test_non_json_arguments_still_handled(self):
        body = {"messages": [{"role": "assistant", "tool_calls": [
            {"function": {"name": "bash", "arguments": "not json <PII_ab1_X_1>"}}]}]}
        main._rehydrate_body_for_local(body, _mapping(("<PII_ab1_X_1>", "/etc/hosts")))
        assert body["messages"][0]["tool_calls"][0]["function"]["arguments"] == \
            "not json /etc/hosts"


class TestNoMapping:
    def test_empty_mapping_is_a_no_op(self):
        body = {"messages": [{"role": "user", "content": "<PII_ab1_X_1>"}]}
        before = json.dumps(body)
        main._rehydrate_body_for_local(body, {})
        main._rehydrate_body_for_local(body, None)
        assert json.dumps(body) == before


class TestCaseInsensitivity:
    def test_a_lowercased_placeholder_is_still_restored(self):
        # PLACEHOLDER_RE is IGNORECASE. A `"<PII_" in text` pre-filter would
        # have skipped this slot while the regex would have matched it, which
        # is why there is no such pre-filter.
        assert PLACEHOLDER_RE.match("<pii_ab1_X_1>")
        body = {"messages": [{"role": "user", "content": "<pii_ab1_X_1>"}]}
        main._rehydrate_body_for_local(body, _mapping(("<pii_ab1_X_1>", "/etc/hosts")))
        assert body["messages"][0]["content"] == "/etc/hosts"


# --- end to end, through the real dispatch path ---------------------------------

CLOUD_MODEL = "test-cloud:cloud"
LOCAL_MODEL = "test-local"


@pytest.fixture
def fallback(monkeypatch):
    """Refuse on cloud, capture the body that reaches the local tier."""
    from privacy.config import PrivacyConfig

    seen: list[dict] = []

    monkeypatch.setitem(main.RT, "local_cloud_reasoning", {
        "test-auto": {"local_model": LOCAL_MODEL, "cloud_model": CLOUD_MODEL,
                      "context_tokens": 1000, "threshold_pct": 0}})
    monkeypatch.setitem(main.PROVIDER_LABELS, "cloud", main.OLLAMA_CLOUD)
    monkeypatch.setattr(main, "get_config", lambda: PrivacyConfig(
        trusted=frozenset({main.OLLAMA_CLOUD}),
        restricted=frozenset({main.ANTHROPIC})))

    def fake_stream(client, method, path, body, model, req_id, headers=None):
        seen.append({"model": model, "body": json.loads(json.dumps(body))})

        async def gen():
            if model == CLOUD_MODEL:
                main.tracing.note(upstream_status=429)
                yield b"refused"
                return
            yield b""
        return gen()

    monkeypatch.setattr(main, "_supervised_stream", fake_stream)
    return seen


def _post(body):
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        return c.post("/v1/chat/completions", json=body)


def test_cloud_gets_the_masked_body_and_local_gets_the_real_one(fallback):
    """The whole point, end to end. Fails before this phase."""
    _post({"model": "test-auto", "stream": True, "messages": [
        {"role": "user", "content": f"audit artifact: {BLOB}"}]})

    assert [s["model"] for s in fallback] == [CLOUD_MODEL, LOCAL_MODEL]

    cloud_text = json.dumps(fallback[0]["body"])
    local_text = json.dumps(fallback[1]["body"])

    # The gate still did its job on the way out.
    assert BLOB not in cloud_text
    assert "<PII_" in cloud_text

    # ...and the local tier, which never needed it, got the real value back.
    assert BLOB in local_text
    assert "<PII_" not in local_text


def test_the_summarizer_never_sees_a_placeholder(monkeypatch, fallback):
    """The ordering constraint, made testable.

    _compact_for_local hands the body to a model to summarize. A model
    paraphrasing `<PII_...>` reformats it past PLACEHOLDER_RE's reach, and the
    mapping is per-request -- so a placeholder that reaches the summarizer is
    gone for good. Rehydration therefore has to happen BEFORE compaction, not
    merely before dispatch, and this is what pins that order.
    """
    summarized: list[str] = []

    async def fake_compact(body, summarize):
        summarized.append(json.dumps(body))
        return body

    monkeypatch.setattr(main, "compact_messages", fake_compact)
    # context_tokens is 1000 in the fixture, so this comfortably overflows and
    # compaction actually runs rather than returning the body untouched.
    big = "\n".join(f"artifact {i}: {BLOB}" for i in range(400))
    _post({"model": "test-auto", "stream": True,
           "messages": [{"role": "user", "content": big}]})

    assert summarized, "compaction never ran -- the test would be vacuous"
    # The binding assertion. If rehydration ran AFTER compaction, the summarizer
    # would see placeholders and none of the real value -- so the raw blob being
    # here is what pins the order. Verified by mutation: moving the call below
    # _compact_for_local fails this.
    assert BLOB in summarized[0]
    # This one flaked ~1 run in 3 while it was being written, and chasing that
    # turned up a separate, worse bug: Tier C was redacting INSIDE the
    # placeholders Tier B had just written, which left a dead placeholder one
    # rehydration pass could not restore. See
    # privacy/test_tier_c_placeholders.py.
    #
    # It holds only because that is fixed, so this assertion doubles as a
    # cross-check on it. If it starts flaking again, suspect double-masking
    # before suspecting anything here.
    assert "<PII_" not in summarized[0]
