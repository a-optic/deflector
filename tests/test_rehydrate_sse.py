# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""_rehydrate_sse tests.

Regression origin: a redacted request on the /v1 path streamed raw
`<PII_..._IP_PRIVATE_1>` text to the caller, because the NDJSON rehydrator
does json.loads() on `data: {...}` lines, always raises, and passed every
frame through untouched. Offline.
Run: .venv/bin/python -m pytest tests/test_rehydrate_sse.py -q
"""

import asyncio
import json

import main

MAPPING = {
    "<PII_n1_IP_PRIVATE_1>": "192.0.2.10",
    "<PII_n1_IP_PRIVATE_2>": "192.0.2.20",
}


def _frame(**delta) -> bytes:
    return ("data: " + json.dumps(
        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}) + "\n\n").encode()


def _run(chunks, mapping=MAPPING):
    async def gen():
        for c in chunks:
            yield c

    async def collect():
        return [x async for x in main._rehydrate_sse(gen(), mapping)]

    return b"".join(asyncio.run(collect()))


def _contents(raw: bytes) -> str:
    out = []
    for line in raw.decode().splitlines():
        if not line.startswith("data: "):
            continue
        p = line[6:].strip()
        if p == "[DONE]":
            continue
        try:
            obj = json.loads(p)
        except json.JSONDecodeError:
            continue  # deliberately-malformed frames are passed through verbatim
        for ch in obj.get("choices") or []:
            out.append(ch.get("delta", {}).get("content", "") or "")
    return "".join(out)


def test_placeholder_restored_in_content():
    raw = _run([_frame(content="proxy is at <PII_n1_IP_PRIVATE_1> ok")])
    assert "192.0.2.10" in _contents(raw)
    assert "PII_n1_IP_PRIVATE_1" not in raw.decode()


def test_placeholder_split_across_chunks_is_restored():
    raw = _run([_frame(content="host <PII_n1_IP_"), _frame(content="PRIVATE_2> done")])
    assert "192.0.2.20" in _contents(raw)
    assert "PII_n1_IP_PRIVATE_2" not in raw.decode()


def test_sse_framing_preserved():
    raw = _run([_frame(content="hi"), b"data: [DONE]\n\n"])
    text = raw.decode()
    # every data frame must be terminated by a blank line, or strict SSE
    # parsers will not dispatch the event
    assert text.endswith("data: [DONE]\n\n")
    for frame in [f for f in text.split("\n\n") if f.strip()]:
        assert frame.startswith("data: ")


def test_done_sentinel_passed_through_exactly_once():
    raw = _run([_frame(content="x"), b"data: [DONE]\n\n"])
    assert raw.decode().count("[DONE]") == 1


def test_reasoning_field_also_rehydrated():
    raw = _run([_frame(reasoning="thinking about <PII_n1_IP_PRIVATE_1>")])
    assert "192.0.2.10" in raw.decode()


def test_content_and_reasoning_buffers_are_independent():
    # a placeholder split across two content frames must not be corrupted by
    # reasoning text arriving in between (shared buffer bug)
    raw = _run([
        _frame(content="at <PII_n1_IP_"),
        _frame(reasoning="some unrelated thinking"),
        _frame(content="PRIVATE_1> end"),
    ])
    assert "192.0.2.10" in _contents(raw)


def test_non_data_lines_passed_through():
    raw = _run([b": keepalive comment\n", _frame(content="hi")])
    assert b": keepalive comment" in raw


def test_unparseable_data_frame_passed_through_not_dropped():
    raw = _run([b"data: {not json\n\n", _frame(content="ok")])
    assert b"not json" in raw
    assert "ok" in _contents(raw)


def test_tail_flushed_when_stream_ends_without_done():
    # trailing partial placeholder must not be swallowed at end of stream
    raw = _run([_frame(content="tail <PII_n1_IP_")])
    assert "<PII_n1_IP_" in raw.decode()


def test_no_mapping_hits_leaves_text_unchanged():
    raw = _run([_frame(content="nothing to restore here")])
    assert _contents(raw) == "nothing to restore here"


# --- chunk boundaries -----------------------------------------------------------
# Regression cases for the incident these were written after. Every case above
# feeds WHOLE frames as chunks, which is exactly why the bug survived: a frame
# split mid-JSON made json.loads raise, the partial line went out through the
# except-branch with a single "\n", and its remainder followed as a bare line.
# With no blank line between them a client folds the NEXT real `data:` line into
# the same event, joining with "\n" per the SSE spec -- surfacing to the user as
# "Bad control character in string literal in JSON at position 181".

def _data_payloads(raw: bytes) -> list[str]:
    return [ln[6:].strip() for ln in raw.decode().splitlines()
            if ln.startswith("data: ")]


def _assert_well_formed(raw: bytes):
    """Every emitted data line must be parseable JSON or the sentinel -- the
    invariant that was actually violated."""
    for p in _data_payloads(raw):
        if p == "[DONE]":
            continue
        json.loads(p)          # raises on a truncated frame
    for line in raw.decode().splitlines():
        if line.strip() and not line.startswith("data: "):
            raise AssertionError(f"stray non-SSE line leaked: {line!r}")


def test_frame_split_mid_json_is_reassembled():
    whole = _frame(content="proxy at <PII_n1_IP_PRIVATE_1> ok")
    cut = len(whole) // 2
    raw = _run([whole[:cut], whole[cut:]])
    _assert_well_formed(raw)
    assert "192.0.2.10" in _contents(raw)


def test_output_identical_at_every_possible_split_point():
    # The property that makes chunk boundaries irrelevant. Byte-for-byte over
    # every split of a two-frame stream, including inside a placeholder, inside
    # a JSON string, and on the \n\n terminator.
    stream = (_frame(content="host <PII_n1_IP_PRIVATE_2> is up")
              + _frame(content=" and <PII_n1_IP_PRIVATE_1> too")
              + b"data: [DONE]\n\n")
    baseline = _run([stream])
    _assert_well_formed(baseline)
    for i in range(1, len(stream)):
        raw = _run([stream[:i], stream[i:]])
        assert raw == baseline, f"output differs when split at byte {i}"


def test_byte_at_a_time_stream_is_reassembled():
    stream = _frame(content="ip <PII_n1_IP_PRIVATE_1> end") + b"data: [DONE]\n\n"
    raw = _run([stream[i:i + 1] for i in range(len(stream))])
    _assert_well_formed(raw)
    assert "192.0.2.10" in _contents(raw)


def test_final_line_without_trailing_newline_is_not_dropped():
    frame = _frame(content="tail <PII_n1_IP_PRIVATE_1>").rstrip(b"\n")
    raw = _run([frame])
    assert "192.0.2.10" in _contents(raw)


def test_ndjson_frame_split_mid_json_is_reassembled():
    line = (json.dumps({"message": {"role": "assistant",
                                    "content": "at <PII_n1_IP_PRIVATE_2> now"},
                        "done": False}) + "\n").encode()
    cut = len(line) // 2

    async def gen():
        yield line[:cut]
        yield line[cut:]

    async def collect():
        return [x async for x in main._rehydrate_ndjson(gen(), MAPPING)]

    raw = b"".join(asyncio.run(collect()))
    obj = json.loads(raw.decode().strip())        # raises if truncated
    assert obj["message"]["content"] == "at 192.0.2.20 now"


# --- tool-call arguments -------------------------------------------------------
#
# Regression origin: rehydration covered `content` and `reasoning` only, but
# redaction rewrites strings INSIDE tool-call arguments too. A placeholder in a
# tool argument therefore reached the client verbatim, which then executed the
# tool with the literal `<PII_..._1>` string as a real value.

def _tool_frame(idx, args, **extra) -> bytes:
    fn = {"arguments": args}
    fn.update(extra)
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": idx, "function": fn}]},
        "finish_reason": None}]}) + "\n\n").encode()


def _tool_args(raw: bytes) -> dict:
    """Concatenate streamed argument fragments per tool-call index, as a client does."""
    out: dict = {}
    for line in raw.decode().splitlines():
        if not line.startswith("data: "):
            continue
        p = line[6:].strip()
        if p == "[DONE]":
            continue
        try:
            obj = json.loads(p)
        except json.JSONDecodeError:
            continue
        for ch in obj.get("choices") or []:
            for tc in ch.get("delta", {}).get("tool_calls") or []:
                out[tc["index"]] = out.get(tc["index"], "") + tc["function"]["arguments"]
    return out


def test_placeholder_restored_in_tool_call_arguments():
    raw = _run([_tool_frame(0, '{"host": "<PII_n1_IP_PRIVATE_1>"}'), b"data: [DONE]\n\n"])
    assert _tool_args(raw) == {0: '{"host": "192.0.2.10"}'}


def test_tool_call_arguments_split_across_frames_are_restored():
    raw = _run([_tool_frame(0, '{"host": "<PII_n1_IP'),
                _tool_frame(0, '_PRIVATE_1>"}'),
                b"data: [DONE]\n\n"])
    assert _tool_args(raw) == {0: '{"host": "192.0.2.10"}'}


def test_tool_call_arguments_still_parse_as_json():
    raw = _run([_tool_frame(0, '{"a": "<PII_n1_IP_PRIVATE_1>", "b": 2}'), b"data: [DONE]\n\n"])
    assert json.loads(_tool_args(raw)[0]) == {"a": "192.0.2.10", "b": 2}


def test_parallel_tool_calls_do_not_share_a_buffer():
    # A partial placeholder in call 0 must not be completed by call 1's text.
    raw = _run([_tool_frame(0, '{"x": "<PII_n1_IP'),
                _tool_frame(1, '{"y": "<PII_n1_IP_PRIVATE_2>"}'),
                _tool_frame(0, '_PRIVATE_1>"}'),
                b"data: [DONE]\n\n"])
    assert _tool_args(raw) == {0: '{"x": "192.0.2.10"}', 1: '{"y": "192.0.2.20"}'}


def test_tool_argument_tail_flushed_when_stream_ends_without_done():
    raw = _run([_tool_frame(0, '{"host": "<PII_n1_IP_PRIVATE_1>"}')])
    assert _tool_args(raw) == {0: '{"host": "192.0.2.10"}'}


def test_tool_call_name_and_index_preserved():
    raw = _run([_tool_frame(0, '{"host": "<PII_n1_IP_PRIVATE_1>"}', name="deploy"),
                b"data: [DONE]\n\n"])
    assert '"name": "deploy"' in raw.decode()
    assert _tool_args(raw) == {0: '{"host": "192.0.2.10"}'}


def test_tool_call_index_omitted_falls_back_to_position():
    frame = ("data: " + json.dumps({"choices": [{"index": 0, "delta": {
        "tool_calls": [{"function": {"arguments": '{"h": "<PII_n1_IP_PRIVATE_1>"}'}}]},
        "finish_reason": None}]}) + "\n\n").encode()
    assert "192.0.2.10" in _run([frame, b"data: [DONE]\n\n"]).decode()


def test_ndjson_tool_call_arguments_rehydrated():
    line = (json.dumps({"message": {"role": "assistant", "tool_calls": [
        {"function": {"name": "deploy",
                      "arguments": {"host": "<PII_n1_IP_PRIVATE_1>", "port": 22}}}]},
        "done": True}) + "\n").encode()

    async def gen():
        yield line

    async def collect():
        return [x async for x in main._rehydrate_ndjson(gen(), MAPPING)]

    out = json.loads(b"".join(asyncio.run(collect())).decode().strip())
    args = out["message"]["tool_calls"][0]["function"]["arguments"]
    assert args == {"host": "192.0.2.10", "port": 22}


def test_ndjson_tool_call_arguments_nested_and_as_string():
    line = (json.dumps({"message": {"role": "assistant", "tool_calls": [
        {"function": {"arguments": {"outer": {"hosts": ["<PII_n1_IP_PRIVATE_1>"]}}}},
        {"function": {"arguments": '{"host": "<PII_n1_IP_PRIVATE_2>"}'}}]},
        "done": True}) + "\n").encode()

    async def gen():
        yield line

    async def collect():
        return [x async for x in main._rehydrate_ndjson(gen(), MAPPING)]

    calls = json.loads(b"".join(asyncio.run(collect())).decode().strip())["message"]["tool_calls"]
    assert calls[0]["function"]["arguments"] == {"outer": {"hosts": ["192.0.2.10"]}}
    assert calls[1]["function"]["arguments"] == '{"host": "192.0.2.20"}'
