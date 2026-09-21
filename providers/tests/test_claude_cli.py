# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Claude CLI provider tests — offline (no `claude` spawn).
Run: .venv/bin/python -m pytest providers/tests/test_claude_cli.py -q
"""

import json

from privacy.rehydrate import RehydrateStream
from private_ref import OPERATOR_NAMES
from providers.claude_cli import DEFAULT_SYSTEM, build_prompt, build_system, transform_line

NAME = OPERATOR_NAMES[0]

# Trimmed real events from `claude -p ... --output-format stream-json` (haiku).
INIT = '{"type":"system","subtype":"init","model":"claude-haiku-4-5"}'
THINK = ('{"type":"assistant","message":{"content":'
         '[{"type":"thinking","thinking":"reason about it"}]}}')
TEXT = ('{"type":"assistant","message":{"content":'
        '[{"type":"text","text":"hi there"}]}}')
RATE = '{"type":"rate_limit_event"}'
RESULT = ('{"type":"result","subtype":"success","result":"hi there",'
          '"usage":{"input_tokens":10,"output_tokens":41}}')


def _new_state():
    return {"model": "claude-haiku-4-5", "seen_text": set(), "out_chars": 0, "done": False}


def _run(lines):
    state, reh, frames = _new_state(), RehydrateStream({}), []
    for ln in lines:
        for f in transform_line(ln, state, reh):
            frames.append(json.loads(f))
    return state, frames


def test_build_prompt_flattens():
    body = {"system": "be brief",
            "messages": [{"role": "user", "content": "hello"},
                         {"role": "assistant", "content": "hi"},
                         {"role": "user", "content": "bye"}]}
    p = build_prompt(body)
    # system is NOT folded into the text prompt (goes to --system-prompt instead)
    assert "[System]" not in p and "be brief" not in p
    assert p.count("[User]") == 2 and "[Assistant]" in p
    assert p.strip().endswith("bye")


def test_build_system_collects():
    body = {"system": "be brief",
            "messages": [{"role": "system", "content": "also be kind"},
                         {"role": "user", "content": "hi"}]}
    s = build_system(body)
    assert "be brief" in s and "also be kind" in s


def test_build_system_default():
    assert build_system({"messages": [{"role": "user", "content": "hi"}]}) == DEFAULT_SYSTEM


def test_skip_noise_events():
    _, frames = _run([INIT, RATE])
    assert frames == []


def test_text_and_thinking_emit():
    _, frames = _run([INIT, THINK, TEXT, RESULT])
    contents = [f["message"]["content"] for f in frames if not f.get("done")]
    assert "hi there" in contents
    thinking = [f["message"].get("thinking") for f in frames
                if f.get("message") and f["message"].get("thinking")]
    assert "reason about it" in thinking


def test_terminal_frame_carries_usage():
    _, frames = _run([INIT, TEXT, RESULT])
    done = frames[-1]
    assert done["done"] is True
    assert done["eval_count"] == 41 and done["prompt_eval_count"] == 10


def test_no_duplicate_text():
    # same text block arriving twice must emit only once
    _, frames = _run([INIT, TEXT, TEXT, RESULT])
    streamed = [f for f in frames if not f.get("done") and f["message"].get("content")]
    assert len([f for f in streamed if f["message"]["content"] == "hi there"]) == 1


def test_result_fallback_when_no_stream():
    # tools-only turn: no assistant text streamed, result carries the answer
    _, frames = _run([INIT, RESULT])
    streamed = [f["message"]["content"] for f in frames if not f.get("done")]
    assert "hi there" in streamed


def test_rehydration_on_claude_path():
    state, frames = _new_state(), None
    reh = RehydrateStream({"<PII_a1_PERSON_1>": NAME})
    ev = ('{"type":"assistant","message":{"content":'
          '[{"type":"text","text":"call <PII_a1_PERSON_1> now"}]}}')
    out = []
    for f in transform_line(ev, state, reh):
        out.append(json.loads(f))
    assert out[0]["message"]["content"].startswith(f"call {NAME}")
