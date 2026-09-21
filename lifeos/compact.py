# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Local-fallback context compaction (v4.0 §3 addendum).

When a Tier B secret block, a tier refusal, or a silent cloud tier forces a
`local_cloud_reasoning` request off the cloud escalation it needed for context
room, the local model's window may still be smaller than the prompt. Compact by
folding the older turns into one summary turn and keeping a verbatim tail. The
summary itself is produced by a caller-supplied async callable so this module
stays pure string/dict shuffling and unit-testable without a network call.

WHY A TAIL RATHER THAN ONE MESSAGE
----------------------------------
This kept exactly one message -- the last -- which had two problems.

The obvious one is fidelity: an agentic session is hundreds of turns, and
collapsing all but one of them into a paragraph throws away the working context
of whatever the model is in the middle of doing.

The subtle one is correctness, and it was live. Pi is a tool loop: after a tool
runs it re-POSTs with the result appended and NO new user message, so the last
message is routinely a `tool` result. Keeping only that produced

    [system, summary, tool]

-- a bare tool result with no record of the call it answers. The local Ollama
accepts that with a 200 rather than rejecting it, so it never surfaced as an
error; the model simply loses the thread ("you've provided a history where...").
Any scheme that keeps SOME messages owes this invariant, which is the main
reason the tail is contiguous: snapping one boundary is a single backward walk,
where scattered selections would need general grouping across the whole fold.
"""

from __future__ import annotations

from typing import Awaitable, Callable

Summarizer = Callable[[str], Awaitable[str]]

# How many trailing turns survive verbatim. Small on purpose: every message kept
# is budget not spent on the summary, and the point is to preserve the turn in
# progress, not to avoid summarizing. A tool loop spends 2-3 messages per step
# (assistant call, tool result, assistant reply), so this is roughly the last
# two or three steps.
DEFAULT_TAIL_MESSAGES = 8


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            seg.get("text", "") for seg in content
            if isinstance(seg, dict) and isinstance(seg.get("text"), str)
        )
    return ""


def build_summary_prompt(messages: list[dict]) -> str:
    turns = "\n\n".join(
        f"{m.get('role')}: {_flatten_content(m.get('content'))}" for m in messages
    )
    return (
        "Summarize the conversation below densely: preserve facts, decisions, "
        "names, numbers, and open questions the next reply will need. Output "
        "only the summary, no commentary.\n\n" + turns
    )


def _tail_start(convo: list[dict], tail_messages: int) -> int:
    """Index where the verbatim tail begins, never orphaning a `tool` message.

    A `tool` message answers a specific `tool_call_id` on the assistant message
    before it. If the tail were cut between them, the kept side would carry a
    result with nothing to attach it to. Walking the boundary BACKWARD past any
    run of `tool` messages lands it on the assistant that issued the calls, so
    the pair travels together.

    Backward rather than forward on purpose: moving forward would drop the tool
    results instead, which loses whatever the model just went and fetched --
    usually the most relevant thing in the tail.
    """
    cut = max(0, len(convo) - tail_messages)
    while cut > 0 and convo[cut].get("role") == "tool":
        cut -= 1
    return cut


async def compact_messages(body: dict, summarize: Summarizer,
                           tail_messages: int = DEFAULT_TAIL_MESSAGES) -> dict:
    """Fold older turns into one summary turn, keeping a verbatim tail.

    Returns a new body; `summarize` is awaited once with the prompt for the
    turns being folded. Returns `body` unchanged -- so the caller can decide
    what to do next -- when there is nothing worth folding (fewer than 3
    non-system turns, or a tail that already covers everything) or when the
    summarizer comes back empty.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return body

    system_msgs = [m for m in messages if m.get("role") == "system"]
    convo = [m for m in messages if m.get("role") != "system"]
    if len(convo) < 3:
        return body

    cut = _tail_start(convo, max(1, tail_messages))
    older, tail = convo[:cut], convo[cut:]
    if not older:
        # The tail already covers the whole conversation, so there is nothing
        # to summarize. Folding here would spend a model call to produce a body
        # no smaller than the one we were given.
        return body

    summary = (await summarize(build_summary_prompt(older))).strip()
    if not summary:
        return body

    out = dict(body)
    out["messages"] = system_msgs + [
        {"role": "system", "content": f"[earlier conversation, compacted]\n{summary}"},
        *tail,
    ]
    return out
