# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""open_brain.search tests: scoring, thresholding, and failing open.

Two kinds of test live here. The offline ones stub both HTTP calls and are the
real regression guard. The contract ones at the bottom talk to the running
Open Brain and skip when it is not up -- they exist because this module depends
on a THIRD-PARTY response shape that upstream is actively changing (199 commits
between the pinned build and 1.0.2, `src/api/main.py` +148/-97). A silent shape
change would otherwise degrade retrieval in production with nothing failing.

Run: .venv/bin/python -m pytest tests/test_open_brain.py -q
"""

import httpx
import pytest

import open_brain
from open_brain import Hit, search

API = "http://api.invalid"
EMB = "http://embed.invalid"


def _fake_transport(monkeypatch, *, rows, vectors, search_exc=None, embed_exc=None):
    """Stub both POSTs the module makes, keyed by URL."""
    calls = {"search": 0, "embed": 0}

    def fake_post(url, json=None, timeout=None):
        if url.endswith("/memories/search"):
            calls["search"] += 1
            if search_exc:
                raise search_exc
            return httpx.Response(200, json=rows, request=httpx.Request("POST", url))
        if url.endswith("/api/embed"):
            calls["embed"] += 1
            if embed_exc:
                raise embed_exc
            n = len(json["input"])
            return httpx.Response(200, json={"embeddings": vectors[:n]},
                                  request=httpx.Request("POST", url))
        raise AssertionError(f"unexpected POST to {url}")

    monkeypatch.setattr(open_brain.httpx, "post", fake_post)
    return calls


def _go(**kw):
    return search("what did we decide about retries", base_url=API,
                  embed_model="m", embed_url=EMB, **kw)


# Orthogonal unit vectors make the distances exact and obvious:
# identical -> 0.0, orthogonal -> 1.0
V_QUERY = [1.0, 0.0, 0.0]
V_NEAR = [0.9, 0.1, 0.0]
V_FAR = [0.0, 1.0, 0.0]

ROWS = [
    {"id": "1", "source": "s", "content": "close match", "tags": ["a"]},
    {"id": "2", "source": "s", "content": "unrelated", "tags": ["b"]},
]


class TestScoring:
    def test_distance_is_computed_not_taken_from_the_api(self, monkeypatch):
        # The API never returns a score -- that is the whole reason this module
        # exists -- so a distance appearing here can only have been computed.
        _fake_transport(monkeypatch, rows=ROWS, vectors=[V_QUERY, V_NEAR, V_FAR])
        hits = _go(max_distance=1.0)
        assert all(isinstance(h, Hit) for h in hits)
        assert hits[0].distance < 0.02          # near-identical direction
        assert hits[1].distance == pytest.approx(1.0)   # orthogonal

    def test_results_are_sorted_closest_first(self, monkeypatch):
        # Reverse the server's order; the module must still sort.
        _fake_transport(monkeypatch, rows=list(reversed(ROWS)),
                        vectors=[V_QUERY, V_FAR, V_NEAR])
        hits = _go(max_distance=1.0)
        assert [h.content for h in hits] == ["close match", "unrelated"]

    def test_query_and_candidates_are_embedded_in_one_call(self, monkeypatch):
        # Latency here is round trips, not compute.
        calls = _fake_transport(monkeypatch, rows=ROWS, vectors=[V_QUERY, V_NEAR, V_FAR])
        _go(max_distance=1.0)
        assert calls["embed"] == 1


class TestThreshold:
    def test_weak_matches_are_dropped(self, monkeypatch):
        _fake_transport(monkeypatch, rows=ROWS, vectors=[V_QUERY, V_NEAR, V_FAR])
        hits = _go(max_distance=0.70)
        assert [h.content for h in hits] == ["close match"]

    def test_everything_can_be_dropped(self, monkeypatch):
        # Injecting an irrelevant memory is worse than injecting none.
        _fake_transport(monkeypatch, rows=ROWS, vectors=[V_QUERY, V_FAR, V_FAR])
        assert _go(max_distance=0.30) == []

    def test_exclude_tags_filters_before_embedding(self, monkeypatch):
        calls = _fake_transport(monkeypatch, rows=ROWS, vectors=[V_QUERY, V_NEAR])
        hits = _go(max_distance=1.0, exclude_tags=("b",))
        assert [h.content for h in hits] == ["close match"]
        assert calls["embed"] == 1


class TestFailsOpen:
    """Every caller is on a path that only runs because something already
    broke. Retrieval must never be the thing that raises."""

    def test_search_error_returns_empty(self, monkeypatch):
        _fake_transport(monkeypatch, rows=ROWS, vectors=[],
                        search_exc=httpx.ConnectError("refused"))
        assert _go() == []

    def test_embed_error_returns_empty(self, monkeypatch):
        # Deliberately NOT "fall back to the server's ordering": without a
        # magnitude there is no way to know the best hit is good enough.
        _fake_transport(monkeypatch, rows=ROWS, vectors=[],
                        embed_exc=httpx.ReadTimeout("slow"))
        assert _go() == []

    def test_embedding_count_mismatch_returns_empty(self, monkeypatch):
        _fake_transport(monkeypatch, rows=ROWS, vectors=[V_QUERY])   # too few
        assert _go() == []

    def test_empty_corpus_returns_empty(self, monkeypatch):
        _fake_transport(monkeypatch, rows=[], vectors=[])
        assert _go() == []

    def test_blank_query_does_not_call_out_at_all(self, monkeypatch):
        calls = _fake_transport(monkeypatch, rows=ROWS, vectors=[V_QUERY, V_NEAR, V_FAR])
        assert search("   ", base_url=API, embed_model="m", embed_url=EMB) == []
        assert calls == {"search": 0, "embed": 0}

    def test_malformed_rows_are_skipped(self, monkeypatch):
        _fake_transport(monkeypatch,
                        rows=[{"id": "1"}, {"content": 42}, ROWS[0]],
                        vectors=[V_QUERY, V_NEAR])
        assert [h.content for h in _go(max_distance=1.0)] == ["close match"]

    def test_zero_vector_does_not_divide_by_zero(self, monkeypatch):
        # An all-zero embedding is what open_brain's own OllamaEmbedder writes
        # when it fails, so it is a real shape to survive rather than a
        # hypothetical. It must score as "maximally distant", not raise.
        _fake_transport(monkeypatch, rows=ROWS[:1], vectors=[V_QUERY, [0.0, 0.0, 0.0]])
        assert _go(max_distance=1.0)[0].distance == pytest.approx(1.0)
        assert _go(max_distance=0.70) == []      # and is dropped at the default


# --- contract tests against the running service --------------------------------

LIVE_API = "http://127.0.0.1:8000"
LIVE_EMB = "http://127.0.0.1:11434"   # same tier Open Brain ingested with


def _live() -> bool:
    try:
        return httpx.get(f"{LIVE_API}/health", timeout=2).status_code == 200
    except Exception:
        return False


live_only = pytest.mark.skipif(not _live(), reason="Open Brain not running")


@live_only
def test_contract_search_returns_the_fields_this_module_reads():
    """Upstream is actively rewriting src/api/main.py. If the shape changes,
    fail here rather than silently retrieving nothing in production."""
    r = httpx.post(f"{LIVE_API}/memories/search",
                   json={"query": "books", "limit": 3}, timeout=30)
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list) and rows, "empty corpus makes this vacuous"
    for key in ("id", "source", "content", "tags"):
        assert key in rows[0], f"upstream dropped {key!r}"


@live_only
def test_contract_score_is_still_absent():
    """The premise of this whole module. If upstream ever adds `score`, this
    fails -- and the client-side recomputation can be simplified away."""
    rows = httpx.post(f"{LIVE_API}/memories/search",
                      json={"query": "books", "limit": 1}, timeout=30).json()
    assert "score" not in rows[0], "upstream now returns score -- simplify open_brain.py"


@live_only
def test_live_relevance_separates_related_from_unrelated():
    related = search("which books changed how I see the world", base_url=LIVE_API,
                     embed_model="snowflake-arctic-embed2", embed_url=LIVE_EMB,
                     limit=3, max_distance=1.0, timeout_s=60)
    unrelated = search("the migratory patterns of arctic terns", base_url=LIVE_API,
                       embed_model="snowflake-arctic-embed2", embed_url=LIVE_EMB,
                       limit=3, max_distance=1.0, timeout_s=60)
    assert related and unrelated
    # The gap the 0.70 default sits in. Measured 0.367 vs 0.800.
    assert related[0].distance < 0.70 < unrelated[0].distance
