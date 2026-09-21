# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""compact_messages tests.

This module had NO tests, which is how the orphaned-tool bug below survived: it
does not raise, and the local Ollama answers a malformed body with a 200, so
nothing anywhere said it was wrong.

Offline -- `summarize` is an injected callable, so no model is involved.
Run: .venv/bin/python -m pytest lifeos/tests/test_compact.py -q
"""

import asyncio
import json

from lifeos.compact import DEFAULT_TAIL_MESSAGES, _tail_start, compact_messages


async def _summary(_prompt: str) -> str:
    return "earlier: the agent audited the repo"


def _compact(body, **kw):
    return asyncio.run(compact_messages(body, _summary, **kw))


def _tool_loop(steps: int = 6, end_on_tool: bool = False) -> dict:
    """A realistic agent loop: assistant tool_call -> tool result -> assistant."""
    msgs = [{"role": "system", "content": "you are a coding agent"},
            {"role": "user", "content": "audit the repo"}]
    for i in range(steps):
        msgs.append({"role": "assistant", "tool_calls": [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": "bash",
                          "arguments": json.dumps({"command": f"cmd{i}"})}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "content": f"out{i}"})
        msgs.append({"role": "assistant", "content": f"step {i} done"})
    if end_on_tool:
        msgs.pop()          # Pi re-POSTs after a tool runs, before any new turn
    return {"model": "m", "messages": msgs}


def _orphaned_tool_ids(messages: list[dict]) -> list[str]:
    """tool messages whose answering assistant tool_calls message is missing."""
    seen: set[str] = set()
    orphans = []
    for m in messages:
        if m.get("role") == "assistant":
            for call in m.get("tool_calls") or []:
                seen.add(call["id"])
        elif m.get("role") == "tool" and m.get("tool_call_id") not in seen:
            orphans.append(m.get("tool_call_id"))
    return orphans


class TestToolPairingInvariant:
    """The bug this file exists for.

    Pi re-POSTs after a tool runs with no new user message, so the last message
    is routinely a `tool` result. Keeping only the last message produced
    `[system, summary, tool]` -- a result with no record of the call it answers.
    """

    def test_a_body_ending_on_a_tool_result_does_not_orphan_it(self):
        out = _compact(_tool_loop(end_on_tool=True))
        assert _orphaned_tool_ids(out["messages"]) == []
        assert out["messages"][-1]["role"] == "tool"      # tail still ends there

    def test_no_orphans_at_any_tail_size(self):
        # The boundary can land anywhere in the loop; every landing must be safe.
        for n in range(1, 13):
            for end_on_tool in (False, True):
                out = _compact(_tool_loop(end_on_tool=end_on_tool), tail_messages=n)
                assert _orphaned_tool_ids(out["messages"]) == [], (n, end_on_tool)

    def test_the_tail_may_grow_past_n_to_keep_a_pair_together(self):
        # Correctness beats the budget: asking for 2 can yield 3 when the cut
        # would otherwise separate an assistant call from its result.
        out = _compact(_tool_loop(), tail_messages=2)
        tail = [m for m in out["messages"][2:]]
        assert len(tail) >= 2
        assert _orphaned_tool_ids(out["messages"]) == []

    def test_tail_start_walks_backward_never_forward(self):
        # Forward would drop the tool results -- usually the most relevant thing
        # in the tail, since it is whatever the model just went and fetched.
        convo = [{"role": "assistant", "tool_calls": [{"id": "c"}]},
                 {"role": "tool", "tool_call_id": "c"},
                 {"role": "tool", "tool_call_id": "c"}]
        assert _tail_start(convo, 1) == 0
        assert _tail_start(convo, 2) == 0


class TestTailWidening:
    def test_more_than_one_message_survives(self):
        out = _compact(_tool_loop())
        kept = out["messages"][2:]          # after system + summary
        assert len(kept) > 1

    def test_summary_replaces_the_folded_turns(self):
        out = _compact(_tool_loop(steps=8))
        assert out["messages"][1]["role"] == "system"
        assert "earlier: the agent audited the repo" in out["messages"][1]["content"]
        assert len(out["messages"]) < len(_tool_loop(steps=8)["messages"])

    def test_original_system_messages_are_preserved(self):
        out = _compact(_tool_loop())
        assert out["messages"][0]["content"] == "you are a coding agent"

    def test_order_is_preserved(self):
        src = _tool_loop(steps=8)
        out = _compact(src)
        kept = [m for m in out["messages"] if m.get("role") != "system"]
        positions = [src["messages"].index(m) for m in kept]
        assert positions == sorted(positions)

    def test_the_input_body_is_not_mutated(self):
        src = _tool_loop()
        before = json.dumps(src)
        _compact(src)
        assert json.dumps(src) == before


class TestNothingWorthFolding:
    def test_short_conversation_is_returned_untouched(self):
        body = {"model": "m", "messages": [
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]}
        assert _compact(body) is body

    def test_tail_covering_everything_skips_the_model_call(self):
        # Folding here would spend a summarizer hop to produce a body no
        # smaller than the one handed in.
        body = _tool_loop(steps=2)
        assert _compact(body, tail_messages=100) is body

    def test_empty_summary_leaves_the_body_alone(self):
        async def blank(_p):
            return "   "
        out = asyncio.run(compact_messages(_tool_loop(), blank))
        assert out is not None and len(out["messages"]) == len(_tool_loop()["messages"])

    def test_messages_not_a_list_is_returned_untouched(self):
        body = {"model": "m", "messages": None}
        assert _compact(body) is body


class TestDefault:
    def test_default_tail_is_sized_for_a_tool_loop(self):
        # A loop step costs 2-3 messages, so the default should cover a couple
        # of steps -- enough to keep the work in progress, not so much that the
        # summary has no budget left.
        assert 4 <= DEFAULT_TAIL_MESSAGES <= 12
