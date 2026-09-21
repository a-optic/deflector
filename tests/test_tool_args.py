# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""_sanitize_tool_call_args tests.

Ollama Cloud rejects the WHOLE request with `400 invalid tool call arguments` if
any tool call's `arguments` is not a parseable JSON string -- empty string, null
and malformed all trigger it, verified against the live endpoint. The local tiers
accept the same body, so this only bites on escalation, and the bad call stays in
the conversation history: every later turn fails identically until the transcript
is edited. Hence repair-at-dispatch rather than propagate.

Run: .venv/bin/python -m pytest tests/test_tool_args.py -q
"""

import json

import main


def _body(arguments, name="fetch"):
    fn = {"name": name}
    if arguments is not _MISSING:
        fn["arguments"] = arguments
    return {"model": "m", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": fn}]},
    ]}


_MISSING = object()


def _args(body):
    return body["messages"][1]["tool_calls"][0]["function"]["arguments"]


def _sanitize(body):
    main._sanitize_tool_call_args(body, "req-1")
    return body


def test_valid_arguments_left_byte_identical():
    original = json.dumps({"path": "/etc/hosts", "depth": 2})
    body = _sanitize(_body(original))
    assert _args(body) == original


def test_empty_string_becomes_empty_object():
    assert json.loads(_args(_sanitize(_body("")))) == {}


def test_whitespace_only_becomes_empty_object():
    assert json.loads(_args(_sanitize(_body("   ")))) == {}


def test_none_becomes_empty_object():
    assert json.loads(_args(_sanitize(_body(None)))) == {}


def test_missing_arguments_becomes_empty_object():
    assert json.loads(_args(_sanitize(_body(_MISSING)))) == {}


def test_dict_is_serialized_to_string():
    body = _sanitize(_body({"path": "/x"}))
    assert isinstance(_args(body), str)
    assert json.loads(_args(body)) == {"path": "/x"}


def test_malformed_json_preserved_under_raw_key():
    # Content is kept, not dropped: it may be a redacted payload whose
    # placeholders still have to survive to rehydration.
    body = _sanitize(_body('{"path": "/x/<PII_a1_IP_PRIVATE_1>'))
    assert json.loads(_args(body))["_raw"] == '{"path": "/x/<PII_a1_IP_PRIVATE_1>'


def test_json_scalar_is_not_a_valid_arguments_object():
    assert json.loads(_args(_sanitize(_body("5")))) == {"_raw": "5"}


def test_every_repaired_result_is_a_json_object_string():
    # The contract upstream actually enforces, asserted over all the bad shapes.
    for bad in ["", "   ", None, _MISSING, "not json", "[1,2]", "5"]:
        out = _args(_sanitize(_body(bad)))
        assert isinstance(out, str)
        assert isinstance(json.loads(out), dict)


def test_body_without_tool_calls_untouched():
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    before = json.dumps(body, sort_keys=True)
    main._sanitize_tool_call_args(body, "req-1")
    assert json.dumps(body, sort_keys=True) == before


def test_repair_is_recorded_on_the_trace():
    written = []
    orig = main._log_request_trace
    main._log_request_trace = written.append
    try:
        main._sanitize_tool_call_args(_body(""), "req-1")
        main._sanitize_tool_call_args(_body(json.dumps({"ok": 1})), "req-2")
    finally:
        main._log_request_trace = orig
    assert len(written) == 1                      # only the repair, not the clean one
    assert written[0]["ev"] == "tool_args_repaired"
    assert written[0]["count"] == 1
