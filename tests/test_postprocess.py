# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""strip_cot tests. Run: .venv/bin/python -m pytest tests/test_postprocess.py -q"""

from postprocess import COT_CLOSE, strip_cot


def test_strips_preamble_before_close_tag():
    text = "reasoning about the answer here" + COT_CLOSE + "final answer"
    assert strip_cot(text) == "final answer"


def test_uses_last_close_tag_when_multiple():
    text = f"first{COT_CLOSE}middle{COT_CLOSE}last"
    assert strip_cot(text) == "last"


def test_passthrough_when_tag_absent():
    assert strip_cot("no cot here, just an answer") == "no cot here, just an answer"


def test_whitespace_trimmed_on_both_paths():
    assert strip_cot(f"junk{COT_CLOSE}   padded answer   ") == "padded answer"
    assert strip_cot("   padded answer   ") == "padded answer"


def test_empty_input():
    assert strip_cot("") == ""
