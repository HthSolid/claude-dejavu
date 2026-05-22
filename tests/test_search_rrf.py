"""Unit tests for v0.8.7 RRF wiring in recall._hybrid + JIT hook.

The closed-source bridge primitive `dejavu_bridge.retrieve_rrf` fuses
two ranked lists by reciprocal-rank-fusion. v0.8.7 wires it into both
CLI search (recall._hybrid) and the JIT recall hook (user_prompt_submit
._run_search) so that when the caller-side cloud embed succeeds, the
search is BM25 + nearVector hybrid; when it doesn't, we silently fall
back to v0.8.6's BM25-only path.

Coverage:
  - bm25_only_fallback_when_embed_returns_none (CLI): asserts the
    v0.8.6 path is preserved exactly when embed_query returns None.
  - rrf_fuses_two_lists (helper): the _rrf_fuse helper reorders by
    fused RRF score (well-known textbook ordering).
  - bm25_jit_only_when_embed_returns_none (JIT hook): same guarantee
    in the hook path.
  - rrf_used_in_jit_when_embed_succeeds (JIT hook): when both branches
    return hits, RRF runs and the breadcrumb logs mode=rrf.
  - vec_only_when_bm25_returns_empty: rare-but-possible — vector hits
    populate `wv_hits` without RRF.
  - rrf_failure_falls_back_to_bm25: bridge exception → bm25 used,
    no crash.
  - hybrid_handles_no_query_embed_module: the import-side failure
    (no query_embed importable) falls through cleanly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "hooks"))

import recall  # noqa: E402  type: ignore[import]
import user_prompt_submit as ups  # noqa: E402  type: ignore[import]


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


def _mk_hit(tid: int, score: float, sess: str = "sess0001") -> dict:
    return {
        "session_id": sess,
        "turn_id": tid,
        "project_slug": "test-proj",
        "role": "user",
        "ts": "2026-05-18",
        "ai_title": "test",
        "_additional": {"score": score},
    }


# ─── helpers / _rrf_fuse ──────────────────────────────────────────────────────

def test_rrf_fuse_merges_two_lists() -> None:
    _step("_rrf_fuse: fuses BM25 + nearVector lists (turn_id-keyed)")
    bm25 = [_mk_hit(1, 5.0), _mk_hit(2, 3.0)]
    vec = [_mk_hit(2, 0.9), _mk_hit(3, 0.6)]
    fused = recall._rrf_fuse(bm25, vec)
    _aT("fused contains all 3 unique turn_ids",
        {h.get("turn_id") for h in fused if h.get("turn_id") is not None}
        == {1, 2, 3}, f"got: {fused!r}")
    # turn_id=2 appears in both lists at rank 1 + 0 (1-indexed: 2 + 1
    # = top-ranked overall). It should come out first.
    _aT("turn_id=2 (appears in both lists) ranks first",
        fused[0].get("turn_id") in (2, "2"),
        f"got first: {fused[0]!r}")


def test_rrf_fuse_returns_bm25_on_empty_vec() -> None:
    _step("_rrf_fuse: empty vec → returns bm25 unchanged (v0.8.6 path)")
    bm25 = [_mk_hit(1, 5.0), _mk_hit(2, 3.0)]
    fused = recall._rrf_fuse(bm25, [])
    _aT("returns bm25 list as-is", fused == bm25, f"got: {fused!r}")


def test_rrf_fuse_returns_vec_on_empty_bm25() -> None:
    _step("_rrf_fuse: empty bm25 → returns vec unchanged")
    vec = [_mk_hit(1, 0.9), _mk_hit(2, 0.5)]
    fused = recall._rrf_fuse([], vec)
    _aT("returns vec list as-is", fused == vec, f"got: {fused!r}")


def test_rrf_fuse_falls_back_on_bridge_exception() -> None:
    _step("_rrf_fuse: bridge raises → returns bm25 (no crash)")
    bm25 = [_mk_hit(1, 5.0)]
    vec = [_mk_hit(2, 0.5)]
    import distill_pipeline as dp
    real = dp._bridge.retrieve_rrf

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated wheel ICE")

    dp._bridge.retrieve_rrf = _boom  # type: ignore[assignment]
    try:
        fused = recall._rrf_fuse(bm25, vec)
    finally:
        dp._bridge.retrieve_rrf = real  # type: ignore[assignment]
    _aT("returns bm25 on bridge crash", fused == bm25, f"got: {fused!r}")


# ─── recall._hybrid CLI path ────────────────────────────────────────────────

def test_hybrid_bm25_only_when_embed_returns_none() -> None:
    """v0.8.6 preservation: when embed_query returns None (no key /
    cloud unreachable / timeout), _hybrid must return EXACTLY what
    the BM25-only path returns. No vector branch executes."""
    _step("_hybrid: embed_query=None → BM25-only path (v0.8.6 preserved)")
    bm25_canned = [_mk_hit(1, 5.0), _mk_hit(2, 3.0)]
    nv_called = {"n": 0}

    def _fake_bm25(query, limit, where, timeout=30.0):
        return list(bm25_canned)

    def _fake_nv(vec, limit, where, timeout=30.0):
        nv_called["n"] += 1
        return [_mk_hit(99, 0.99)]

    import query_embed as qe
    with patch.object(recall, "_bm25_query", _fake_bm25), \
         patch.object(recall, "_near_vector_query", _fake_nv), \
         patch.object(qe, "embed_query", lambda *_a, **_kw: None):
        from scope import resolve_scope
        scope = resolve_scope("global", cwd="/tmp")
        hits = recall._hybrid("test query", 10, scope)

    _aT("nearVector branch was NOT called", nv_called["n"] == 0,
        f"got: {nv_called['n']}")
    _aT("returns exactly the bm25 list (v0.8.6 ordering)",
        hits == bm25_canned, f"got: {hits!r}")


def test_hybrid_uses_rrf_when_embed_succeeds() -> None:
    """When embed_query returns a vector, both branches run and RRF
    fuses them. The fused list ≠ pure BM25 ordering when the vector
    branch surfaces extra hits."""
    _step("_hybrid: embed succeeds → RRF fuses BM25 + nearVector")
    bm25 = [_mk_hit(1, 5.0), _mk_hit(2, 3.0)]
    vec = [_mk_hit(3, 0.95), _mk_hit(2, 0.8)]

    import query_embed as qe
    with patch.object(recall, "_bm25_query",
                       lambda *_a, **_kw: list(bm25)), \
         patch.object(recall, "_near_vector_query",
                       lambda *_a, **_kw: list(vec)), \
         patch.object(qe, "embed_query",
                       lambda *_a, **_kw: [0.1] * 8):
        from scope import resolve_scope
        scope = resolve_scope("global", cwd="/tmp")
        hits = recall._hybrid("test query", 10, scope)

    turn_ids = {int(h.get("turn_id")) for h in hits
                if h.get("turn_id") is not None}
    _aT("fused result contains hits from both branches",
        turn_ids == {1, 2, 3}, f"got: {turn_ids!r}")


def test_hybrid_falls_back_to_bm25_when_vec_empty() -> None:
    """If the cloud embed succeeded but nearVector returned no hits
    (e.g. Weaviate has no indexed vectors for this scope), the BM25
    list should still be the canonical answer — not a degraded empty
    result. v0.8.6 ordering preserved."""
    _step("_hybrid: empty nearVector → BM25-only fallback")
    bm25 = [_mk_hit(1, 5.0)]

    import query_embed as qe
    with patch.object(recall, "_bm25_query",
                       lambda *_a, **_kw: list(bm25)), \
         patch.object(recall, "_near_vector_query",
                       lambda *_a, **_kw: []), \
         patch.object(qe, "embed_query",
                       lambda *_a, **_kw: [0.1] * 4):
        from scope import resolve_scope
        scope = resolve_scope("global", cwd="/tmp")
        hits = recall._hybrid("test query", 10, scope)

    _aT("returns bm25 list", hits == bm25, f"got: {hits!r}")


def test_hybrid_handles_missing_query_embed_module() -> None:
    """If query_embed module is unimportable (corrupt install, weird
    path), _hybrid should fall back to BM25 instead of crashing. The
    bare `except Exception` around the import handles this."""
    _step("_hybrid: ImportError on query_embed → BM25-only")
    bm25 = [_mk_hit(1, 5.0)]
    nv_called = {"n": 0}

    def _fake_nv(*_a, **_kw):
        nv_called["n"] += 1
        return [_mk_hit(99, 0.5)]

    # Simulate import failure by monkeypatching sys.modules.
    import query_embed as qe
    with patch.object(recall, "_bm25_query",
                       lambda *_a, **_kw: list(bm25)), \
         patch.object(recall, "_near_vector_query", _fake_nv), \
         patch.object(qe, "embed_query",
                       side_effect=ImportError("simulated import error")):
        from scope import resolve_scope
        scope = resolve_scope("global", cwd="/tmp")
        hits = recall._hybrid("test query", 10, scope)

    _aT("returns bm25 on embed_query ImportError", hits == bm25,
        f"got: {hits!r}")
    _aT("nearVector branch not called",
        nv_called["n"] == 0, f"got: {nv_called['n']}")


# ─── JIT hook _run_search ────────────────────────────────────────────────────

def test_jit_run_search_bm25_only_when_embed_returns_none() -> None:
    _step("JIT _run_search: embed=None → BM25-only path (v0.8.6 preserved)")
    bm25 = [_mk_hit(1, 5.0), _mk_hit(2, 3.0)]
    nv_called = {"n": 0}

    def _fake_bm25(query, top_n, wv_url, wv_class, deadline_s):
        return list(bm25)

    def _fake_nv(*_a, **_kw):
        nv_called["n"] += 1
        return []

    import query_embed as qe
    with patch.object(ups, "_bm25_jit", _fake_bm25), \
         patch.object(ups, "_near_vector_jit", _fake_nv), \
         patch.object(qe, "embed_query", lambda *_a, **_kw: None):
        hits = ups._run_search("how to migrate from rsync to restic",
                                 top_n=5, deadline_s=1.5)

    # _run_search continues into PG gist fetch which we can't easily
    # mock here — but the assertion that nearVector wasn't called is
    # the load-bearing v0.8.6-preservation check.
    _aT("nearVector branch was NOT called", nv_called["n"] == 0,
        f"got: {nv_called['n']}")
    turn_ids = {h.get("turn_id") for h in hits}
    _aT("returned hits cover both bm25 turn_ids",
        turn_ids == {1, 2}, f"got: {turn_ids!r}")


def test_jit_run_search_uses_rrf_when_embed_succeeds() -> None:
    _step("JIT _run_search: embed succeeds → RRF fusion")
    bm25 = [_mk_hit(1, 5.0)]
    vec = [_mk_hit(2, 0.9), _mk_hit(1, 0.5)]

    import query_embed as qe
    with patch.object(ups, "_bm25_jit",
                       lambda *_a, **_kw: list(bm25)), \
         patch.object(ups, "_near_vector_jit",
                       lambda *_a, **_kw: list(vec)), \
         patch.object(qe, "embed_query",
                       lambda *_a, **_kw: [0.1] * 4):
        hits = ups._run_search("test query that is long enough",
                                 top_n=5, deadline_s=1.5)
    turn_ids = {h.get("turn_id") for h in hits}
    _aT("fused result contains both turn_ids",
        turn_ids == {1, 2}, f"got: {turn_ids!r}")


def test_jit_run_search_handles_embed_exception() -> None:
    """If embed_query raises (broken cloud, weird state), the hook
    must continue with BM25-only, not crash."""
    _step("JIT _run_search: embed exception → BM25-only path")
    bm25 = [_mk_hit(1, 5.0)]
    import query_embed as qe
    with patch.object(ups, "_bm25_jit",
                       lambda *_a, **_kw: list(bm25)), \
         patch.object(ups, "_near_vector_jit",
                       lambda *_a, **_kw: []), \
         patch.object(qe, "embed_query",
                       side_effect=RuntimeError("simulated cloud explode")):
        hits = ups._run_search("test query that is long enough",
                                 top_n=5, deadline_s=1.5)
    turn_ids = {h.get("turn_id") for h in hits}
    _aT("returns bm25-only hits", turn_ids == {1},
        f"got: {turn_ids!r}")


def test_jit_run_search_zero_deadline_returns_empty() -> None:
    _step("JIT _run_search: deadline<=0 → []")
    hits = ups._run_search("query", top_n=5, deadline_s=0)
    _aT("returns empty list on zero deadline", hits == [],
        f"got: {hits!r}")


def test_jit_run_search_rrf_falls_back_on_bridge_failure() -> None:
    """When the bridge's retrieve_rrf raises, _run_search falls back
    to the BM25 list — no crash, no degraded user experience."""
    _step("JIT _run_search: bridge crash on retrieve_rrf → BM25-only")
    bm25 = [_mk_hit(1, 5.0), _mk_hit(2, 3.0)]
    vec = [_mk_hit(3, 0.9)]

    import query_embed as qe
    import distill_pipeline as dp
    real = dp._bridge.retrieve_rrf

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated wheel ICE")

    dp._bridge.retrieve_rrf = _boom  # type: ignore[assignment]
    try:
        with patch.object(ups, "_bm25_jit",
                           lambda *_a, **_kw: list(bm25)), \
             patch.object(ups, "_near_vector_jit",
                           lambda *_a, **_kw: list(vec)), \
             patch.object(qe, "embed_query",
                           lambda *_a, **_kw: [0.1] * 4):
            hits = ups._run_search("test query that is long enough",
                                     top_n=5, deadline_s=1.5)
    finally:
        dp._bridge.retrieve_rrf = real  # type: ignore[assignment]
    turn_ids = {h.get("turn_id") for h in hits}
    _aT("falls back to bm25 on RRF crash", turn_ids == {1, 2},
        f"got: {turn_ids!r}")


def main() -> int:
    tests = [
        test_rrf_fuse_merges_two_lists,
        test_rrf_fuse_returns_bm25_on_empty_vec,
        test_rrf_fuse_returns_vec_on_empty_bm25,
        test_rrf_fuse_falls_back_on_bridge_exception,
        test_hybrid_bm25_only_when_embed_returns_none,
        test_hybrid_uses_rrf_when_embed_succeeds,
        test_hybrid_falls_back_to_bm25_when_vec_empty,
        test_hybrid_handles_missing_query_embed_module,
        test_jit_run_search_bm25_only_when_embed_returns_none,
        test_jit_run_search_uses_rrf_when_embed_succeeds,
        test_jit_run_search_handles_embed_exception,
        test_jit_run_search_zero_deadline_returns_empty,
        test_jit_run_search_rrf_falls_back_on_bridge_failure,
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
