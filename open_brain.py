# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retrieve earlier material from Open Brain, with a relevance score.

WHY THIS EXISTS AT ALL
----------------------
Open Brain's SQL computes a relevance score -- `(embedding <=> query) AS score`,
cosine distance, lower is better -- and its response model then drops the field
on the floor. Without it a caller can rank results but cannot say "the best
match is still bad, inject nothing", which is the only judgement that matters
when the alternative is misleading the model.

WHY WE DON'T JUST PATCH IT
--------------------------
Open Brain is a fork tracked against benclawbot/open-brain, and the fork's
whole value is that it can keep pulling. Measured at the 1.0.2 merge: upstream
was 199 commits and 210 files ahead, and `src/api/main.py` -- the file holding
the response model -- had changed by +148/-97. Adding a field there would be
the first source divergence in a repo that currently has none, in the most
active file. Upstream 1.0.2 did not add the field either (it added
`captured_by`), so waiting is not a strategy.

HOW THE SCORE IS RECOVERED
--------------------------
The API returns results already ORDERED BY distance; only the magnitude is
lost. So we recompute it: embed the query and the returned contents with the
same model Open Brain ingested them with, and take the cosine distance
ourselves.

This is exact, not an approximation. Verified against ground truth by pulling a
row's stored vector straight out of Postgres and re-embedding its content:
cosine(stored, recomputed) = 1.0000000000. (Element-wise magnitudes differ --
one side is normalised -- which cosine distance is by definition indifferent
to.) The number this module returns is the number pgvector would have returned.

CALIBRATION
-----------
Measured against the live corpus, top-1 distance:

    "which books changed how I see the world"      0.367   relevant
    "what is my security posture this week"        0.601   relevant
    "the migratory patterns of arctic terns"       0.800   unrelated
    "how do I bake sourdough bread at high altitude" 0.885  unrelated

Hence a default cut at 0.70. That is four queries against a small, homogeneous
corpus -- indicative, not settled. Re-measure before trusting it on new
material.

FAIL OPEN, ALWAYS
-----------------
Every caller of this module is on a path that only runs because something else
already broke. A retrieval that errors, times out, or returns nothing must be
indistinguishable from "no relevant material", never an exception. There is no
raise in the public surface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Hit:
    """One retrieved memory and how close it actually was."""
    id: str
    source: str
    content: str
    tags: tuple[str, ...]
    distance: float          # cosine distance, 0 = identical, lower is better


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance without numpy, so this module has no import weight.

    numpy is present (via spaCy/Presidio) but pulling it in here would make a
    1024-float dot product depend on it. Straight Python is microseconds at
    this size and keeps the module trivially testable.
    """
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


def _embed(texts: list[str], *, model: str, base_url: str, timeout: float) -> list[list[float]] | None:
    """Batch-embed in ONE call. Returns None on any failure.

    One call rather than per-text: the round trip dominates at this size, and
    batching the query together with every candidate halves the latency.

    Point this at the SAME endpoint Open Brain ingested with -- `main` (11434)
    today. Identical model, identical vectors, so a recomputed distance matches
    what pgvector stored.

    An earlier version of this comment claimed `main` had
    OLLAMA_MAX_LOADED_MODELS=1 and would evict the 36B chat model on every
    embed. That came from ~/.agentstop/*.plist, which turns out to be stale:
    the running daemons on both 11434 and 11435 report MAX_LOADED_MODELS=2 and
    KEEP_ALIVE=24h, and `main` was observed holding the embedder and the 36B
    simultaneously. There is no swap to avoid.

    Do NOT point this at the deflector's own port. It proxies /api/embed
    happily, but a request that is already inside the deflector calling back
    into it is a loop with nothing to gain -- the privacy gate short-circuits
    local destinations anyway.
    """
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/api/embed",
                       json={"model": model, "input": texts}, timeout=timeout)
        r.raise_for_status()
        vectors = r.json().get("embeddings")
    except Exception:
        return None
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        return None
    return vectors


def search(query: str, *, base_url: str, embed_model: str, embed_url: str,
           limit: int = 5, max_distance: float = 0.70,
           timeout_s: float = 5.0, exclude_tags: tuple[str, ...] = ()) -> list[Hit]:
    """Relevant earlier material, closest first, never anything weaker than
    `max_distance`. Returns [] on any failure -- see the module docstring.
    """
    if not query.strip():
        return []

    try:
        r = httpx.post(f"{base_url.rstrip('/')}/memories/search",
                       json={"query": query, "limit": limit}, timeout=timeout_s)
        r.raise_for_status()
        rows = r.json()
    except Exception:
        return []
    if not isinstance(rows, list) or not rows:
        return []

    kept = [row for row in rows
            if isinstance(row, dict) and isinstance(row.get("content"), str)
            and not (set(row.get("tags") or ()) & set(exclude_tags))]
    if not kept:
        return []

    vectors = _embed([query] + [row["content"] for row in kept],
                     model=embed_model, base_url=embed_url, timeout=timeout_s)
    if vectors is None:
        # Ranking without a magnitude cannot answer "is the best one good
        # enough", and injecting an irrelevant memory is worse than injecting
        # none, so the whole result is dropped rather than guessed at.
        return []

    q, rest = vectors[0], vectors[1:]
    hits = [
        Hit(id=str(row.get("id", "")), source=str(row.get("source", "")),
            content=row["content"], tags=tuple(row.get("tags") or ()),
            distance=_cosine_distance(q, vec))
        for row, vec in zip(kept, rest)
    ]
    # Sort locally rather than trusting the server's order: the API sorts by
    # distance today, but this module's contract is its own.
    return sorted([h for h in hits if h.distance <= max_distance],
                  key=lambda h: h.distance)
