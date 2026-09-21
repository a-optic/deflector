# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Request-lifecycle tracing tests.

The `complete` event exists because a real outage produced a client that sat
for 300s and received 0 bytes, with nothing recorded anywhere about how or why
the stream ended. `test_complete_on_client_disconnect` is that incident as a
regression test.
Run: .venv/bin/python -m pytest tests/test_tracing.py -q
"""

import asyncio
import json

import pytest

import main
import retention
import tracing


def _events(log_dir, ev=None):
    # files are date-stamped now; resolve the same way the writers do
    p = retention.log_path(log_dir, "requests")
    if not p.exists():
        return []
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return [r for r in rows if ev is None or r.get("ev") == ev]


async def _drive(gen):
    return [c async for c in gen]


def test_complete_on_normal_stream(isolate_log_dir):
    async def scenario():
        tracing.start("t-normal")

        async def inner():
            yield b"abc"
            yield b"de"

        out = await _drive(main._traced_stream(inner(), "m", "req-1"))
        assert out == [b"abc", b"de"]

    asyncio.run(scenario())
    ev = _events(isolate_log_dir, "complete")
    assert len(ev) == 1
    assert ev[0]["outcome"] == "ok"
    assert ev[0]["bytes_out"] == 5
    assert ev[0]["chunks"] == 2
    assert ev[0]["ttfb"] is not None
    assert ev[0]["id"] == "t-normal"


def test_complete_on_client_disconnect(isolate_log_dir):
    """The 300s/0-byte outage, as a test.

    A client that hangs up mid-stream must produce outcome=client_disconnect
    with the bytes actually delivered -- previously this produced no record at
    all, which is what made the incident take hours to diagnose.
    """
    async def scenario():
        tracing.start("t-disconnect")
        started = asyncio.Event()

        async def inner():
            yield b"partial"
            started.set()
            await asyncio.sleep(60)   # never completes; client gives up first
            yield b"never"            # pragma: no cover

        async def consume():
            async for _ in main._traced_stream(inner(), "m", "req-2"):
                pass

        task = asyncio.create_task(consume())
        await started.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # let the generator's finally run during teardown
        await asyncio.sleep(0)

    asyncio.run(scenario())
    ev = _events(isolate_log_dir, "complete")
    assert len(ev) == 1
    assert ev[0]["outcome"] == "client_disconnect"
    assert ev[0]["bytes_out"] == 7          # what actually reached the client
    assert ev[0]["id"] == "t-disconnect"


def test_kill_reason_surfaces_in_complete(isolate_log_dir):
    """A supervisor kill must not look like a clean end.

    _supervised_stream yields its kill chunk and then returns NORMALLY, so
    without the recorded reason the outcome would read as "ok".
    """
    async def scenario():
        tracing.start("t-kill")

        async def inner():
            yield b"some output"
            main._log_kill("stall", {"id": "req-3", "tps": 0.0})

        await _drive(main._traced_stream(inner(), "m", "req-3"))

    asyncio.run(scenario())
    ev = _events(isolate_log_dir, "complete")
    assert ev[0]["outcome"] == "kill:stall"


def test_error_outcome_is_typed(isolate_log_dir):
    async def scenario():
        tracing.start("t-err")

        async def inner():
            yield b"x"
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await _drive(main._traced_stream(inner(), "m", "req-4"))

    asyncio.run(scenario())
    assert _events(isolate_log_dir, "complete")[0]["outcome"] == "error:ValueError"


def test_trace_id_correlates_across_all_log_files(isolate_log_dir):
    """The whole point: one id joins requests/routing/kills/lifeos logs.

    Correlating these by hand, via timestamps, is what made the last
    investigation slow.
    """
    async def scenario():
        tracing.start("t-join")
        main._log_route("test-reason", "orig", "routed")
        main._log_kill("stall", {"id": "req-5"})
        main._log_lifeos_escalation({"skill_model": "m", "decision": "escalated"})

        async def inner():
            yield b"z"

        await _drive(main._traced_stream(inner(), "m", "req-5"))

    asyncio.run(scenario())

    def ids(stem):
        p = retention.log_path(isolate_log_dir, stem)
        return {json.loads(l).get("id")
                for l in p.read_text().splitlines() if l.strip()}

    assert "t-join" in ids("routing")
    assert "t-join" in ids("kills")
    assert "t-join" in ids("lifeos-escalations")
    assert "t-join" in ids("requests")


def test_writers_safe_outside_a_request():
    """Log writers are called from startup paths and tests with no context.

    `tracing.trace_id()` must return None rather than raising, or a missing
    context would take down a request.
    """
    tracing.TRACE_CTX.set(None)
    assert tracing.trace_id() is None
    tracing.note(anything="ignored")        # must not raise


def test_new_trace_id_is_unique():
    """id(request) was reused across requests; token_hex is not."""
    ids = {tracing.new_trace_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_transport_failure_is_not_reported_as_ok(isolate_log_dir):
    """A tier that answered with nothing must not count as a success.

    _supervised_stream CATCHES transport failures now, so the generator
    completes normally and the `except BaseException` branch never runs. The
    six real `error:ReadTimeout` rows from 2026-09-08 would have become `ok`
    without a marker branch -- the same blind spot that once let a run of hard
    400s show up here as 100% success.
    """
    async def scenario():
        tracing.start("t-transport")

        async def inner():
            tracing.note(upstream_error_type="ReadTimeout", upstream_silent=True)
            yield b"terminal-frame"

        await _drive(main._traced_stream(inner(), "m", "req-6"))

    asyncio.run(scenario())
    ev = _events(isolate_log_dir, "complete")
    assert ev[0]["outcome"] == "upstream_transport:ReadTimeout"


def test_a_real_status_still_wins_over_the_transport_marker(isolate_log_dir):
    """Ordering in the outcome chain is deliberate: a status is more specific."""
    async def scenario():
        tracing.start("t-both")

        async def inner():
            tracing.note(upstream_status=429, upstream_error_type="ReadTimeout")
            yield b"x"

        await _drive(main._traced_stream(inner(), "m", "req-7"))

    asyncio.run(scenario())
    assert _events(isolate_log_dir, "complete")[0]["outcome"] == "upstream_error:429"
