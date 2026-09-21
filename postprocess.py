# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Output post-processing for tier C model batiai/minimax-m2.7:q3.

The model leaks chain-of-thought before the final structured review, closing
with a literal `</think>` tag. Downstream skills must strip everything up to
and including that tag before persisting or citing the result. Passthrough
when the tag is absent so wrappers stay universal.
"""

from __future__ import annotations

COT_CLOSE = "</think>"


def strip_cot(text: str) -> str:
    """Drop chain-of-thought preamble that ends with `</think>`.

    Returns everything after the last `</think>` occurrence, whitespace-trimmed.
    If the tag is absent, returns the input unchanged (trimmed).
    """
    idx = text.rfind(COT_CLOSE)
    if idx == -1:
        return text.strip()
    return text[idx + len(COT_CLOSE):].strip()
