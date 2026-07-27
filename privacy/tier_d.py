# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Tier D — local LLM rewrite (v4.0 §3.5). Optional, default OFF.

For fuzzy contextual identity that A-C miss ("my boss at the Reno plant who got
fired last week"). A local Phi/Gemma-class model rewrites the redacted text to strip
residual PII. Off by default: adds 2-10s latency, non-deterministic, lossy
(~0.70-0.85 semantic similarity). Has NO reverse mapping — anything Tier D removes is
gone from the response too. Enable per-request only when the operator accepts that.

Stub: wired but not implemented against a model yet. Returns text unchanged so the
pipeline is safe to ship with tier_d_enabled=False (the only supported state today).
"""

from __future__ import annotations


def tier_d_llm_rewrite(text: str) -> str:
    # TODO(stage-2+): call local Phi/Gemma via the tasks Ollama endpoint with a
    # de-identification prompt. Until then this is a no-op and MUST stay gated behind
    # tier_d_enabled so no request silently relies on it.
    return text
