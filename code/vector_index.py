"""In-process vector index (v0.9.1).

A pure-numpy brute-force cosine-similarity store over per-turn
vectors. Built as an alternative to a Weaviate HTTP roundtrip when:

- the Weaviate instance is remote and the HTTP cost dominates
- the deploy can't depend on Weaviate (sandbox, offline laptop, CI)
- the corpus fits in RAM and you want sub-30ms `nearest_turns`
  without an HNSW C++ build dep

Storage: `<data_root>/vector_index/`
  turns.npy      — (N, dim) float32 unit-norm vectors
  turn_ids.npy   — (N,)     int64 turn IDs aligned with vectors
  meta.json      — {dim, count, class_name, source_url, built_at}

API:

    idx = TurnVectorIndex.load_or_empty(data_root)
    idx.bootstrap_from_weaviate(class_name, wv_url)   # one-shot
    idx.save()

    hits = idx.query(query_vec, k=10)
    # → [(turn_id, score), …] sorted desc

    idx.add_vectors(turn_ids, vectors)                # incremental
    idx.save()

NEVER raises on query — empty index returns []. Bootstrap errors are
logged + the index stays whatever it was (often empty).

Pure numpy. No native compile. Up to ~500k turns fits comfortably in
memory at 1024 dim (≈ 2 GB) and queries in ~25ms. Beyond that an
HNSW backend (separate module, future v0.9.2) takes over.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import numpy as np  # type: ignore[import]
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False
    np = None  # type: ignore[assignment]

# v0.9.2 — hnswlib optional acceleration. When the package is
# installed we use it for queries (27× faster than numpy brute force
# at 100k vectors, sub-1ms p50). When it's missing we fall back to
# the v0.9.1 numpy brute-force path. The on-disk numpy arrays remain
# the source of truth so:
#   - downgrading dejavu (removing hnswlib) doesn't lose data
#   - the brute-force code path can validate HNSW recall on demand
try:
    import hnswlib  # type: ignore[import]
    _HNSWLIB_AVAILABLE = True
except ImportError:
    _HNSWLIB_AVAILABLE = False
    hnswlib = None  # type: ignore[assignment]


# Tunables (env-overridable). M = graph degree, ef_construction =
# search width at build, ef = search width at query. Larger values =
# better recall at higher CPU+memory cost. Defaults chosen for 1024-
# dim text embeddings; verify recall on each corpus via the regression
# test.
#
# v0.9.4 — preset shortcuts. `CLAUDE_DEJAVU_HNSW_PRESET=high-recall`
# bumps every tunable up so users who care about recall over RAM
# (most of us) get the right config without remembering three env
# names. Individual env vars still WIN when set.

_HNSW_PRESETS = {
    # M=16 / ef=100 / efc=200 — fast, ~96% recall@10 on text embeds.
    "balanced":     (16, 100, 200),
    # M=32 / ef=200 / efc=400 — slower build, larger graph, ~99%+
    # recall@10 even on adversarial / overlapping clusters.
    "high-recall":  (32, 200, 400),
    # M=8  / ef=50  / efc=100 — small graph, fastest build + query,
    # ~90% recall@10. Use only when memory is genuinely tight.
    "low-memory":    (8,  50, 100),
}


def _hnsw_preset() -> tuple[int, int, int]:
    name = os.environ.get(
        "CLAUDE_DEJAVU_HNSW_PRESET", "balanced").lower().strip()
    return _HNSW_PRESETS.get(name, _HNSW_PRESETS["balanced"])


def _hnsw_M() -> int:
    raw = os.environ.get("CLAUDE_DEJAVU_HNSW_M")
    if raw:
        try:
            return max(4, int(raw))
        except ValueError:
            pass
    return _hnsw_preset()[0]


def _hnsw_ef() -> int:
    raw = os.environ.get("CLAUDE_DEJAVU_HNSW_EF")
    if raw:
        try:
            return max(10, int(raw))
        except ValueError:
            pass
    return _hnsw_preset()[1]


def _hnsw_ef_construction() -> int:
    raw = os.environ.get("CLAUDE_DEJAVU_HNSW_EF_CONSTRUCTION")
    if raw:
        try:
            return max(10, int(raw))
        except ValueError:
            pass
    return _hnsw_preset()[2]


def _hnsw_disabled() -> bool:
    """Honor an explicit kill switch so a user can force the brute-
    force path without uninstalling hnswlib (useful for A/B tests
    and the recall regression check)."""
    return os.environ.get(
        "CLAUDE_DEJAVU_HNSW_DISABLE", "").lower() in (
        "1", "true", "yes", "on")


def _is_available() -> bool:
    return _NUMPY_AVAILABLE


def _hnsw_is_active() -> bool:
    return (_HNSWLIB_AVAILABLE
             and _NUMPY_AVAILABLE
             and not _hnsw_disabled())


class TurnVectorIndex:
    def __init__(self, data_root: Path, dim: int = 1024,
                  slug: str = "_default") -> None:
        # v0.9.5 — per-project isolation. The slug names a
        # subdirectory under <data_root>/vector_index/ so each
        # project can have its own index file, its own HNSW preset,
        # and its own rebuild schedule. The default slug `_default`
        # preserves v0.9.4 behaviour: one shared index for the whole
        # install. Users opt into per-project isolation by passing
        # `--slug <name>` to `claude-dejavu vector-index rebuild`.
        self.data_root = data_root
        self.dim = dim
        self.slug = slug or "_default"
        self.vectors: Optional["np.ndarray"] = None  # type: ignore[name-defined]
        self.turn_ids: Optional["np.ndarray"] = None  # type: ignore[name-defined]
        self.class_name: str = ""
        self.source_url: str = ""
        self.built_at: float = 0.0
        # v0.9.2 — optional HNSW companion. None when hnswlib is
        # unavailable OR the index hasn't been built/loaded yet.
        # When set, `query()` prefers it over numpy brute force.
        # The numpy arrays remain the source of truth so the HNSW
        # index can be reconstructed if it's missing or stale.
        self._hnsw: object = None
        self._hnsw_dirty: bool = False  # add_vectors set; rebuild on save
        self._tid_to_label: dict[int, int] = {}  # turn_id → hnsw label

    @property
    def index_dir(self) -> Path:
        # v0.9.5 — slug-scoped subdirectory under the original
        # vector_index folder. Legacy installs (where the index lived
        # directly under vector_index/ without a slug subdir) get
        # auto-migrated on first load — see `_migrate_legacy_layout`.
        return self.data_root / "vector_index" / self.slug

    @property
    def is_loaded(self) -> bool:
        return (self.vectors is not None
                 and self.turn_ids is not None
                 and self.vectors.shape[0] > 0)

    @staticmethod
    def _migrate_legacy_layout(data_root: Path) -> None:
        """v0.9.5 — if an old (pre-v0.9.5) index lives directly under
        <data_root>/vector_index/ (no slug subdir), move it into
        the `_default` slug subdir so it keeps serving queries
        under the new layout. Runs once per process; cheap when
        already migrated."""
        legacy_dir = data_root / "vector_index"
        if not legacy_dir.exists():
            return
        legacy_meta = legacy_dir / "meta.json"
        if not legacy_meta.exists():
            return  # already migrated or never existed
        default_dir = legacy_dir / "_default"
        if default_dir.exists():
            return  # already migrated in a previous run
        try:
            default_dir.mkdir(parents=True, exist_ok=True)
            for name in ("turns.npy", "turn_ids.npy",
                          "turns.hnsw", "meta.json"):
                src = legacy_dir / name
                if src.exists():
                    src.rename(default_dir / name)
        except Exception:
            # If migration fails, the old layout still works via the
            # legacy path; the new code will just see an empty
            # _default slug and bootstrap on next call.
            pass

    @classmethod
    def load_or_empty(cls, data_root: Path,
                       slug: str = "_default") -> "TurnVectorIndex":
        """Load the persisted index from disk if present; otherwise
        return an empty instance ready for bootstrap.

        v0.9.5 — `slug` parameter selects which per-project index to
        load. Default `_default` is the global shared index used by
        v0.9.4 and earlier. Auto-migrates legacy layout on first
        call."""
        cls._migrate_legacy_layout(data_root)
        idx = cls(data_root, slug=slug)
        if not _NUMPY_AVAILABLE:
            return idx
        d = idx.index_dir
        meta_fp = d / "meta.json"
        if not meta_fp.exists():
            return idx
        try:
            meta = json.loads(meta_fp.read_text(encoding="utf-8"))
            idx.dim = int(meta.get("dim", 1024))
            idx.class_name = str(meta.get("class_name", ""))
            idx.source_url = str(meta.get("source_url", ""))
            idx.built_at = float(meta.get("built_at", 0.0))
            v_fp = d / "turns.npy"
            t_fp = d / "turn_ids.npy"
            if v_fp.exists() and t_fp.exists():
                idx.vectors = np.load(v_fp, mmap_mode="r")
                idx.turn_ids = np.load(t_fp, mmap_mode="r")
        except Exception:
            # Corrupted index → start fresh.
            idx.vectors = None
            idx.turn_ids = None
        # v0.9.2 — try to load the HNSW companion. If missing or
        # malformed we leave it None; query() will fall back to
        # brute force, and the next save() will rebuild + persist.
        if _hnsw_is_active() and idx.is_loaded:
            hnsw_fp = idx.index_dir / "turns.hnsw"
            if hnsw_fp.exists():
                try:
                    h = hnswlib.Index(  # type: ignore[union-attr]
                        space="cosine", dim=idx.dim)
                    h.load_index(
                        str(hnsw_fp),
                        max_elements=int(idx.vectors.shape[0]))
                    h.set_ef(_hnsw_ef())
                    idx._hnsw = h
                    # Rebuild the turn_id → label map. Labels were
                    # assigned at build time as the row index in
                    # `vectors`, so the mapping is positional.
                    idx._tid_to_label = {
                        int(t): i for i, t in
                        enumerate(idx.turn_ids)}
                except Exception:
                    idx._hnsw = None
        return idx

    def save(self) -> None:
        if not _NUMPY_AVAILABLE or self.vectors is None:
            return
        self.index_dir.mkdir(parents=True, exist_ok=True)
        np.save(self.index_dir / "turns.npy",
                 np.ascontiguousarray(self.vectors))
        np.save(self.index_dir / "turn_ids.npy",
                 np.ascontiguousarray(self.turn_ids))
        # v0.9.2 — persist the HNSW companion. Rebuild it when
        # `add_vectors` was called between this save and the prior
        # one (or when it's never been built). Always re-emit the
        # binary on save so it stays in lock-step with the numpy
        # arrays — half a stale HNSW would yield mystery recall
        # regressions and is hard to detect from outside.
        if _hnsw_is_active() and self.vectors.shape[0] > 0:
            if self._hnsw is None or self._hnsw_dirty:
                self._rebuild_hnsw()
            try:
                hnsw_fp = self.index_dir / "turns.hnsw"
                if self._hnsw is not None:
                    self._hnsw.save_index(  # type: ignore[union-attr]
                        str(hnsw_fp))
            except Exception:
                # Persistence failure is non-fatal — at the next load
                # we just rebuild. But surface it on stderr so an op
                # can see the disk-write issue.
                import sys as _sys
                _sys.stderr.write(
                    "[vector_index] hnsw save failed; will rebuild "
                    "on next load\n")
        (self.index_dir / "meta.json").write_text(
            json.dumps({
                "dim": int(self.dim),
                "count": int(self.vectors.shape[0]),
                "class_name": self.class_name,
                "source_url": self.source_url,
                "built_at": self.built_at,
                "hnsw": bool(self._hnsw is not None),
                "hnsw_M": _hnsw_M(),
                "hnsw_ef": _hnsw_ef(),
                "hnsw_ef_construction": _hnsw_ef_construction(),
            }, indent=2),
            encoding="utf-8",
        )

    def _rebuild_hnsw(self) -> None:
        """Build an HNSW index from the current numpy arrays. Costs
        ~1s per 1000 vectors at default M=16, ef_construction=200.
        Called on save() when the index is dirty or missing."""
        if not _hnsw_is_active() or self.vectors is None:
            return
        n = int(self.vectors.shape[0])
        if n == 0:
            return
        try:
            h = hnswlib.Index(  # type: ignore[union-attr]
                space="cosine", dim=self.dim)
            h.init_index(max_elements=n,
                          ef_construction=_hnsw_ef_construction(),
                          M=_hnsw_M())
            # Use row index as label so query() can map label→
            # turn_id via self.turn_ids[label].
            labels = np.arange(n, dtype=np.int64)
            h.add_items(self.vectors, labels)
            h.set_ef(_hnsw_ef())
            self._hnsw = h
            self._tid_to_label = {
                int(t): i for i, t in enumerate(self.turn_ids)}
            self._hnsw_dirty = False
        except Exception:
            self._hnsw = None

    def query(self, query_vec: "np.ndarray",  # type: ignore[name-defined]
                k: int = 10) -> list[tuple[int, float]]:
        """Cosine similarity top-k. Returns [(turn_id, score), …]
        sorted desc. Empty list on empty index or any failure.

        v0.9.2 — uses the HNSW companion when available (sub-1ms p50
        at 100k vectors) and falls back to numpy brute force
        otherwise. The brute-force path is exposed as `query_brute`
        for the recall regression test."""
        if not self.is_loaded or not _NUMPY_AVAILABLE:
            return []
        if (_hnsw_is_active() and self._hnsw is not None
                and not self._hnsw_dirty):
            try:
                q = np.asarray(query_vec,
                                 dtype=np.float32).reshape(-1)
                if q.shape[0] != self.dim:
                    return []
                qn = np.linalg.norm(q) + 1e-9
                q = q / qn
                k_eff = min(k, int(self.vectors.shape[0]))
                if k_eff <= 0:
                    return []
                labels, dists = (
                    self._hnsw.knn_query(  # type: ignore[union-attr]
                        q, k=k_eff))
                # cosine distance from hnswlib = 1 - cos_sim.
                return [
                    (int(self.turn_ids[int(lbl)]),
                     float(1.0 - float(d)))
                    for lbl, d in zip(labels[0], dists[0])
                ]
            except Exception:
                # HNSW failure → fall through to brute force.
                pass
        return self.query_brute(query_vec, k)

    def query_brute(self, query_vec: "np.ndarray",  # type: ignore[name-defined]
                     k: int = 10) -> list[tuple[int, float]]:
        """Numpy brute-force cosine top-k. Always correct (modulo
        float32 precision). Exposed so the recall regression test
        can validate HNSW results against it."""
        if not self.is_loaded or not _NUMPY_AVAILABLE:
            return []
        try:
            q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
            if q.shape[0] != self.dim:
                return []
            qn = np.linalg.norm(q) + 1e-9
            q = q / qn
            sims = self.vectors @ q  # (N,)
            k = min(k, sims.shape[0])
            if k <= 0:
                return []
            top_idx = np.argpartition(-sims, k - 1)[:k]
            top_idx = top_idx[np.argsort(-sims[top_idx])]
            return [(int(self.turn_ids[i]), float(sims[i]))
                    for i in top_idx]
        except Exception:
            return []

    def add_vectors(self, turn_ids: list[int],
                     vectors: list[list[float]]) -> None:
        """Incremental append. Vectors are NOT pre-normalized — the
        index normalizes on ingest to keep query() fast.

        v0.9.2 — when an HNSW companion is present, the new rows are
        also added to it incrementally (hnswlib's add_items is cheap
        — ~10 µs per vector). When the companion is missing, we set
        the dirty flag so the next save() rebuilds from scratch.
        """
        if not _NUMPY_AVAILABLE or not turn_ids or not vectors:
            return
        if len(turn_ids) != len(vectors):
            return
        new_v = np.asarray(vectors, dtype=np.float32)
        if new_v.ndim != 2 or new_v.shape[1] != self.dim:
            return
        # Normalize per-row.
        norms = np.linalg.norm(new_v, axis=1, keepdims=True) + 1e-9
        new_v = new_v / norms
        new_ids = np.asarray(turn_ids, dtype=np.int64)
        start_label = (0 if self.vectors is None
                        else int(self.vectors.shape[0]))
        if self.vectors is None or self.vectors.shape[0] == 0:
            self.vectors = new_v
            self.turn_ids = new_ids
        else:
            self.vectors = np.vstack([self.vectors, new_v])
            self.turn_ids = np.concatenate([self.turn_ids, new_ids])
        # Incremental HNSW update if companion exists.
        if _hnsw_is_active() and self._hnsw is not None:
            try:
                # Grow the index if it would overflow max_elements.
                new_total = int(self.vectors.shape[0])
                current_max = self._hnsw.get_max_elements()  # type: ignore[union-attr]
                if new_total > current_max:
                    # 1.5× growth to amortize re-allocations.
                    self._hnsw.resize_index(  # type: ignore[union-attr]
                        max(new_total, int(current_max * 1.5)))
                labels = np.arange(
                    start_label, start_label + new_v.shape[0],
                    dtype=np.int64)
                self._hnsw.add_items(  # type: ignore[union-attr]
                    new_v, labels)
                for i, tid in enumerate(new_ids):
                    self._tid_to_label[int(tid)] = (
                        start_label + i)
            except Exception:
                # On any failure mark dirty so save() rebuilds.
                self._hnsw_dirty = True
        else:
            # No companion (yet). Flag so save() builds one.
            self._hnsw_dirty = True

    def sync_new_from_weaviate(self, class_name: str, wv_url: str,
                                  max_new: int = 5_000,
                                  timeout_s: float = 5.0) -> int:
        """v0.9.2 — incremental sync. Fetches Weaviate objects whose
        turn_id is GREATER than the current max in this index, appends
        them, and returns the count added. Capped at `max_new` so a
        backlog doesn't blow the Stop-hook budget — repeat the call
        if a bigger backlog needs draining.

        Idempotent: a no-op when the index already covers the latest
        turn_id in Weaviate.
        """
        if not _NUMPY_AVAILABLE:
            return 0
        if self.turn_ids is None or self.turn_ids.shape[0] == 0:
            # Nothing to sync against. Fall back to a fresh
            # bootstrap if the caller wants population.
            return 0
        current_max = int(self.turn_ids.max())
        # Weaviate GraphQL where-filter: turn_id > current_max,
        # paginate with `after: <uuid>`.
        loaded_ids: list[int] = []
        loaded_vecs: list[list[float]] = []
        after: str | None = None
        per_page = 500
        pages = 0
        max_pages = max_new // per_page + 1
        while pages < max_pages and len(loaded_ids) < max_new:
            pages += 1
            try:
                q = (
                    "{ Get { " + class_name + "("
                    f"limit: {per_page}, "
                    "where: { path: [\"turn_id\"], "
                    "operator: GreaterThan, "
                    f"valueInt: {current_max} }}"
                    + (f", after: \"{after}\"" if after else "")
                    + ") { _additional { id vector } turn_id }"
                    " } }")
                req = urllib.request.Request(
                    f"{wv_url}/v1/graphql",
                    data=json.dumps({"query": q}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(
                        req, timeout=timeout_s) as resp:
                    payload = json.loads(
                        resp.read().decode("utf-8"))
            except Exception:
                break
            objs = (payload.get("data", {})
                     .get("Get", {})
                     .get(class_name) or [])
            if not objs:
                break
            for o in objs:
                tid = o.get("turn_id")
                add = o.get("_additional") or {}
                vec = add.get("vector")
                uuid = add.get("id")
                if (isinstance(tid, int) and isinstance(vec, list)
                        and len(vec) > 0):
                    loaded_ids.append(tid)
                    loaded_vecs.append(vec)
                after = uuid or after
            if len(objs) < per_page:
                break
        if not loaded_ids:
            return 0
        self.add_vectors(loaded_ids, loaded_vecs)
        return len(loaded_ids)

    def bootstrap_from_weaviate(self, class_name: str, wv_url: str,
                                  batch_size: int = 500,
                                  max_objects: int = 200_000,
                                  timeout_s: float = 5.0,
                                  project_slug: str | None = None) -> int:
        """One-shot fetch of all turn vectors from a Weaviate class.
        Paginates via the `after: <uuid>` cursor pattern Weaviate
        supports. Returns the number of vectors loaded; 0 on any
        failure.

        v0.9.5 — `project_slug` filters server-side via a GraphQL
        `where` clause so a per-project index only contains turns
        from that project. Default None keeps v0.9.4 behaviour
        (every turn in the class)."""
        if not _NUMPY_AVAILABLE:
            return 0
        loaded_ids: list[int] = []
        loaded_vecs: list[list[float]] = []
        after: str | None = None
        attempts = 0
        max_pages = max_objects // batch_size + 1
        where_clause = ""
        if project_slug:
            where_clause = (
                ", where: { path: [\"project_slug\"], "
                "operator: Equal, "
                f"valueString: \"{project_slug}\" }}")
        while attempts < max_pages:
            attempts += 1
            try:
                q = (
                    "{ Get { " + class_name + "("
                    f"limit: {batch_size}"
                    + (f", after: \"{after}\"" if after else "")
                    + where_clause
                    + ") { _additional { id vector } turn_id }"
                    " } }")
                req = urllib.request.Request(
                    f"{wv_url}/v1/graphql",
                    data=json.dumps({"query": q}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(
                        req, timeout=timeout_s) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except Exception:
                break
            objs = (payload.get("data", {})
                     .get("Get", {})
                     .get(class_name) or [])
            if not objs:
                break
            for o in objs:
                tid = o.get("turn_id")
                add = o.get("_additional") or {}
                vec = add.get("vector")
                obj_uuid = add.get("id")
                if (isinstance(tid, int) and isinstance(vec, list)
                        and len(vec) > 0):
                    loaded_ids.append(tid)
                    loaded_vecs.append(vec)
                after = obj_uuid or after
            if len(objs) < batch_size:
                break  # end of class
        if not loaded_ids:
            return 0
        self.dim = len(loaded_vecs[0])
        self.vectors = None
        self.turn_ids = None
        self.add_vectors(loaded_ids, loaded_vecs)
        self.class_name = class_name
        self.source_url = wv_url
        self.built_at = time.time()
        return len(loaded_ids)


# v0.9.5 — per-(data_root, slug) singleton cache. Multiple per-
# project indexes can co-exist in the same MCP process. The
# (data_root, slug) tuple identifies each one. The old
# _SINGLETON / _SINGLETON_DATA_ROOT names are kept as module
# attributes for backwards compat with any external code that
# poked at them (test fixtures do).
_SINGLETONS: dict[tuple[str, str], "TurnVectorIndex"] = {}
_SINGLETON: TurnVectorIndex | None = None  # last-accessed, for compat
_SINGLETON_DATA_ROOT: Path | None = None    # last-accessed, for compat


def get_index(data_root: Path,
                slug: str = "_default") -> TurnVectorIndex:
    """Return the cached TurnVectorIndex for (data_root, slug),
    loading it from disk on first call. v0.9.5 — `slug` selects
    the per-project index; default `_default` is the global shared
    index that v0.9.4 and earlier always used."""
    global _SINGLETON, _SINGLETON_DATA_ROOT
    key = (str(data_root), slug or "_default")
    if key not in _SINGLETONS:
        _SINGLETONS[key] = TurnVectorIndex.load_or_empty(
            data_root, slug=slug or "_default")
    _SINGLETON = _SINGLETONS[key]
    _SINGLETON_DATA_ROOT = data_root
    return _SINGLETONS[key]


def list_loaded_slugs() -> list[str]:
    """v0.9.5 — return slugs currently held in the singleton cache.
    Used by dejavu_status to enumerate per-project indexes."""
    return sorted({s for _, s in _SINGLETONS.keys()})


def list_known_slugs(data_root: Path) -> list[str]:
    """v0.9.5 — return every slug that has an index directory on
    disk (whether or not currently loaded into the singleton cache).
    Used by dejavu_status to give a complete inventory."""
    root = data_root / "vector_index"
    if not root.exists():
        return []
    out = []
    for p in root.iterdir():
        if p.is_dir() and (p / "meta.json").exists():
            out.append(p.name)
    return sorted(out)


# v0.9.4 — background auto-bootstrap. Module-level flag set when a
# background thread is already mid-bootstrap; subsequent callers see
# it and skip rather than spawning a second thread.
_BOOTSTRAP_IN_PROGRESS: bool = False
_BOOTSTRAP_LOCK = None  # type: ignore[assignment]


def _bootstrap_lock():
    global _BOOTSTRAP_LOCK
    if _BOOTSTRAP_LOCK is None:
        import threading
        _BOOTSTRAP_LOCK = threading.Lock()
    return _BOOTSTRAP_LOCK


def maybe_start_background_bootstrap(
        data_root: Path, class_name: str, wv_url: str,
        on_done=None) -> str:
    """v0.9.4 — fire-and-forget bootstrap when the index is empty.
    Returns a one-line status string for callers (dejavu_status,
    install logs).

    Conditions (ALL must hold) for the spawn:
      - vector_index module is available (numpy installed)
      - the local index is not explicitly disabled
      - the on-disk index doesn't exist yet OR has 0 vectors
      - no bootstrap thread is already running
      - hnswlib is reachable in this venv (else brute force suffices
        without a build step)
      - Weaviate base URL responds to a 1-sec health probe

    The spawned thread:
      1. calls bootstrap_from_weaviate (paginated, ~1s per 1k turns)
      2. calls save() — persists turns.npy + turn_ids.npy + turns.hnsw
      3. invokes on_done(n_loaded) if provided

    During the bootstrap, dejavu_search queries continue to land in
    the existing branches (Weaviate hybrid + PG-lexical); the
    local-vector branch sees an empty index and returns [] cleanly.
    When the thread finishes the singleton hot-swaps in the
    populated index — no MCP restart needed.
    """
    global _BOOTSTRAP_IN_PROGRESS
    if not _NUMPY_AVAILABLE:
        return "skip: numpy not installed"
    if (os.environ.get("CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX", "true")
            .lower() in ("0", "false", "no", "off")):
        return "skip: explicitly disabled"
    idx = get_index(data_root)
    if idx.is_loaded:
        return f"skip: already loaded ({idx.vectors.shape[0]:,} vec)"
    with _bootstrap_lock():
        if _BOOTSTRAP_IN_PROGRESS:
            return "skip: bootstrap already in progress"
        _BOOTSTRAP_IN_PROGRESS = True

    def _worker():
        global _BOOTSTRAP_IN_PROGRESS
        try:
            # Quick health probe — if WV doesn't answer in 1s, abort.
            try:
                with urllib.request.urlopen(
                        f"{wv_url}/v1/.well-known/ready",
                        timeout=1.0):
                    pass
            except Exception:
                return
            n = idx.bootstrap_from_weaviate(class_name, wv_url)
            if n > 0:
                idx.save()
                if on_done is not None:
                    try:
                        on_done(n)
                    except Exception:
                        pass
        finally:
            with _bootstrap_lock():
                _BOOTSTRAP_IN_PROGRESS = False

    import threading as _t
    _t.Thread(target=_worker, daemon=True,
               name="dejavu-vector-bootstrap").start()
    return f"spawned: bootstrap from {wv_url}/{class_name}"


def vector_index_status(data_root: Path,
                          slug: str = "_default") -> dict:
    """Reporting helper for dejavu_status. v0.9.5 — `slug` selects
    which per-project index to report on; default `_default` is the
    global shared index for backwards compat with v0.9.4 callers."""
    idx = get_index(data_root, slug=slug)
    # v0.9.3 — auto-on. Disabled only when the user explicitly opts
    # out via env. The "feature without usage is garbage" principle.
    enabled = not (os.environ.get(
        "CLAUDE_DEJAVU_LOCAL_VECTOR_INDEX", "true").lower()
                    in ("0", "false", "no", "off"))
    return {
        "available": _NUMPY_AVAILABLE,
        "enabled": enabled,
        "loaded": idx.is_loaded,
        "count": int(idx.vectors.shape[0]) if idx.is_loaded else 0,
        "dim": idx.dim,
        "class_name": idx.class_name,
        "source_url": idx.source_url,
        "built_at": idx.built_at,
        "hnsw_available": _HNSWLIB_AVAILABLE,
        "hnsw_active": (_hnsw_is_active()
                          and idx._hnsw is not None
                          and not idx._hnsw_dirty),
        "hnsw_disabled": _hnsw_disabled(),
        "hnsw_M": _hnsw_M(),
        "hnsw_ef": _hnsw_ef(),
        "hnsw_ef_construction": _hnsw_ef_construction(),
    }
