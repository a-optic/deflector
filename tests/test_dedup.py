# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
_dedup_result tests: a primary that raises must release followers with that
same failure, not leave them waiting on the shared condition forever.
Run: .venv/bin/python -m pytest tests/test_dedup.py -q
"""

import asyncio

import anyio
import pytest

import main


def test_follower_gets_error_not_hang_when_primary_raises():
    async def scenario():
        key = ("test-model", "same-content-hash")
        started = asyncio.Event()
        release = asyncio.Event()

        async def primary_run():
            started.set()
            await release.wait()
            raise RuntimeError("upstream blew up")

        async def follower_run():
            # Never actually called (only the primary invokes `run`), but
            # `_dedup_result` requires a callable either way.
            raise AssertionError("follower must not re-run the request")

        primary_task = asyncio.create_task(main._dedup_result(key, primary_run))
        await started.wait()
        # Primary is mid-flight and has registered the _InFlight entry;
        # start a follower against the same key before releasing the primary.
        follower_task = asyncio.create_task(main._dedup_result(key, follower_run))
        await asyncio.sleep(0)  # let the follower actually join and start waiting

        release.set()

        # The bug: without releasing/marking done on exception, the follower
        # await would hang forever. Bound it so a regression fails fast
        # instead of hanging pytest.
        with pytest.raises(RuntimeError, match="upstream blew up"):
            await asyncio.wait_for(primary_task, timeout=5)
        with pytest.raises(RuntimeError, match="upstream blew up"):
            await asyncio.wait_for(follower_task, timeout=5)

        # Entry must be released so the next request with this key isn't
        # stuck joining a dead entry either.
        assert key not in main._inflight

    asyncio.run(scenario())


def test_dedup_result_normal_success_unaffected():
    async def scenario():
        key = ("test-model", "another-content-hash")

        async def run():
            return {"ok": True}, 200

        resp, status = await main._dedup_result(key, run)
        assert resp == {"ok": True}
        assert status == 200
        assert key not in main._inflight

    asyncio.run(scenario())


def test_stream_follower_released_when_primary_cancelled_mid_stream():
    """Reproduces the real incident: a client (Pi) disconnects mid-stream,
    which Starlette turns into cancelling the task driving _dedup_stream's
    primary. Without shielding the finally-block cleanup, that cancellation
    can interrupt the cleanup itself, so entry.done never flips and a
    follower sharing the same (model, content) key hangs forever."""
    async def scenario():
        key = ("test-model", "same-content-hash")
        got_first_chunk = asyncio.Event()

        async def inner():
            yield b"chunk1"
            got_first_chunk.set()
            await asyncio.sleep(100)  # never reached -- primary gets cancelled first
            yield b"chunk2"  # pragma: no cover

        async def drain(gen):
            return [c async for c in gen]

        primary_task = asyncio.create_task(drain(main._dedup_stream(key, inner())))
        await got_first_chunk.wait()
        await asyncio.sleep(0)  # let the primary actually suspend on the next chunk

        async def unused_inner():
            raise AssertionError("follower must not iterate its own inner")
            yield  # pragma: no cover

        follower_task = asyncio.create_task(drain(main._dedup_stream(key, unused_inner())))
        await asyncio.sleep(0)  # let the follower actually join and start waiting

        # Simulate the client disconnecting: cancel the primary while it's
        # suspended mid-stream, same as Starlette does on client disconnect.
        primary_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await primary_task

        # The bug: without shielded cleanup this hangs forever. Bound it so a
        # regression fails fast instead of hanging pytest.
        follower_chunks = await asyncio.wait_for(follower_task, timeout=5)
        assert follower_chunks == [b"chunk1"]

        # Entry must be fully released (not just marked done) so a later
        # request reusing this key starts fresh instead of attaching to a
        # dead, already-finished entry and replaying its stale chunks.
        assert key not in main._inflight

    asyncio.run(scenario())


def test_result_follower_released_when_primary_cancelled_mid_run():
    """Same scenario as above, for the non-streaming _dedup_result path."""
    async def scenario():
        key = ("test-model", "same-content-hash-2")
        started = asyncio.Event()

        async def primary_run():
            started.set()
            await asyncio.sleep(100)  # never reached -- primary gets cancelled first
            return {"ok": True}, 200  # pragma: no cover

        primary_task = asyncio.create_task(main._dedup_result(key, primary_run))
        await started.wait()
        await asyncio.sleep(0)

        async def unused_run():
            raise AssertionError("follower must not re-run the request")

        follower_task = asyncio.create_task(main._dedup_result(key, unused_run))
        await asyncio.sleep(0)

        primary_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await primary_task

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(follower_task, timeout=5)

        assert key not in main._inflight

    asyncio.run(scenario())


def test_stream_entry_fully_released_under_anyio_sticky_cancellation():
    """Plain asyncio.Task.cancel() only delivers cancellation once, so it
    can't tell apart 'shield the mark-done step' from 'shield the mark-done
    step AND fold the release into the same shielded unit' -- a single
    cancellation is already consumed by the time the finally block's
    await asyncio.shield(...) runs, so an unshielded release() line right
    after it would still execute fine in that test.

    Starlette's actual disconnect handling cancels via an anyio cancel scope,
    which is NOT one-shot: every checkpoint hit while still inside a
    cancelled scope re-raises the cancellation, for as long as the task
    keeps yielding control within that scope's lifetime. That can interrupt
    the `await asyncio.shield(...)` expression itself (the shielded work
    still finishes independently in the background, but code *after* that
    await in the same scope does not run) -- which is exactly why the
    release has to live inside the shielded coroutine, not after it. This
    test reproduces that with a real anyio.CancelScope instead of a bare
    asyncio cancel, so it actually distinguishes the two designs."""
    async def scenario():
        key = ("test-model", "anyio-sticky-cancel")
        got_first_chunk = anyio.Event()
        chunks: list[bytes] = []

        async def inner():
            yield b"chunk1"
            got_first_chunk.set()
            await anyio.sleep(100)  # never reached -- scope gets cancelled first
            yield b"chunk2"  # pragma: no cover

        async def primary():
            async for c in main._dedup_stream(key, inner()):
                chunks.append(c)

        async with anyio.create_task_group() as tg:
            tg.start_soon(primary)
            await got_first_chunk.wait()
            await anyio.sleep(0)  # let the primary actually suspend on the next chunk
            tg.cancel_scope.cancel()
        # Task group only returns once the cancelled primary has fully
        # unwound (including its finally block), under real sticky
        # cancellation the whole way -- no manual timeout/wait_for needed
        # here, unlike the bare-asyncio tests above.

        assert chunks == [b"chunk1"]

        # The actual assertion: the entry must be gone, not just done=True.
        # If release() were a separate unshielded line, sticky cancellation
        # would have skipped it, and this key would still be sitting in
        # main._inflight forever.
        assert key not in main._inflight

        # And a completely fresh request reusing this key must start clean,
        # not attach as a follower to a stale finished entry.
        async def fresh_inner():
            yield b"fresh"

        with anyio.fail_after(5):
            result = [c async for c in main._dedup_stream(key, fresh_inner())]
        assert result == [b"fresh"]

    anyio.run(scenario)
