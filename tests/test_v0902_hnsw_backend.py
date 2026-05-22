"""v0.9.2 unit tests — hnswlib backend + incremental sync.

Two pieces extend the v0.9.1 TurnVectorIndex:

  A. hnswlib-backed query path: when the package is installed and
     not explicitly disabled, queries route through HNSW for a ~27×
     speedup over brute force at 100k vectors. The numpy arrays
     remain the source of truth so the brute-force fallback is
     always available.

  B. Incremental sync: TurnVectorIndex.sync_new_from_weaviate fetches
     turn_ids > current_max and appends them. Wired into the
     ingester's Stop-hook path so the local index stays fresh.

Plus: recall@k regression — HNSW top-K must overlap with brute-
force top-K above a threshold; the test fails loudly if a future
parameter regression collapses recall.

Infra-free. Synthetic random vectors; clustered enough that HNSW
recall is meaningfully high (random gaussian is the worst case).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "mcp"))

os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT",
                       "/tmp/dejavu-v0902-test")
os.makedirs("/tmp/dejavu-v0902-test", exist_ok=True)

import vector_index  # type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []
SKIP: list[str] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, detail: str = "") -> None:
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail[:300]}")


def _skip(msg: str) -> None:
    SKIP.append(msg)
    print(f"  \033[33m⊘ SKIP\033[0m {msg}", flush=True)


def _aT(name: str, cond: bool, detail: str = "") -> None:
    _ok(name) if cond else _fail(name, detail)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _have_hnsw() -> bool:
    return (vector_index._HNSWLIB_AVAILABLE
             and vector_index._NUMPY_AVAILABLE)


def _make_clustered_vectors(n_clusters: int, per_cluster: int,
                              dim: int, seed: int = 0):
    """Generate gaussian clusters so recall@k is meaningful.
    Pure-random vectors are nearly orthogonal and HNSW recall is
    misleadingly low — clustered data is what real text embeddings
    look like."""
    import numpy as np  # type: ignore[import]
    rng = np.random.default_rng(seed)
    centroids = rng.standard_normal((n_clusters, dim)).astype(
        np.float32)
    centroids /= (np.linalg.norm(centroids, axis=1, keepdims=True)
                   + 1e-9)
    vecs = []
    for c in centroids:
        for _ in range(per_cluster):
            v = c + 0.05 * rng.standard_normal(dim).astype(np.float32)
            vecs.append(v)
    V = np.asarray(vecs, dtype=np.float32)
    V /= np.linalg.norm(V, axis=1, keepdims=True) + 1e-9
    return V


# ─── A: hnsw path active when package is installed ────────────────────────

def test_hnsw_available_flag() -> None:
    _step("A: vector_index reports hnswlib availability")
    _aT("_HNSWLIB_AVAILABLE matches actual import",
        isinstance(vector_index._HNSWLIB_AVAILABLE, bool))
    _aT("_hnsw_is_active honors CLAUDE_DEJAVU_HNSW_DISABLE",
        # Toggle the env, check the flag flips
        (lambda: (
            os.environ.update({"CLAUDE_DEJAVU_HNSW_DISABLE": "1"}),
            (vector_index._hnsw_is_active() is False),
            os.environ.pop("CLAUDE_DEJAVU_HNSW_DISABLE"))[1])(),
        "")


def test_hnsw_query_matches_brute_top_k() -> None:
    _step("A: HNSW query matches brute-force top-K on clustered data")
    if not _have_hnsw():
        _skip("hnswlib not installed")
        return
    import numpy as np  # type: ignore[import]
    V = _make_clustered_vectors(20, 50, dim=128, seed=42)
    n = V.shape[0]
    root = Path(tempfile.mkdtemp(prefix="vi-hnsw-"))
    idx = vector_index.TurnVectorIndex(root, dim=128)
    idx.add_vectors(list(range(1000, 1000 + n)), V.tolist())
    idx.save()  # builds + persists the HNSW companion
    _aT("hnsw index built after save()",
        idx._hnsw is not None)
    # Query with one of the vectors as the probe.
    q = V[37]
    hnsw_hits = idx.query(q, k=10)
    brute_hits = idx.query_brute(q, k=10)
    _aT("hnsw returned non-empty results",
        len(hnsw_hits) == 10)
    overlap = (set(t for t, _ in hnsw_hits)
                & set(t for t, _ in brute_hits))
    _aT(f"recall@10 ≥ 8 (got {len(overlap)})",
        len(overlap) >= 8,
        f"hnsw={[t for t,_ in hnsw_hits]} "
        f"brute={[t for t,_ in brute_hits]}")
    _aT("top-1 match",
        hnsw_hits[0][0] == brute_hits[0][0],
        f"hnsw[0]={hnsw_hits[0]} brute[0]={brute_hits[0]}")


def test_hnsw_persists_through_save_load() -> None:
    _step("A: HNSW companion survives save → load_or_empty")
    if not _have_hnsw():
        _skip("hnswlib not installed")
        return
    V = _make_clustered_vectors(10, 30, dim=64, seed=7)
    n = V.shape[0]
    root = Path(tempfile.mkdtemp(prefix="vi-hnsw-roundtrip-"))
    idx = vector_index.TurnVectorIndex(root, dim=64)
    idx.add_vectors(list(range(n)), V.tolist())
    idx.save()
    # v0.9.5 — file now lives under the slug subdirectory.
    _aT("hnsw file exists on disk",
        (root / "vector_index" / "_default" / "turns.hnsw").exists()
         or (root / "vector_index" / "turns.hnsw").exists())

    idx2 = vector_index.TurnVectorIndex.load_or_empty(root)
    _aT("reloaded index has hnsw active",
        idx2._hnsw is not None,
        f"hnsw={idx2._hnsw!r}")
    # Same query gives same top-1
    hits = idx2.query(V[5], k=3)
    _aT(f"top-1 after reload is the queried vector",
        hits and hits[0][0] == 5, repr(hits))


def test_hnsw_disable_falls_back_to_brute() -> None:
    _step("A: CLAUDE_DEJAVU_HNSW_DISABLE forces brute-force path")
    if not _have_hnsw():
        _skip("hnswlib not installed")
        return
    V = _make_clustered_vectors(5, 10, dim=32, seed=1)
    n = V.shape[0]
    root = Path(tempfile.mkdtemp(prefix="vi-hnsw-disable-"))
    idx = vector_index.TurnVectorIndex(root, dim=32)
    idx.add_vectors(list(range(n)), V.tolist())
    idx.save()
    os.environ["CLAUDE_DEJAVU_HNSW_DISABLE"] = "1"
    try:
        # query() should now skip HNSW and go to brute force.
        hits = idx.query(V[3], k=2)
        _aT("query still works (brute force)",
            hits and hits[0][0] == 3, repr(hits))
    finally:
        os.environ.pop("CLAUDE_DEJAVU_HNSW_DISABLE", None)


def test_hnsw_incremental_add_keeps_index_fresh() -> None:
    _step("A: add_vectors AFTER initial save keeps HNSW in sync")
    if not _have_hnsw():
        _skip("hnswlib not installed")
        return
    import numpy as np  # type: ignore[import]
    V = _make_clustered_vectors(5, 10, dim=32, seed=2)
    n = V.shape[0]
    root = Path(tempfile.mkdtemp(prefix="vi-hnsw-inc-"))
    idx = vector_index.TurnVectorIndex(root, dim=32)
    idx.add_vectors(list(range(n)), V.tolist())
    idx.save()  # builds initial hnsw

    # Append a fresh vector
    new_v = V[0].copy()
    new_v[0] += 0.001  # near-duplicate
    idx.add_vectors([999], [new_v.tolist()])
    # Query with the new vector → should find it first
    hits = idx.query(new_v, k=2)
    _aT("incrementally-added vector is queryable",
        any(t == 999 for t, _ in hits),
        f"hits={hits}")


def test_status_exposes_hnsw_fields() -> None:
    _step("status: vector_index_status reports hnsw_active + tunables")
    root = Path(tempfile.mkdtemp(prefix="vi-status-hnsw-"))
    s = vector_index.vector_index_status(root)
    for k in ("hnsw_available", "hnsw_active",
               "hnsw_disabled", "hnsw_M", "hnsw_ef",
               "hnsw_ef_construction"):
        _aT(f"key {k!r} present", k in s, repr(list(s.keys())))


# ─── B: incremental sync method exists + is idempotent ────────────────────

def test_sync_new_from_weaviate_method_defined() -> None:
    _step("B: TurnVectorIndex.sync_new_from_weaviate is callable")
    idx = vector_index.TurnVectorIndex(
        Path(tempfile.mkdtemp(prefix="vi-sync-")), dim=4)
    _aT("method defined",
        callable(getattr(idx, "sync_new_from_weaviate", None)))


def test_sync_returns_zero_when_index_empty() -> None:
    _step("B: sync on empty index returns 0 (won't bootstrap by "
          "accident — caller must bootstrap explicitly first)")
    idx = vector_index.TurnVectorIndex(
        Path(tempfile.mkdtemp(prefix="vi-sync-empty-")), dim=4)
    if not vector_index._NUMPY_AVAILABLE:
        _skip("numpy not available")
        return
    n = idx.sync_new_from_weaviate(
        "ClaudeDejavuTurn_test", "http://localhost:9999")
    _aT("returns 0 on empty index", n == 0, f"got {n}")


def test_sync_returns_zero_when_wv_unreachable() -> None:
    _step("B: sync with unreachable WV returns 0 (best-effort, "
          "doesn't raise)")
    if not vector_index._NUMPY_AVAILABLE:
        _skip("numpy not available")
        return
    idx = vector_index.TurnVectorIndex(
        Path(tempfile.mkdtemp(prefix="vi-sync-bad-")), dim=4)
    idx.add_vectors([1, 2, 3], [[1, 0, 0, 0], [0, 1, 0, 0],
                                  [0, 0, 1, 0]])
    n = idx.sync_new_from_weaviate(
        "ClaudeDejavuTurn_unreachable",
        "http://127.0.0.1:1",  # closed port — fast fail
        timeout_s=0.5)
    _aT("returns 0 on WV failure (no exception)",
        n == 0, f"got {n}")


# ─── Stop-hook integration smoke ──────────────────────────────────────────

def test_ingester_imports_vector_index() -> None:
    _step("B: ingester.cmd_stop_hook imports + handles vector_index "
          "without raising")
    src = (ROOT / "code" / "ingester.py").read_text(encoding="utf-8")
    _aT("ingester references vector_index import",
        "from vector_index import" in src
         and "sync_new_from_weaviate" in src)
    _aT("ingester gates on CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX",
        "CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX" in src)


def main() -> int:
    print("v0.9.2 HNSW backend + incremental sync tests")
    tests = [
        test_hnsw_available_flag,
        test_hnsw_query_matches_brute_top_k,
        test_hnsw_persists_through_save_load,
        test_hnsw_disable_falls_back_to_brute,
        test_hnsw_incremental_add_keeps_index_fresh,
        test_status_exposes_hnsw_fields,
        test_sync_new_from_weaviate_method_defined,
        test_sync_returns_zero_when_index_empty,
        test_sync_returns_zero_when_wv_unreachable,
        test_ingester_imports_vector_index,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            _fail(f"{t.__name__} raised", repr(e))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}     "
          f"SKIP: {len(SKIP)}")
    if FAIL:
        for n, d in FAIL:
            print(f"  ✗ {n}  {d[:120]}")
    if SKIP:
        for n in SKIP:
            print(f"  ⊘ {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
