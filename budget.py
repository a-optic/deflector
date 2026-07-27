# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Token-preflight budget (Bucket B, spec 03 §3b).

Estimate a prompt's size BEFORE a Claude hop so an over-budget request can be
*downshifted* to a local / ollama-cloud tier that can hold it, rather than rejected.
This is a size gate, not a policy gate: the reactive wall / output-cap abort in
providers.claude_cli.stream_claude stays as the backstop for output-side runaway.

tiktoken's cl100k_base is not Claude's tokenizer; it underestimates Claude by ~12%,
so a safety margin is applied. The number only needs to be good enough to decide
"does this comfortably fit the context window" — exactness is not required.
"""

from __future__ import annotations

import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")
SAFETY = 1.15  # cl100k underestimates Claude ~12%; round up (spec §4)

# Input-side context budget per Claude model. Pair with claude_cli.OUTPUT_CAP for the
# output side. Held well under the 200k hard window to leave room for the reply.
CONTEXT_BUDGET = {
    "claude-haiku-4-5": 180_000,
    "claude-sonnet-5":  180_000,
    "claude-opus-4-8":  180_000,
}
_DEFAULT_BUDGET = 180_000


def estimate_prompt_tokens(text: str) -> int:
    """Margin-adjusted token estimate for `text` against Claude's tokenizer."""
    return int(len(_enc.encode(text)) * SAFETY)


def over_budget(text: str, model_id: str) -> bool:
    """True if `text` is too large for `model_id`'s input context budget."""
    return estimate_prompt_tokens(text) > CONTEXT_BUDGET.get(model_id, _DEFAULT_BUDGET)
