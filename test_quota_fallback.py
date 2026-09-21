# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Cloud-quota fallback tests.

Ollama Cloud's free tier answers `429 ... you have reached your session usage
limit`. That is a property of the account, not the request, so no retry against
that tier can work -- observed as a Pi session dying mid-conversation after the
`*-auto` lane escalated past its 45% threshold. The local tier can still answer,
so the lane drops back to it instead of ending the session.

Run: .venv/bin/python -m pytest test_quota_fallback.py -q
"""

import asyncio
import json

import main


def _drain(agen):
    async def go():
        return [c async for c in agen]
    return asyncio.run(go())


def _make_primary(status=None, chunks=(b"real-content",)):
    """Stands in for _supervised_stream: on an error it notes the status on the
    trace context and yields exactly one terminal chunk, which is what makes a
    one-chunk peek sufficient to decide."""
    async def gen():
        if status is not None:
            main.tracing.note(upstream_status=status)
            yield b"terminal-error-chunk"
            return
        for c in chunks:
            yield c
    return gen()


def _make_fallback(chunks=(b"local-a", b"local-b")):
    async def gen():
        for c in chunks:
            yield c
    return gen


def test_healthy_stream_passes_through_unchanged():
    main.tracing.start("t-ok")
    out = _drain(_quota(_make_primary(chunks=[b"a", b"b", b"c"])))
    assert out == [b"a", b"b", b"c"]


def _quota(primary, fallback=None):
    return main._quota_fallback_stream(primary, fallback or _make_fallback())


def test_429_swaps_to_local_and_drops_the_error_chunk():
    main.tracing.start("t-429")
    out = _drain(_quota(_make_primary(status=429)))
    assert out == [b"local-a", b"local-b"]
    assert b"terminal-error-chunk" not in b"".join(out)


def test_429_clears_marker_so_outcome_is_not_reported_as_an_error():
    ctx = main.tracing.start("t-clear")
    _drain(_quota(_make_primary(status=429)))
    assert "upstream_status" not in ctx


def test_402_subscription_required_also_swaps_to_local():
    # Arrived after 429: a model that had been free moved behind a subscription
    # ("this model requires a subscription or extra usage") and killed sessions
    # identically. Same account-level shape, same recovery.
    main.tracing.start("t-402")
    out = _drain(_quota(_make_primary(status=402)))
    assert out == [b"local-a", b"local-b"]


def test_non_quota_errors_are_not_swapped():
    # A 400 is about THIS request; answering it from another model would hide a
    # real bug behind a silently different answer.
    main.tracing.start("t-400")
    out = _drain(_quota(_make_primary(status=400)))
    assert out == [b"terminal-error-chunk"]


def test_empty_primary_yields_nothing():
    main.tracing.start("t-empty")
    out = _drain(_quota(_make_primary(chunks=[])))
    assert out == []


def test_primary_is_closed_so_inflight_count_does_not_leak():
    # _supervised_stream parks at its yield inside a try/finally that decrements
    # the per-model in-flight counter; abandoning it would leave the count high.
    closed = []

    async def primary():
        try:
            main.tracing.note(upstream_status=429)
            yield b"terminal-error-chunk"
        finally:
            closed.append(True)

    main.tracing.start("t-close")
    _drain(_quota(primary()))
    assert closed == [True]


# --- _compact_for_local ---------------------------------------------------------

def _body(n_chars):
    return {"model": "m", "messages": [
        {"role": "user", "content": "x " * n_chars},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "and now"},
    ]}


def test_compaction_skipped_when_it_already_fits():
    body = _body(10)
    out = asyncio.run(main._compact_for_local(
        body, "main", "local-m", 10**9, reason="r", original_model="o"))
    assert out is body          # untouched, no summarizer hop


def test_compaction_runs_when_over_the_local_window(monkeypatch):
    calls = []

    async def fake_compact(body, summarize):
        calls.append(True)
        return {"model": body["model"], "messages": [{"role": "user", "content": "summary"}]}

    monkeypatch.setattr(main, "compact_messages", fake_compact)
    out = asyncio.run(main._compact_for_local(
        _body(5000), "main", "local-m", 10, reason="r", original_model="o"))
    assert calls == [True]
    assert out["messages"][0]["content"] == "summary"


def test_only_account_level_statuses_swap():
    # The whole set, asserted in one place so widening it stays a deliberate act.
    for status, should_swap in [(402, True), (429, True),
                                (400, False), (404, False), (500, False), (503, False)]:
        main.tracing.start(f"t-{status}")
        out = _drain(_quota(_make_primary(status=status)))
        swapped = out == [b"local-a", b"local-b"]
        assert swapped is should_swap, f"status {status}: swapped={swapped}"


# --- silent tier ----------------------------------------------------------------
# On 2026-09-08 ollama.com accepted connections and returned zero bytes, for
# every model and both API dialects. There is no status to key off, so
# _supervised_stream marks the trace context instead.

def _make_silent_primary(silent=True, chunks=(b"terminal-frame",)):
    async def gen():
        main.tracing.note(upstream_error_type="ReadTimeout",
                          upstream_silent=silent)
        for c in chunks:
            yield c
    return gen()


def test_silent_tier_swaps_to_local():
    main.tracing.start("t-silent")
    out = _drain(_quota(_make_silent_primary()))
    assert out == [b"local-a", b"local-b"]


def test_silent_tier_terminal_frame_is_dropped():
    # The caller must not receive both an upstream_timeout finish_reason and a
    # real answer -- the frame the primary emitted is the one being replaced.
    main.tracing.start("t-silent-drop")
    out = _drain(_quota(_make_silent_primary()))
    assert b"terminal-frame" not in b"".join(out)


def test_all_markers_cleared_so_a_recovered_request_reports_ok():
    # Leaving upstream_error_type behind would make _traced_stream record a
    # request that recovered and answered successfully as a transport failure.
    ctx = main.tracing.start("t-silent-clear")
    _drain(_quota(_make_silent_primary()))
    for key in ("upstream_status", "upstream_silent", "upstream_error_type"):
        assert key not in ctx, key


def test_failure_after_output_does_not_swap():
    # A tier that generated and then dropped keeps what it sent. Swapping would
    # stitch a second, contradictory answer onto the first.
    main.tracing.start("t-not-silent")
    out = _drain(_quota(_make_silent_primary(
        silent=False, chunks=(b"real-content", b"terminal-frame"))))
    assert out == [b"real-content", b"terminal-frame"]


def test_transport_error_alone_is_not_enough_to_swap():
    # The trigger is `upstream_silent`, not "a transport error happened".
    main.tracing.start("t-typed-only")

    async def primary():
        main.tracing.note(upstream_error_type="ReadError")
        yield b"real-content"

    assert _drain(_quota(primary())) == [b"real-content"]


def test_the_trigger_is_recorded_before_the_markers_are_cleared():
    # The routing log for the fallback is written after the markers are
    # dropped, so without this a log reading "cloud-fallback" could not say
    # whether the tier was silent or the account was refused -- two different
    # operational stories.
    ctx = main.tracing.start("t-why-silent")
    _drain(_quota(_make_silent_primary()))
    assert ctx["fallback_trigger"] == "silent"

    ctx = main.tracing.start("t-why-quota")
    _drain(_quota(_make_primary(status=429)))
    assert ctx["fallback_trigger"] == "quota"
