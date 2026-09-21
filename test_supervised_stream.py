# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""_supervised_stream tests: forwarding must be unconditional (fail-open) --
parsing exists only to feed token accounting / kill detection, never to gate
what reaches the client. A prior version of this file locked in a CoT-buffer-
until-`</think>`-closes design that turned out to silently drop 100% of
output on the openai-compat path (SSE frames never matched the parser at
all) and effectively serialize the native path into buffer-everything since
this deployment's models keep thinking in a separate field (`message.thinking`
/ `delta.reasoning`) and never emit a literal `<think>` tag in content at all
-- the tag the old code was watching for never appeared, so the "flush at
done" fallback was the only thing saving `/api/chat` from being fully broken
too. Offline, no real Ollama/network needed - a fake client stands in for
httpx.AsyncClient.
Run: .venv/bin/python -m pytest test_supervised_stream.py -q
"""

import asyncio
import json

import main


class _FakeStream:
    # status_code defaults to 200 so every pre-existing case keeps exercising the
    # normal forwarding path unchanged; only the upstream-error tests set it.
    def __init__(self, chunks, clock, times, status_code=200, error_body=b""):
        self._chunks = chunks
        self._clock = clock
        self._times = times
        self.status_code = status_code
        self._error_body = error_body

    async def aread(self):
        return self._error_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        for chunk, t in zip(self._chunks, self._times):
            self._clock[0] = t
            yield chunk

    async def aclose(self):
        pass


class _FakeClient:
    def __init__(self, chunks, clock, times, status_code=200, error_body=b""):
        self._chunks = chunks
        self._clock = clock
        self._times = times
        self._status_code = status_code
        self._error_body = error_body

    def stream(self, method, path, json=None, headers=None):
        return _FakeStream(self._chunks, self._clock, self._times,
                           self._status_code, self._error_body)


def _line(**kw) -> bytes:
    return (json.dumps(kw) + "\n").encode()


def _sse(**kw) -> bytes:
    return f"data: {json.dumps(kw)}\n\n".encode()


def _run(chunks, times, path="/api/chat", status_code=200, error_body=b"",
         tools=False):
    # `tools` drives which idle budget the supervisor applies -- it reads the
    # request body, the same signal body_read already logs.
    body = {"model": "m"}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": "write_file"}}]
    clock = [0.0]
    client = _FakeClient(chunks, clock, times, status_code, error_body)
    orig_time = main.time.time
    main.time.time = lambda: clock[0]
    try:
        async def collect():
            out = []
            async for piece in main._supervised_stream(
                client, "POST", path, body, "m", "req-1", {},
            ):
                out.append(piece)
            return out
        return asyncio.run(collect())
    finally:
        main.time.time = orig_time


def test_content_forwarded_immediately_unconditionally():
    # The core invariant: every chunk from upstream reaches the caller
    # exactly as received, regardless of whether it parses as JSON, has a
    # `<think>` tag, or is otherwise unexpected in shape. A version of this
    # code that only forwarded on successful parse + gate-open silently
    # dropped 100% of output for a request shape (openai SSE) its parser
    # didn't recognize -- this test is the direct regression guard for that.
    chunks = [
        _line(message={"role": "assistant", "content": "thinking hard </think>real"}, done=False),
        _line(message={"role": "assistant", "content": " answer continues"}, done=False),
        b"not even json\n",
        _line(message={"role": "assistant", "content": ""}, done=True, done_reason="stop"),
    ]
    out = _run(chunks, times=[0.0, 0.1, 0.2, 0.3])
    assert out == chunks


def test_thinking_field_counted_for_stall_detection_without_being_forwarded_separately():
    # message.thinking (native Ollama's own CoT/content separation) must
    # still count as real token activity for stall detection -- a long
    # thinking-only phase shouldn't spuriously trip the stall kill just
    # because `content` stayed empty the whole time.
    main_th = dict(main.TH)
    main.TH["stall_idle_s"] = 60
    try:
        chunks = [
            _line(message={"role": "assistant", "content": "", "thinking": "a lot of reasoning here"}, done=False),
            _line(message={"role": "assistant", "content": "answer"}, done=False),
            _line(message={"role": "assistant", "content": ""}, done=True, done_reason="stop"),
        ]
        out = _run(chunks, times=[0.0, 10.0, 10.1])
        assert out == chunks  # no kill chunk appended
    finally:
        main.TH.clear()
        main.TH.update(main_th)


def test_stall_kill_forwards_prior_content_then_appends_well_formed_marker():
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 5
    try:
        chunks = [
            _line(message={"role": "assistant", "content": "some content"}, done=False),
            _line(message={"role": "assistant", "content": " more"}, done=False),
        ]
        # second chunk arrives 100s later at ~0 tokens/sec -> stall kill.
        out = _run(chunks, times=[0.0, 100.0])
        # both real chunks still forwarded (fail-open), then a kill marker.
        assert out[0] == chunks[0]
        assert out[1] == chunks[1]
        killed = json.loads(out[2])
        assert killed["done"] is True
        assert killed["done_reason"] == "agentstop_stall"
        assert killed["message"]["content"] == ""
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_generate_endpoint_kill_chunk_uses_response_field():
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 5
    try:
        # Two chunks: the first establishes activity, the second arrives after
        # a gap. A single late chunk is time-to-first-byte, not a stall.
        chunks = [_line(response="some text", done=False),
                  _line(response=" more", done=False)]
        out = _run(chunks, times=[0.0, 100.0], path="/api/generate")
        killed = json.loads(out[-1])
        assert killed["done_reason"] == "agentstop_stall"
        assert killed["response"] == ""
        assert "message" not in killed
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_openai_sse_forwarded_verbatim_including_done_sentinel():
    chunks = [
        _sse(choices=[{"index": 0, "delta": {"reasoning": "thinking"}, "finish_reason": None}]),
        _sse(choices=[{"index": 0, "delta": {"content": "answer"}, "finish_reason": None}]),
        _sse(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        b"data: [DONE]\n\n",
    ]
    out = _run(chunks, times=[0.0, 0.1, 0.2, 0.3], path="/v1/chat/completions")
    assert out == chunks


def test_openai_kill_chunk_shape():
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 5
    try:
        chunks = [_sse(choices=[{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]),
                  _sse(choices=[{"index": 0, "delta": {"content": " there"}, "finish_reason": None}])]
        out = _run(chunks, times=[0.0, 100.0], path="/v1/chat/completions")
        assert out[0] == chunks[0]
        kill_text = out[-1].decode()
        assert kill_text.startswith("data: ")
        assert kill_text.rstrip().endswith("data: [DONE]")
        payload = json.loads(kill_text.split("\n\n")[0][len("data: "):])
        assert payload["choices"][0]["finish_reason"] == "agentstop_stall"
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


# --- upstream HTTP errors -------------------------------------------------------
# Regression cases for the incident these were written after: Ollama Cloud answered
# `400 invalid tool call arguments`, _supervised_stream forwarded that error JSON as
# if it were tokens, and the stream ended with no finish_reason. Pi reported "Stream
# ended without finish_reason" and retried 3x against a perfectly deterministic 400,
# while the logs recorded every one of them as outcome "ok".

_ERR_BODY = (b'{"error":{"message":"invalid tool call arguments (ref: x)",'
             b'"type":"invalid_request_error","param":null,"code":null}}')


def test_upstream_400_not_forwarded_and_terminates_sse_with_finish_reason():
    out = _run([b"never sent"], times=[0.0], path="/v1/chat/completions",
               status_code=400, error_body=_ERR_BODY)
    joined = b"".join(out)
    # the raw error document must not reach the client as if it were content
    assert b"invalid_request_error" not in joined
    text = joined.decode()
    assert text.rstrip().endswith("data: [DONE]")
    payload = json.loads(text.split("\n\n")[0][len("data: "):])
    assert payload["choices"][0]["finish_reason"] == "upstream_error_400"


def test_upstream_500_terminates_ndjson_with_done_reason():
    out = _run([b"never sent"], times=[0.0], path="/api/chat",
               status_code=500, error_body=b'{"error":"boom"}')
    obj = json.loads(b"".join(out).decode())
    assert obj["done"] is True
    assert obj["done_reason"] == "upstream_error_500"
    assert obj["message"] == {"role": "assistant", "content": ""}


def test_upstream_error_recorded_on_trace_context_for_outcome():
    ctx = main.tracing.start("trace-upstream")
    _run([b"never sent"], times=[0.0], path="/v1/chat/completions",
         status_code=429, error_body=b'{"error":"slow down"}')
    assert ctx["upstream_status"] == 429


# --- response content type ------------------------------------------------------
# The /v1 lane emits SSE but every streaming response was labelled NDJSON, so a
# client choosing its parser from the header could point a line-splitting reader
# at a framed protocol.

def test_openai_path_declares_sse():
    assert main._stream_media_type("/v1/chat/completions") == "text/event-stream"


def test_native_ollama_paths_declare_ndjson():
    for p in ("/api/chat", "/api/generate", "/api/chat/"):
        assert main._stream_media_type(p) == "application/x-ndjson"


def test_media_type_matches_the_frames_actually_emitted():
    # The label has to agree with _kill_chunk's wire shape for the same path,
    # or the two can drift apart silently.
    sse = main._kill_chunk("m", "stall", is_chat=True, is_openai=True)
    assert sse.startswith(b"data: ")
    assert main._stream_media_type("/v1/chat/completions") == "text/event-stream"
    nd = main._kill_chunk("m", "stall", is_chat=True, is_openai=False)
    assert not nd.startswith(b"data: ")
    assert main._stream_media_type("/api/chat") == "application/x-ndjson"


# --- tool-call activity ---------------------------------------------------------
# Regression cases for the incident these were written after: a real agent turn
# is mostly tool calls, and _stream_piece reads only content/reasoning. The
# supervisor therefore saw an idle stream while 12KB of healthy output flowed
# past, and killed Pi turns at ~42s having counted ~16 tokens out of 12589
# bytes. Probed live: a pure tool-call response was 3510 bytes on the wire,
# 2730 chars of arguments, and exactly 0 counted tokens.


def _sse_tool(args: str, index: int = 0) -> bytes:
    return _sse(choices=[{"index": 0, "delta": {"tool_calls": [
        {"index": index, "function": {"arguments": args}}]}, "finish_reason": None}])


def test_openai_tool_call_arguments_count_as_activity():
    # `arguments` is a STRING on the OpenAI-compat path.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 45
    try:
        # A long tool call streamed in small pieces 2s apart across a 60s turn
        # -- the shape a real agent write_file emits.
        body = ['{"path": "/tmp/f.py", "content": "'] + \
               ['def fib(n): return n\\n'] * 28 + ['"}']
        chunks = [
            _sse(choices=[{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]),
        ] + [_sse_tool(piece) for piece in body]
        times = [2.0 * i for i in range(len(chunks))]
        assert times[-1] >= 60  # a full minute of wall clock

        # Under the OLD cumulative average this was fatal: the only counted
        # token in the whole turn is "x", so 1/60 = 0.017 tok/s against a 0.5
        # floor. Every one of these chunks is real activity, and no gap comes
        # near stall_idle_s.
        out = _run(chunks, times=times, path="/v1/chat/completions")
        assert out == chunks  # no kill marker appended
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_native_tool_call_arguments_count_as_activity():
    # `arguments` is a DICT on native /api/chat -- verified against the live
    # model. A str-only reader would silently score this as zero activity.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 45
    try:
        chunks = [
            _line(message={"role": "assistant", "content": "x"}, done=False),
            _line(message={"role": "assistant", "tool_calls": [
                {"function": {"name": "write_file",
                              "arguments": {"path": "/tmp/f.py",
                                            "content": "def fib(n): return n"}}}]},
                  done=False),
            _line(message={"role": "assistant", "tool_calls": [
                {"function": {"name": "write_file",
                              "arguments": {"path": "/tmp/g.py",
                                            "content": "def fac(n): return n"}}}]},
                  done=False),
        ]
        out = _run(chunks, times=[0.0, 20.0, 40.0])
        assert out == chunks  # 20s gaps, all under stall_idle_s
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_tool_call_arguments_do_not_feed_ngram_loop_detection():
    # Tool arguments are structured JSON: keys, braces and indentation repeat by
    # nature. Feeding them to a 0.7-overlap detector would just swap the
    # false-stall class for a false-ngram_loop one, so they count as activity
    # without entering token_buf. Identical repeated calls must NOT be killed.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 5
    main.TH["ngram_overlap_ratio"] = 0.1   # trivially trippable
    main.TH["ngram_size"] = 2
    main.TH["ngram_window"] = 16
    try:
        same = '{"path": "/tmp/a", "content": "aaa bbb aaa bbb aaa bbb"}'
        chunks = [
            _sse(choices=[{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]),
        ] + [_sse_tool(same) for _ in range(6)]
        out = _run(chunks, times=[float(i) for i in range(len(chunks))],
                   path="/v1/chat/completions")
        assert out == chunks
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_burst_then_freeze_is_killed_on_the_idle_gap():
    # The failure the old cumulative average MISSED. A fast burst keeps
    # total/elapsed above the floor long after the stream has actually died, so
    # a hung request survived to max_wall_seconds (900s). The gap since the last
    # activity is what actually matters.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 30
    try:
        burst = [_line(message={"role": "assistant", "content": f"tok{i} " * 20},
                       done=False) for i in range(5)]
        chunks = burst + [_line(message={"role": "assistant", "content": ""}, done=False)]
        times = [0.0, 0.1, 0.2, 0.3, 0.4, 400.0]
        out = _run(chunks, times=times)
        killed = json.loads(out[-1])
        assert killed["done_reason"] == "agentstop_stall"
        # and it fired on the gap, not on the 900s wall clock
        assert times[-1] < main.TH["max_wall_seconds"]
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_stream_tool_args_handles_both_shapes_and_junk():
    assert main._stream_tool_args(
        {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": '{"a":1}'}}]}}]},
        True) == '{"a":1}'
    assert main._stream_tool_args(
        {"message": {"tool_calls": [{"function": {"arguments": {"a": 1}}}]}},
        False) == '{"a": 1}'
    # partial / absent / malformed frames must be inert, never raise
    assert main._stream_tool_args({}, True) == ""
    assert main._stream_tool_args({}, False) == ""
    assert main._stream_tool_args({"message": {"tool_calls": "nope"}}, False) == ""
    assert main._stream_tool_args({"message": {"tool_calls": [None]}}, False) == ""
    assert main._stream_tool_args({"message": {"tool_calls": [{"function": {}}]}}, False) == ""


# --- Ollama's tool-call buffering window ----------------------------------------
# Ollama does not stream tool-call arguments incrementally on EITHER api: it
# buffers the whole call and emits one frame when the model finishes. Probed
# against Ollama directly, this proxy out of the path, writing a 250-line file
# into a tool argument -- 31s, 42s and 52s of total wire silence, and the real
# Pi failure was a 64s window. There is no liveness signal to be clever with,
# so a tools-capable request simply gets a budget longer than its tool calls.


def test_tools_request_survives_the_buffering_silence():
    # The exact shape of the 13:21 production failure: reasoning streams (which
    # ARMS the detector by making total_tokens > 0), then the wire goes dead for
    # 64s while Ollama buffers the tool call.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 45
    main.TH["stall_idle_tools_s"] = 240
    try:
        chunks = [
            _sse(choices=[{"index": 0, "delta": {"reasoning": "thinking hard"},
                           "finish_reason": None}]),
            _sse_tool('{"path": "/tmp/big.py", "content": "...250 lines..."}'),
        ]
        out = _run(chunks, times=[0.0, 64.0], path="/v1/chat/completions",
                   tools=True)
        assert out == chunks  # no kill marker
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_same_silence_without_tools_is_still_killed():
    # The budget is not a blanket relaxation: a request that cannot call tools
    # has no buffering excuse, so 64s of silence is still a stall.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 45
    main.TH["stall_idle_tools_s"] = 240
    try:
        chunks = [
            _sse(choices=[{"index": 0, "delta": {"content": "hi"},
                           "finish_reason": None}]),
            _sse(choices=[{"index": 0, "delta": {"content": " there"},
                           "finish_reason": None}]),
        ]
        out = _run(chunks, times=[0.0, 64.0], path="/v1/chat/completions",
                   tools=False)
        payload = json.loads(out[-1].decode().split("\n\n")[0][len("data: "):])
        assert payload["choices"][0]["finish_reason"] == "agentstop_stall"
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_tools_request_is_still_killed_past_the_larger_budget():
    # Detection is deferred, not disabled -- a genuinely hung agent turn still
    # dies, just on the longer clock.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 45
    main.TH["stall_idle_tools_s"] = 240
    try:
        chunks = [
            _line(message={"role": "assistant", "content": "starting"}, done=False),
            _line(message={"role": "assistant", "content": "x"}, done=False),
        ]
        out = _run(chunks, times=[0.0, 300.0], tools=True)
        killed = json.loads(out[-1])
        assert killed["done_reason"] == "agentstop_stall"
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_kill_record_says_which_budget_applied():
    # Without this the two budgets are indistinguishable in the log, and the
    # next person triaging a stall cannot tell whether 60s was over or under.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 45
    main.TH["stall_idle_tools_s"] = 240
    recorded = []
    orig_log = main._log_kill
    main._log_kill = lambda reason, rec: recorded.append((reason, rec))
    try:
        chunks = [
            _line(message={"role": "assistant", "content": "hi"}, done=False),
            _line(message={"role": "assistant", "content": "x"}, done=False),
        ]
        _run(chunks, times=[0.0, 300.0], tools=True)
        reason, rec = recorded[-1]
        assert reason == "stall"
        assert rec["limit"] == 240
        assert rec["tools"] is True
    finally:
        main._log_kill = orig_log
        main.TH.clear()
        main.TH.update(orig_th)


# --- time-to-first-byte is not idle time ----------------------------------------
# A 220KB compaction request -- Pi squashing a whole conversation into 2 messages
# to summarize -- spent 76s in prefill on the local 35B and was killed the instant
# its first token arrived, with idle == elapsed == ttfb == 76.5s against a 45s
# budget. Pi surfaced it as "Compaction failed: Summarization failed".


def test_long_prefill_is_not_a_stall():
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 45
    try:
        # Nothing for 76s (prefill), then the stream runs normally.
        chunks = [
            _line(message={"role": "assistant", "content": "Summary"}, done=False),
            _line(message={"role": "assistant", "content": " continues"}, done=False),
            _line(message={"role": "assistant", "content": ""}, done=True,
                  done_reason="stop"),
        ]
        out = _run(chunks, times=[76.5, 76.6, 76.7])
        assert out == chunks  # no kill marker appended
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_gap_after_first_token_is_still_a_stall():
    # The other half: once the stream HAS produced something, the idle clock is
    # live. Deferring the start must not disable detection.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 45
    try:
        chunks = [
            _line(message={"role": "assistant", "content": "Summary"}, done=False),
            _line(message={"role": "assistant", "content": " x"}, done=False),
        ]
        out = _run(chunks, times=[76.5, 200.0])   # 123s gap after real activity
        killed = json.loads(out[-1])
        assert killed["done_reason"] == "agentstop_stall"
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


def test_frames_with_no_activity_do_not_start_the_idle_clock():
    # Keepalive-ish frames carrying no content/reasoning/tool args must not be
    # mistaken for first activity, or prefill becomes killable again.
    orig_th = dict(main.TH)
    main.TH["stall_idle_s"] = 45
    try:
        chunks = [
            _line(message={"role": "assistant", "content": ""}, done=False),
            _line(message={"role": "assistant", "content": ""}, done=False),
            _line(message={"role": "assistant", "content": "finally"}, done=False),
        ]
        out = _run(chunks, times=[10.0, 60.0, 120.0])
        assert out == chunks
    finally:
        main.TH.clear()
        main.TH.update(orig_th)


# ---------------------------------------------------------------------------
# Transport failures: the tier accepted the connection and never answered, or
# dropped it mid-flight. Before this was caught, httpx.ReadTimeout propagated
# out of the response body iterator AFTER headers were already sent, so the
# body simply stopped -- reported by OpenAI-compat clients as "stream ended
# without finish_reason", which they then retry. On 2026-09-08 that turned an
# ollama.com outage into a retry storm the repeat-guard rejected with a 400
# that named the symptom rather than the cause.
# ---------------------------------------------------------------------------

class _RaisingStream:
    """Fails either on open (nothing ever streamed) or partway through."""

    def __init__(self, chunks, clock, times, exc, raise_on_open):
        self._chunks = chunks
        self._clock = clock
        self._times = times
        self._exc = exc
        self._raise_on_open = raise_on_open
        self.status_code = 200
        self.closed = False

    async def __aenter__(self):
        if self._raise_on_open:
            raise self._exc
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        for chunk, t in zip(self._chunks, self._times):
            self._clock[0] = t
            yield chunk
        raise self._exc

    async def aclose(self):
        self.closed = True


class _RaisingClient:
    def __init__(self, chunks, clock, times, exc, raise_on_open):
        self._args = (chunks, clock, times, exc, raise_on_open)

    def stream(self, method, path, json=None, headers=None):
        return _RaisingStream(*self._args)


def _run_failing(exc, chunks=(), times=(), raise_on_open=True,
                 path="/api/chat"):
    """Drive _supervised_stream against a failing transport.

    Returns (emitted_chunks, trace_ctx) so the terminal frame and the two
    context markers can be asserted together.
    """
    clock = [0.0]
    client = _RaisingClient(list(chunks), clock, list(times), exc, raise_on_open)
    ctx = main.tracing.start("t-transport")
    orig_time = main.time.time
    main.time.time = lambda: clock[0]
    try:
        async def collect():
            out = []
            async for piece in main._supervised_stream(
                client, "POST", path, {"model": "m"}, "m", "req-1", {},
            ):
                out.append(piece)
            return out
        return asyncio.run(collect()), ctx
    finally:
        main.time.time = orig_time


def _finish_reason(sse_bytes: bytes) -> str:
    first = sse_bytes.decode().split("\n\n")[0]
    return json.loads(first.removeprefix("data: "))["choices"][0]["finish_reason"]


def test_timeout_on_open_yields_one_terminal_chunk_not_an_exception():
    # The whole point: the caller gets a readable end instead of a truncated
    # body. Without this the exception escaped and Starlette could no longer
    # turn it into anything, because headers were already on the wire.
    out, _ = _run_failing(main.httpx.ReadTimeout("timed out"), path="/v1/chat/completions")
    assert len(out) == 1
    assert _finish_reason(out[0]) == "upstream_timeout"
    assert out[0].endswith(b"data: [DONE]\n\n")


def test_timeout_before_any_byte_is_marked_silent():
    _, ctx = _run_failing(main.httpx.ReadTimeout("timed out"))
    assert ctx["upstream_error_type"] == "ReadTimeout"
    assert ctx["upstream_silent"] is True


def test_failure_after_real_output_is_not_marked_silent():
    # A tier that generated for a while and then dropped is much weaker
    # evidence that it is down -- it just proved it works. The cooldown reads
    # this flag, so getting it wrong would park a healthy tier on local.
    chunks = [_line(message={"role": "assistant", "content": "hi"}, done=False)]
    out, ctx = _run_failing(main.httpx.ReadError("reset"), chunks=chunks,
                            times=[1.0], raise_on_open=False)
    assert ctx["upstream_silent"] is False
    assert out[0] == chunks[0]          # everything already sent still stands
    assert len(out) == 2                # plus the terminal frame


def test_connect_error_is_handled_like_a_timeout():
    # ConnectError and RemoteProtocolError mean the same thing operationally
    # as a timeout, which is why the handler catches TransportError rather
    # than TimeoutException.
    out, ctx = _run_failing(main.httpx.ConnectError("refused"))
    assert ctx["upstream_error_type"] == "ConnectError"
    assert len(out) == 1


def test_native_path_terminal_chunk_shape():
    out, _ = _run_failing(main.httpx.ReadTimeout("t"), path="/api/chat")
    obj = json.loads(out[0])
    assert obj["done"] is True
    assert obj["done_reason"] == "upstream_timeout"
    assert obj["message"] == {"role": "assistant", "content": ""}


def test_generate_path_uses_response_not_message():
    out, _ = _run_failing(main.httpx.ReadTimeout("t"), path="/api/generate")
    obj = json.loads(out[0])
    assert obj["response"] == ""
    assert "message" not in obj


def test_in_flight_counter_decrements_on_transport_error():
    # The handler sits in the same try as the `finally: _dec(model)`, so a
    # mistake in that structure would leak the counter and make every later
    # concurrency decision wrong.
    before = main._active.get("m", 0)
    _run_failing(main.httpx.ReadTimeout("t"))
    assert main._active.get("m", 0) == before

    # And prove the assertion above can actually fail, rather than passing
    # because the counter is untouched on every path: a normal run moves it.
    seen = []
    orig_inc = main._inc
    main._inc = lambda m: (seen.append(main._active.get(m, 0) + 1), orig_inc(m))[1]
    try:
        _run_failing(main.httpx.ReadTimeout("t"))
    finally:
        main._inc = orig_inc
    assert seen == [before + 1]


def test_upstream_error_chunk_is_unchanged_for_a_real_status():
    # The transport frame is a sibling, not a refactor of _upstream_error_chunk.
    # This guards that the 4xx/5xx path still emits exactly what it did.
    got = main._upstream_error_chunk("m", 429, is_chat=True, is_openai=True)
    assert _finish_reason(got) == "upstream_error_429"
    assert b"deflector-upstream-429" in got
