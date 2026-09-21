# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Streaming rehydration (v4.0 §6.1).

A placeholder such as <PII_a7f3_PERSON_1> can split across two SSE deltas, so
rehydration cannot be a naive per-chunk replace. It buffers the tail that might be an
unclosed placeholder and flushes on stream end. Only the redact path (trusted cloud)
ever carries a mapping; restricted destinations never received the data, local never
redacted it. An unmatched placeholder is left as-is (never crashes) in case the model
reformats it.

`rehydrate_complete` is the non-streaming counterpart, for a value that arrived whole
and so can never split: it needs no buffer, and using RehydrateStream on one would
withhold a tail that has no later chunk to rejoin.
"""

from __future__ import annotations

import re

PLACEHOLDER_RE = re.compile(r"<PII_[A-Za-z0-9]+_[A-Z_]+_\d+>", re.IGNORECASE)


class RehydrateStream:
    def __init__(self, mapping: dict):
        self.map = mapping
        self.buf = ""

    def feed(self, delta: str) -> str:
        self.buf += delta
        self.buf = PLACEHOLDER_RE.sub(lambda m: self.map.get(m.group(), m.group()), self.buf)
        cut = self.buf.rfind("<")  # possible start of a split placeholder
        if cut == -1:
            out, self.buf = self.buf, ""
        else:
            out, self.buf = self.buf[:cut], self.buf[cut:]
        return out

    def flush(self) -> str:
        out = PLACEHOLDER_RE.sub(lambda m: self.map.get(m.group(), m.group()), self.buf)
        self.buf = ""
        return out


def rehydrate_complete(text: str, mapping: dict) -> str:
    """Restore placeholders in a string that arrived whole (not streamed).

    Same substitution RehydrateStream performs, minus the split-placeholder
    buffering — safe only when the caller knows the value is complete.
    """
    return PLACEHOLDER_RE.sub(lambda m: mapping.get(m.group(), m.group()), text)
