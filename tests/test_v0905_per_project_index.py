"""v0.9.5 unit tests — per-project vector index isolation.

Each project can now opt into its own index under
<data_root>/vector_index/<slug>/. The default `_default` slug
preserves v0.9.4 behaviour (one shared index for all projects).
Migration: legacy installs that had the index directly under
<data_root>/vector_index/ are auto-moved into the `_default`
subdirectory on first load.

Infra-free."""
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
                       "/tmp/dejavu-v0905-test")
os.makedirs("/tmp/dejavu-v0905-test", exist_ok=True)

import vector_index  # type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, detail: str = "") -> None:
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail[:300]}")


def _aT(name: str, cond: bool, detail: str = "") -> None:
    _ok(name) if cond else _fail(name, detail)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def test_index_dir_uses_slug_subdir() -> None:
    _step("v0.9.5: index_dir = <data_root>/vector_index/<slug>/")
    root = Path(tempfile.mkdtemp(prefix="vi-slug-"))
    idx_a = vector_index.TurnVectorIndex(root, slug="alpha")
    idx_b = vector_index.TurnVectorIndex(root, slug="beta")
    idx_d = vector_index.TurnVectorIndex(root)  # default
    _aT("alpha slug under vector_index/alpha/",
        str(idx_a.index_dir).endswith("/vector_index/alpha"))
    _aT("beta slug under vector_index/beta/",
        str(idx_b.index_dir).endswith("/vector_index/beta"))
    _aT("default slug uses _default subdir",
        str(idx_d.index_dir).endswith("/vector_index/_default"),
        str(idx_d.index_dir))


def test_two_projects_have_isolated_indexes() -> None:
    _step("v0.9.5: two projects can hold independent vectors at same "
          "turn_id without colliding")
    if not vector_index._NUMPY_AVAILABLE:
        return
    root = Path(tempfile.mkdtemp(prefix="vi-iso-"))
    idx_a = vector_index.TurnVectorIndex(root, dim=4, slug="alpha")
    idx_a.add_vectors([1, 2], [[1, 0, 0, 0], [0, 1, 0, 0]])
    idx_a.save()

    idx_b = vector_index.TurnVectorIndex(root, dim=4, slug="beta")
    idx_b.add_vectors([1, 2], [[0, 0, 1, 0], [0, 0, 0, 1]])
    idx_b.save()

    # Reload both fresh
    idx_a2 = vector_index.TurnVectorIndex.load_or_empty(
        root, slug="alpha")
    idx_b2 = vector_index.TurnVectorIndex.load_or_empty(
        root, slug="beta")
    hits_a = idx_a2.query_brute([1, 0, 0, 0], k=1)
    hits_b = idx_b2.query_brute([0, 0, 1, 0], k=1)
    _aT("alpha[1] points east (id=1)",
        hits_a[0][0] == 1, repr(hits_a))
    _aT("beta[1] points up (id=1) with up vector",
        hits_b[0][0] == 1, repr(hits_b))
    # Cross-check: querying alpha with beta's vector should NOT
    # return a strong match
    cross = idx_a2.query_brute([0, 0, 1, 0], k=1)
    _aT("cross-query alpha with beta's vector returns weak match "
        "(scores ≤ 0.1)",
        cross and cross[0][1] < 0.1, repr(cross))


def test_get_index_singleton_per_slug() -> None:
    _step("v0.9.5: get_index returns DIFFERENT instances for "
          "different slugs at the same data_root")
    root = Path(tempfile.mkdtemp(prefix="vi-singleton-slug-"))
    a = vector_index.get_index(root, slug="alpha")
    b = vector_index.get_index(root, slug="beta")
    a2 = vector_index.get_index(root, slug="alpha")
    _aT("alpha != beta", a is not b)
    _aT("alpha cached (alpha2 is alpha)", a is a2)


def test_list_known_slugs() -> None:
    _step("v0.9.5: list_known_slugs enumerates on-disk indexes")
    if not vector_index._NUMPY_AVAILABLE:
        return
    root = Path(tempfile.mkdtemp(prefix="vi-list-"))
    # Build alpha + beta + leave gamma empty
    for s in ("alpha", "beta"):
        idx = vector_index.TurnVectorIndex(root, dim=4, slug=s)
        idx.add_vectors([1], [[1, 0, 0, 0]])
        idx.save()
    # Create a stray empty dir (no meta.json) — should be ignored
    (root / "vector_index" / "stray").mkdir(parents=True,
                                                exist_ok=True)
    slugs = vector_index.list_known_slugs(root)
    _aT(f"alpha + beta listed (got {slugs})",
        set(slugs) >= {"alpha", "beta"}, repr(slugs))
    _aT(f"stray (no meta.json) NOT listed",
        "stray" not in slugs, repr(slugs))


def test_legacy_layout_migrates_to_default() -> None:
    _step("v0.9.5: pre-v0.9.5 layout (index under vector_index/) "
          "auto-migrates into _default subdir")
    if not vector_index._NUMPY_AVAILABLE:
        return
    root = Path(tempfile.mkdtemp(prefix="vi-migrate-"))
    legacy = root / "vector_index"
    legacy.mkdir(parents=True, exist_ok=True)
    # Write a fake legacy layout — just meta.json + the two .npy
    # files (their contents don't matter for the migration test).
    (legacy / "meta.json").write_text(json.dumps({
        "dim": 4, "count": 1, "class_name": "Legacy",
        "source_url": "http://wv:8080", "built_at": 0}))
    import numpy as np  # type: ignore[import]
    np.save(legacy / "turns.npy",
             np.zeros((1, 4), dtype=np.float32))
    np.save(legacy / "turn_ids.npy",
             np.asarray([1], dtype=np.int64))

    # Trigger the migration via load_or_empty.
    idx = vector_index.TurnVectorIndex.load_or_empty(
        root, slug="_default")
    _aT("legacy meta.json moved out of vector_index/ root",
        not (legacy / "meta.json").exists())
    _aT("meta.json now lives under _default/",
        (legacy / "_default" / "meta.json").exists())
    _aT("loaded index has the migrated vectors",
        idx.is_loaded and idx.vectors.shape[0] == 1,
        f"loaded={idx.is_loaded} count="
        f"{0 if not idx.is_loaded else idx.vectors.shape[0]}")


def test_cli_rebuild_accepts_slug_flag() -> None:
    _step("v0.9.5: `claude-dejavu vector-index rebuild --slug X` "
          "registered in argparse")
    src = (ROOT / "code" / "recall.py").read_text(encoding="utf-8")
    _aT("recall.py registers --slug arg on vector-index rebuild",
        "--slug" in src and "cmd_vector_index_rebuild" in src,
        "")
    _aT("rebuild handler passes project_slug to "
        "bootstrap_from_weaviate",
        "project_slug=project_filter" in src
         or "project_slug=" in src,
        "")


def test_bootstrap_signature_accepts_project_slug() -> None:
    _step("v0.9.5: bootstrap_from_weaviate accepts project_slug kwarg")
    if not vector_index._NUMPY_AVAILABLE:
        return
    import inspect
    sig = inspect.signature(
        vector_index.TurnVectorIndex.bootstrap_from_weaviate)
    _aT("project_slug kwarg present",
        "project_slug" in sig.parameters,
        repr(list(sig.parameters)))


def test_mcp_search_resolves_slug_for_single_project_scope() -> None:
    _step("v0.9.5: mcp/server.py picks slug-specific index when "
          "scope resolves to ONE project that has an index on disk")
    src = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    _aT("server.py uses list_known_slugs to pick per-project index",
        "list_known_slugs" in src
         and "slug_choice" in src,
        "")
    _aT("default fallback is `_default`",
        '"_default"' in src and "slug_choice" in src,
        "")


def main() -> int:
    print("v0.9.5 per-project vector index isolation tests")
    tests = [
        test_index_dir_uses_slug_subdir,
        test_two_projects_have_isolated_indexes,
        test_get_index_singleton_per_slug,
        test_list_known_slugs,
        test_legacy_layout_migrates_to_default,
        test_cli_rebuild_accepts_slug_flag,
        test_bootstrap_signature_accepts_project_slug,
        test_mcp_search_resolves_slug_for_single_project_scope,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            _fail(f"{t.__name__} raised", repr(e))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    if FAIL:
        for n, d in FAIL:
            print(f"  ✗ {n}  {d[:120]}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
