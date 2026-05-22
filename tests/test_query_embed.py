"""Unit tests for code/query_embed.py (v0.8.7 Context Economy phase 4).

The query_embed module wraps the dejavu-cloud /v1/embed endpoint with
a per-process cache + bounded timeout. Used by recall._hybrid + the
JIT recall hook to do caller-side query embedding for nearVector
hybrid search.

Coverage:
  - exact_cache_hit: same query twice → second call returns the
    cached vector without touching urllib.
  - timeout_returns_none: slow cloud → returns None on TimeoutError.
  - network_error_returns_none: urlopen raises → returns None.
  - bad_response_shape_returns_none: malformed JSON / missing key /
    non-list vector → returns None.
  - no_cloud_key_returns_none: DEJAVU_CLOUD_KEY unset → returns None
    immediately (no network call attempted).
  - cache_eviction: more than _CACHE_CAP unique queries → oldest
    entries evicted.
  - empty_query_returns_none: defensive.
  - lookup_by_vector exposes the vector cache via sem_cache_lookup.

Coverage tier:
  - infra-free. We monkeypatch urllib.request.urlopen to a fake
    response. Real network is never touched.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import query_embed as qe  # noqa: E402  type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, d: str = "") -> None:
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    if d:
        print(f"      {d}")


def _aT(name: str, cond: bool, d: str = "") -> None:
    _ok(name) if cond else _fail(name, d)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


# ─── helpers ─────────────────────────────────────────────────────────────────

class _FakeResp:
    """Stand-in for urllib.urlopen's context-manager response."""
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status
        self.headers = {}

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _vec(dim: int = 1024, fill: float = 0.1) -> list[float]:
    return [fill] * dim


def _make_fake_urlopen(*, payload: dict | None = None,
                       raise_exc: Exception | None = None,
                       call_counter: dict | None = None):
    """Factory: returns a urlopen replacement that either returns
    a fake response with `payload` or raises `raise_exc`. Counts
    calls in `call_counter['n']` so tests can assert
    cache-hit behaviour without network."""
    def _fake(req, timeout=None):  # noqa: ARG001
        if call_counter is not None:
            call_counter["n"] = call_counter.get("n", 0) + 1
        if raise_exc is not None:
            raise raise_exc
        body = json.dumps(payload or {}).encode("utf-8")
        return _FakeResp(body)
    return _fake


# ─── tests ───────────────────────────────────────────────────────────────────

def test_exact_cache_hit() -> None:
    _step("embed_query: same query twice → second call is a cache hit")
    qe._cache_reset()
    counter = {"n": 0}
    fake = _make_fake_urlopen(
        payload={"embeddings": [_vec(dim=8, fill=0.5)]},
        call_counter=counter,
    )
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
        v1 = qe.embed_query("test query one")
        v2 = qe.embed_query("test query one")
    _aT("first call returns 8-dim vector",
        isinstance(v1, list) and len(v1) == 8, f"got: {v1!r}")
    _aT("second call returns identical vector",
        v1 == v2, f"v1={v1[:3]!r} v2={v2[:3]!r}")
    _aT("only one network call (second was cached)",
        counter["n"] == 1, f"got counter={counter['n']}")


def test_different_query_is_separate_cache_entry() -> None:
    _step("embed_query: distinct queries get separate cache entries")
    qe._cache_reset()
    counter = {"n": 0}
    fake = _make_fake_urlopen(
        payload={"embeddings": [_vec(dim=4)]},
        call_counter=counter,
    )
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
        qe.embed_query("alpha")
        qe.embed_query("beta")
        qe.embed_query("alpha")  # should hit cache
    _aT("only 2 network calls (alpha + beta, no realpha)",
        counter["n"] == 2, f"got counter={counter['n']}")


def test_timeout_returns_none() -> None:
    _step("embed_query: urlopen TimeoutError → returns None")
    qe._cache_reset()
    import socket
    fake = _make_fake_urlopen(raise_exc=socket.timeout("simulated timeout"))
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
        v = qe.embed_query("query that will time out", timeout_s=0.001)
    _aT("returns None on timeout", v is None, f"got: {v!r}")


def test_network_error_returns_none() -> None:
    _step("embed_query: urlopen URLError → returns None")
    qe._cache_reset()
    fake = _make_fake_urlopen(
        raise_exc=urllib.error.URLError("simulated unreachable"))
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
        v = qe.embed_query("query during outage")
    _aT("returns None on URLError", v is None, f"got: {v!r}")


def test_http_500_returns_none() -> None:
    _step("embed_query: HTTP 500 → returns None")
    qe._cache_reset()
    err = urllib.error.HTTPError(
        url="x", code=500, msg="server error", hdrs={}, fp=None)
    fake = _make_fake_urlopen(raise_exc=err)
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
        v = qe.embed_query("query during cloud outage")
    _aT("returns None on HTTP 500", v is None, f"got: {v!r}")


def test_http_401_returns_none() -> None:
    _step("embed_query: HTTP 401 (bad auth) → returns None")
    qe._cache_reset()
    err = urllib.error.HTTPError(
        url="x", code=401, msg="unauthorized", hdrs={}, fp=None)
    fake = _make_fake_urlopen(raise_exc=err)
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_bad_key"):
        v = qe.embed_query("query with bad key")
    _aT("returns None on HTTP 401", v is None, f"got: {v!r}")


def test_bad_response_shape_returns_none() -> None:
    _step("embed_query: malformed cloud response shapes → None")
    qe._cache_reset()
    cases = [
        {},  # no embeddings key
        {"embeddings": None},
        {"embeddings": []},
        {"embeddings": [None]},
        {"embeddings": [{}]},  # non-list vector
        {"embeddings": [["not-a-float"]]},  # non-numeric
    ]
    for case in cases:
        qe._cache_reset()
        fake = _make_fake_urlopen(payload=case)
        with patch.object(qe.urllib.request, "urlopen", fake), \
             patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
            v = qe.embed_query("test query")
        _aT(f"malformed case {case!r} → None",
            v is None, f"got: {v!r}")


def test_no_cloud_key_returns_none_immediately() -> None:
    _step("embed_query: DEJAVU_CLOUD_KEY unset → None, no network call")
    qe._cache_reset()
    counter = {"n": 0}
    fake = _make_fake_urlopen(
        payload={"embeddings": [_vec()]},
        call_counter=counter,
    )
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: ""):
        v = qe.embed_query("query without key")
    _aT("returns None when key missing", v is None, f"got: {v!r}")
    _aT("no network call attempted",
        counter["n"] == 0, f"got: {counter['n']}")


def test_empty_query_returns_none() -> None:
    _step("embed_query: empty query → None, no network call")
    qe._cache_reset()
    counter = {"n": 0}
    fake = _make_fake_urlopen(
        payload={"embeddings": [_vec()]},
        call_counter=counter,
    )
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
        _aT("empty string → None", qe.embed_query("") is None)
    _aT("no network call",
        counter["n"] == 0, f"got: {counter['n']}")


def test_cache_eviction_when_over_cap() -> None:
    _step(f"embed_query: cache evicts when more than {qe._CACHE_CAP} unique queries")
    qe._cache_reset()
    cap = qe._CACHE_CAP
    counter = {"n": 0}
    # Each unique query gets a unique vector so we can tell hit vs miss
    def _fake(req, timeout=None):
        counter["n"] = counter.get("n", 0) + 1
        # extract query from request body
        body = req.data if hasattr(req, "data") else b"{}"
        d = json.loads(body)
        q = d["texts"][0]
        # vector encodes query length so each is distinct
        return _FakeResp(json.dumps({
            "embeddings": [[float(len(q))] * 4]
        }).encode("utf-8"))

    with patch.object(qe.urllib.request, "urlopen", _fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
        # Fill cache to capacity
        for i in range(cap):
            qe.embed_query(f"q{i:04d}")
        _aT(f"made {cap} calls so far", counter["n"] == cap)
        # One more query should evict oldest
        qe.embed_query("brand-new-query")
        _aT("one extra call for the new query",
            counter["n"] == cap + 1)
        # Now the original q0000 was evicted → re-fetching it should
        # hit the network again
        qe.embed_query("q0000")
        _aT("q0000 was evicted and required a refetch",
            counter["n"] == cap + 2, f"counter={counter['n']}")


def test_lookup_by_vector_finds_cached_match() -> None:
    _step("lookup_by_vector: cosine match against cached vectors")
    qe._cache_reset()
    fake = _make_fake_urlopen(payload={"embeddings": [[1.0, 0.0, 0.0, 0.0]]})
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
        v = qe.embed_query("cached query")
    _aT("cached vector populated", v == [1.0, 0.0, 0.0, 0.0])
    # Identical vector → "hit" kind
    result = qe.lookup_by_vector([1.0, 0.0, 0.0, 0.0])
    _aT("identical vector → match returned",
        result is not None, f"got: {result!r}")
    if result is not None:
        _aT("kind is 'hit' for identical vector",
            result.get("kind") == "hit", f"got: {result!r}")
        _aT("similarity is 1.0",
            abs(result.get("similarity", 0) - 1.0) < 1e-6,
            f"got: {result!r}")


def test_lookup_by_vector_returns_none_on_empty_cache() -> None:
    _step("lookup_by_vector: empty cache → None")
    qe._cache_reset()
    result = qe.lookup_by_vector([1.0, 0.0, 0.0])
    _aT("empty cache returns None", result is None, f"got: {result!r}")


def test_cache_reset_clears_both_tiers() -> None:
    _step("_cache_reset: clears string + vector caches")
    qe._cache_reset()
    fake = _make_fake_urlopen(payload={"embeddings": [[0.7, 0.3]]})
    with patch.object(qe.urllib.request, "urlopen", fake), \
         patch.object(qe, "_cloud_key", lambda: "dvk_test_fake"):
        qe.embed_query("populate")
    _aT("string cache has 1 entry",
        len(qe._STRING_CACHE) == 1)
    _aT("vector cache has 1 entry",
        len(qe._VECTOR_CACHE) == 1)
    qe._cache_reset()
    _aT("string cache empty after reset",
        len(qe._STRING_CACHE) == 0)
    _aT("vector cache empty after reset",
        len(qe._VECTOR_CACHE) == 0)


def main() -> int:
    # v0.8.8 test isolation: embed_query now consults a PG-backed
    # cross-session cache before the cloud call. These v0.8.7 tests
    # only mock urlopen — they'd otherwise hit (and contaminate) the
    # live PG embed_cache table. Set the kill-switch so the PG tier
    # is silently skipped for the duration of this suite. The
    # dedicated test_embed_cache_pg.py suite exercises the PG tier
    # with a proper psycopg2 fake module.
    os.environ["CLAUDE_DEJAVU_DISABLE_PG_CACHE"] = "1"
    tests = [
        test_exact_cache_hit,
        test_different_query_is_separate_cache_entry,
        test_timeout_returns_none,
        test_network_error_returns_none,
        test_http_500_returns_none,
        test_http_401_returns_none,
        test_bad_response_shape_returns_none,
        test_no_cloud_key_returns_none_immediately,
        test_empty_query_returns_none,
        test_cache_eviction_when_over_cap,
        test_lookup_by_vector_finds_cached_match,
        test_lookup_by_vector_returns_none_on_empty_cache,
        test_cache_reset_clears_both_tiers,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
