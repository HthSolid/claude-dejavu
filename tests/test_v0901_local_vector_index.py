"""v0.9.1 unit tests — in-process vector index + per-project reranker.

Two next-gen primitives:

  1. code/vector_index.py — pure-numpy TurnVectorIndex with brute-
     force cosine top-k. Persisted to disk under
     <data_root>/vector_index/. Skipped (with notice) when numpy
     isn't installed in the test environment.
  2. code/reranker.py — per-project `.dejavu-rerank` config file
     overrides used when env vars aren't set. Caller-set env keeps
     priority.

All tests are infra-free. The optional vector-index round-trip uses
synthetic random vectors; no Weaviate / PG / cloud embed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "mcp"))

os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT",
                       "/tmp/dejavu-v0901-test")
os.makedirs("/tmp/dejavu-v0901-test", exist_ok=True)

import vector_index  # type: ignore[import]
import reranker  # type: ignore[import]


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


# ─── TurnVectorIndex contract ──────────────────────────────────────────────

def test_vector_index_empty_query_returns_empty() -> None:
    _step("vector_index: empty index → query returns []")
    if not vector_index._is_available():
        _skip("numpy not available")
        return
    idx = vector_index.TurnVectorIndex(
        Path(tempfile.mkdtemp(prefix="vi-empty-")))
    import numpy as np  # type: ignore[import]
    out = idx.query(np.zeros(1024, dtype="float32"), k=5)
    _aT("returns empty list", out == [], repr(out))


def test_vector_index_add_then_query_returns_nearest() -> None:
    _step("vector_index: add 5 vectors, query → returns highest-cosine "
          "turn first")
    if not vector_index._is_available():
        _skip("numpy not available")
        return
    import numpy as np  # type: ignore[import]
    idx = vector_index.TurnVectorIndex(
        Path(tempfile.mkdtemp(prefix="vi-add-")), dim=4)
    np.random.seed(42)
    vecs = [
        [1.0, 0.0, 0.0, 0.0],     # tid=100 — east
        [0.0, 1.0, 0.0, 0.0],     # tid=200 — north
        [0.7, 0.7, 0.0, 0.0],     # tid=300 — north-east
        [-1.0, 0.0, 0.0, 0.0],    # tid=400 — west
        [0.0, 0.0, 1.0, 0.0],     # tid=500 — up
    ]
    idx.add_vectors([100, 200, 300, 400, 500], vecs)
    q = np.array([0.9, 0.1, 0.0, 0.0], dtype="float32")
    out = idx.query(q, k=3)
    _aT(f"east (100) is the top hit (got {out})",
        out[0][0] == 100, repr(out))
    _aT(f"NE (300) is second (got {out})",
        out[1][0] == 300, repr(out))
    _aT(f"west (400) is NOT in top-3 (got {out})",
        400 not in [t for t, _ in out], repr(out))


def test_vector_index_save_load_roundtrip() -> None:
    _step("vector_index: save then load_or_empty restores vectors+ids")
    if not vector_index._is_available():
        _skip("numpy not available")
        return
    root = Path(tempfile.mkdtemp(prefix="vi-roundtrip-"))
    idx = vector_index.TurnVectorIndex(root, dim=4)
    idx.class_name = "ClaudeDejavuTurn_test"
    idx.source_url = "http://wv-test:8080"
    import numpy as np  # type: ignore[import]
    idx.add_vectors([1, 2, 3], [[1.0, 0, 0, 0], [0, 1, 0, 0],
                                  [0, 0, 1, 0]])
    idx.save()
    idx2 = vector_index.TurnVectorIndex.load_or_empty(root)
    _aT("loaded index is non-empty", idx2.is_loaded)
    _aT("count == 3 after roundtrip",
        idx2.vectors.shape[0] == 3)
    _aT("metadata preserved (class_name)",
        idx2.class_name == "ClaudeDejavuTurn_test")
    _aT("metadata preserved (source_url)",
        idx2.source_url == "http://wv-test:8080")
    out = idx2.query(np.array([0, 0, 1, 0], dtype="float32"), k=1)
    _aT("query post-roundtrip returns the up vector (id=3)",
        out and out[0][0] == 3, repr(out))


def test_vector_index_status_shape() -> None:
    _step("vector_index_status: shape contract for dejavu_status")
    root = Path(tempfile.mkdtemp(prefix="vi-status-"))
    s = vector_index.vector_index_status(root)
    for k in ("available", "enabled", "loaded", "count", "dim",
               "class_name", "source_url", "built_at"):
        _aT(f"key {k!r} present", k in s, repr(list(s.keys())))


def test_vector_index_singleton_caches_per_data_root() -> None:
    _step("vector_index: get_index() returns the same instance for "
          "the same data root")
    root_a = Path(tempfile.mkdtemp(prefix="vi-sing-a-"))
    root_b = Path(tempfile.mkdtemp(prefix="vi-sing-b-"))
    idx_a1 = vector_index.get_index(root_a)
    idx_a2 = vector_index.get_index(root_a)
    idx_b = vector_index.get_index(root_b)
    _aT("get_index(a) == get_index(a) (cached)",
        idx_a1 is idx_a2)
    _aT("get_index(b) is a different instance",
        idx_b is not idx_a1)


# ─── Per-project reranker config ───────────────────────────────────────────

def test_project_config_file_overrides_default() -> None:
    _step("reranker: .dejavu-rerank file in cwd is read as project "
          "config when env is unset")
    tmp = Path(tempfile.mkdtemp(prefix="rerank-cfg-"))
    (tmp / ".dejavu-rerank").write_text(
        "CLAUDE_DEJAVU_RERANKER=cohere\n"
        "CLAUDE_DEJAVU_RERANK_TIMEOUT_S=2.5\n",
        encoding="utf-8",
    )
    orig_cwd = os.getcwd()
    orig_env_p = os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)
    orig_env_t = os.environ.pop("CLAUDE_DEJAVU_RERANK_TIMEOUT_S", None)
    try:
        os.chdir(tmp)
        _aT("provider resolves to 'cohere'",
            reranker._resolve_provider() == "cohere",
            f"got {reranker._resolve_provider()!r}")
        _aT("timeout resolves to 2.5",
            reranker._resolve_timeout() == 2.5,
            f"got {reranker._resolve_timeout()!r}")
    finally:
        os.chdir(orig_cwd)
        if orig_env_p is not None:
            os.environ["CLAUDE_DEJAVU_RERANKER"] = orig_env_p
        if orig_env_t is not None:
            os.environ["CLAUDE_DEJAVU_RERANK_TIMEOUT_S"] = orig_env_t


def test_env_overrides_project_config_file() -> None:
    _step("reranker: env wins when both env AND project file are set")
    tmp = Path(tempfile.mkdtemp(prefix="rerank-env-wins-"))
    (tmp / ".dejavu-rerank").write_text(
        "CLAUDE_DEJAVU_RERANKER=cohere\n", encoding="utf-8")
    orig_cwd = os.getcwd()
    os.environ["CLAUDE_DEJAVU_RERANKER"] = "voyage"
    try:
        os.chdir(tmp)
        _aT("env CLAUDE_DEJAVU_RERANKER=voyage wins over file=cohere",
            reranker._resolve_provider() == "voyage",
            f"got {reranker._resolve_provider()!r}")
    finally:
        os.chdir(orig_cwd)
        os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)


def test_project_config_walks_to_ancestor() -> None:
    _step("reranker: .dejavu-rerank can live in an ancestor of cwd")
    parent = Path(tempfile.mkdtemp(prefix="rerank-ancestor-"))
    (parent / ".dejavu-rerank").write_text(
        "CLAUDE_DEJAVU_RERANKER=bge\n", encoding="utf-8")
    child = parent / "nested" / "deeper"
    child.mkdir(parents=True, exist_ok=True)
    orig_cwd = os.getcwd()
    os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)
    try:
        os.chdir(child)
        _aT("walks up to ancestor and resolves to 'bge'",
            reranker._resolve_provider() == "bge",
            f"got {reranker._resolve_provider()!r}")
    finally:
        os.chdir(orig_cwd)


def test_project_config_absent_falls_back_to_off() -> None:
    _step("reranker: no env + no file → off")
    tmp = Path(tempfile.mkdtemp(prefix="rerank-none-"))
    orig_cwd = os.getcwd()
    os.environ.pop("CLAUDE_DEJAVU_RERANKER", None)
    try:
        os.chdir(tmp)
        _aT("no config anywhere → provider 'off'",
            reranker._resolve_provider() == "off")
    finally:
        os.chdir(orig_cwd)


# ─── CLI handlers exist + don't crash on missing args ─────────────────────

def test_vector_index_cli_handlers_defined() -> None:
    _step("CLI: cmd_vector_index_rebuild / status exist in recall.py")
    sys.path.insert(0, str(ROOT / "code"))
    import recall  # type: ignore[import]
    _aT("cmd_vector_index_rebuild defined",
        callable(getattr(recall, "cmd_vector_index_rebuild", None)))
    _aT("cmd_vector_index_status defined",
        callable(getattr(recall, "cmd_vector_index_status", None)))


def main() -> int:
    print("v0.9.1 local vector index + per-project reranker tests")
    tests = [
        test_vector_index_empty_query_returns_empty,
        test_vector_index_add_then_query_returns_nearest,
        test_vector_index_save_load_roundtrip,
        test_vector_index_status_shape,
        test_vector_index_singleton_caches_per_data_root,
        test_project_config_file_overrides_default,
        test_env_overrides_project_config_file,
        test_project_config_walks_to_ancestor,
        test_project_config_absent_falls_back_to_off,
        test_vector_index_cli_handlers_defined,
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
