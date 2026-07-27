# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Bucket B tests (spec 03): token-preflight budget + Claude concurrency semaphore.
Offline — no `claude` spawn, no network. Run: .venv/bin/python -m pytest test_budget.py -q
"""

import asyncio

import budget
import main
from budget import estimate_prompt_tokens, over_budget


def test_estimate_monotonic():
    small = estimate_prompt_tokens("hello world")
    big = estimate_prompt_tokens("hello world " * 500)
    assert big > small


def test_estimate_applies_margin():
    text = "the quick brown fox jumps over the lazy dog"
    raw = len(budget._enc.encode(text))
    # SAFETY margin means the estimate is never below the raw cl100k count.
    assert estimate_prompt_tokens(text) >= raw
    assert budget.SAFETY > 1.0


def test_over_budget_false_for_small():
    assert not over_budget("a short prompt", "claude-haiku-4-5")


def test_over_budget_true_for_huge():
    huge = "word " * 200_000  # ~200k tokens, above the 180k budget even before margin
    assert over_budget(huge, "claude-sonnet-5")


def test_over_budget_unknown_model_uses_default():
    assert not over_budget("still short", "some-unknown-model")


def test_semaphore_caps_concurrent_claude():
    """cap+2 concurrent Claude streams must never run more than `max_concurrent_claude`
    processes at once; overflow queues on the semaphore instead of erroring."""
    cap = main._MAX_CLAUDE
    tracker = {"cur": 0, "max": 0, "completed": 0}

    async def fake_stream(body, model_id, mapping=None, max_wall_seconds=900.0):
        tracker["cur"] += 1
        tracker["max"] = max(tracker["max"], tracker["cur"])
        try:
            for _ in range(3):
                await asyncio.sleep(0)  # yield so peers can interleave
                yield b'{"done":false}\n'
        finally:
            tracker["cur"] -= 1
            tracker["completed"] += 1

    async def drive():
        orig = main.stream_claude
        main.stream_claude = fake_stream  # _claude_guarded resolves this at call time
        try:
            async def consume():
                async for _ in main._claude_guarded({}, "claude-haiku-4-5", 900.0):
                    pass
            await asyncio.gather(*(consume() for _ in range(cap + 2)))
        finally:
            main.stream_claude = orig

    asyncio.run(drive())
    assert tracker["max"] <= cap          # invariant: never exceeds the cap
    assert tracker["max"] == cap          # and the cap is actually saturated
    assert tracker["completed"] == cap + 2  # all overflow eventually served, none dropped
