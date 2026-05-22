"""Learned reranker (v0.10.0) — champion/challenger shadow ranker.

Two rankers run in parallel on every `dejavu_search`:

  CHAMPION   — heuristic boost from v0.9.6
               `final = base_score * (1 + log1p(popularity))`
  CHALLENGER — logistic-regression scorer over 7 features

The champion's ranking is what the model sees. The challenger's
ranking is logged alongside in `recall_ranking_trials`. When a
follow-up `dejavu_expand` fires within 60s, the trial's `expanded`
flag is set on the row matching that turn_id. The promotion arbiter
periodically aggregates: over the last N=1000 decided trials, how
often did the challenger rank the expanded turn HIGHER than the
champion? Promote when win-rate ≥ 60%; auto-demote symmetrically
when a promoted challenger drops below 50% over a fresh window.

Features (`extract_features`):
  0. bm25_score          — pg-lexical FTS rank or hybrid BM25 component
  1. hnsw_score          — Weaviate / local-vector cosine sim
  2. recency_days        — negative log of (now - turn.ts) in days
  3. gist_token_overlap  — fraction of query tokens present in gist
  4. same_project        — 1.0 when hit.project_slug == query project
  5. source_tag_weaviate — 1.0 when the hit came from the weaviate branch
  6. popularity_boost    — v0.9.6 heuristic boost as-is (let LR learn
                            whether it's predictive)

Persistence: `<data_root>/ranker/weights.json` — 8 floats
(7 weights + bias). The synthetic baseline (`baseline_weights`) is
generated deterministically so a fresh install has sensible
starting weights without any user feedback.

NEVER raises. Every failure mode (no numpy, missing weights, PG
unreachable, bad payload) falls back to the heuristic. The
shadow log is best-effort: if logging fails we still return the
champion's ranking.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Optional

try:
    import numpy as np  # type: ignore[import]
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False
    np = None  # type: ignore[assignment]


N_FEATURES = 7
_FEATURE_NAMES = (
    "bm25_score", "hnsw_score", "recency_days",
    "gist_token_overlap", "same_project",
    "source_tag_weaviate", "popularity_boost",
)


def _resolve_mode() -> str:
    """Active ranker mode:
        'heuristic' — only the v0.9.6 popularity boost (champion).
        'learned'   — only the LR scorer (challenger).
        'auto'      — heuristic active, LR shadowed (default).
                       Promotion arbiter may flip 'auto' to 'learned'
                       in-process when challenger wins decisively.
    """
    raw = os.environ.get(
        "CLAUDE_DEJAVU_RANKER", "auto").lower().strip()
    if raw in ("heuristic", "learned", "auto"):
        return raw
    return "auto"


def _promote_win_rate() -> float:
    raw = os.environ.get(
        "CLAUDE_DEJAVU_RANKER_PROMOTE_WIN_RATE", "0.60")
    try:
        return max(0.50, min(1.0, float(raw)))
    except ValueError:
        return 0.60


def _promote_min_n() -> int:
    raw = os.environ.get(
        "CLAUDE_DEJAVU_RANKER_PROMOTE_MIN_N", "1000")
    try:
        return max(100, int(raw))
    except ValueError:
        return 1000


def _demote_window_n() -> int:
    raw = os.environ.get(
        "CLAUDE_DEJAVU_RANKER_DEMOTE_WINDOW_N", "500")
    try:
        return max(100, int(raw))
    except ValueError:
        return 500


# ─── Feature extraction ────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _tokenize(text: str) -> set[str]:
    return {m.group(0).lower()
            for m in _WORD_RE.finditer(text or "")}


def extract_features(hit: dict, query: str,
                       query_project_slug: str | None = None,
                       popularity_boost: float = 1.0) -> list[float]:
    """Compute the 7 features for one hit. Numbers are deliberately
    unscaled — the LR fit handles normalization via its weights."""
    add = hit.get("_additional") or {}
    source = (add.get("source") or "").lower()
    bm25 = float(add.get("score") or 0.0) if "pg" in source else 0.0
    hnsw = float(add.get("score") or 0.0) if "weaviate" in source \
            or "vector" in source else 0.0
    # Recency: turn ts often comes back as ISO string; tolerate
    # both ISO and unix-epoch numbers.
    recency_days = 0.0
    ts = hit.get("ts")
    if isinstance(ts, (int, float)):
        recency_days = max(0.0,
                             (time.time() - float(ts)) / 86400.0)
    elif isinstance(ts, str) and ts:
        try:
            from datetime import datetime
            recency_days = max(
                0.0,
                (time.time()
                  - datetime.fromisoformat(ts.replace("Z", "+00:00"))
                  .timestamp())
                / 86400.0)
        except Exception:
            recency_days = 0.0
    # Use -log(1+days) so recent turns score HIGHER (0 days → 0;
    # 30 days → ~-3.4).
    recency = -math.log1p(recency_days)

    qtoks = _tokenize(query)
    gist = (hit.get("gist") or hit.get("content") or "")[:500]
    gtoks = _tokenize(gist)
    overlap = (len(qtoks & gtoks) / max(1, len(qtoks))
                if qtoks else 0.0)

    same_project = 1.0 if (query_project_slug
                            and hit.get("project_slug")
                              == query_project_slug) else 0.0
    src_is_weaviate = 1.0 if "weaviate" in source else 0.0

    return [bm25, hnsw, recency, overlap, same_project,
            src_is_weaviate, float(popularity_boost)]


# ─── Logistic-regression scorer ────────────────────────────────────────────


class Ranker:
    """Hand-rolled LR scorer. 7 weights + bias. score = σ(w·x + b).
    No sklearn dep (keeps install size small). NEVER raises on
    score(); returns 0.5 (the σ midpoint) on any failure."""

    def __init__(self, weights: Optional[list[float]] = None,
                  bias: float = 0.0) -> None:
        if weights is None or len(weights) != N_FEATURES:
            weights = [0.0] * N_FEATURES
        self.weights = list(weights)
        self.bias = float(bias)

    @classmethod
    def from_file(cls, fp: Path) -> "Ranker":
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            return cls(d.get("weights") or [], d.get("bias") or 0.0)
        except Exception:
            return cls(baseline_weights(), baseline_bias())

    def to_file(self, fp: Path) -> None:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps({
            "weights": list(self.weights),
            "bias": float(self.bias),
            "feature_names": list(_FEATURE_NAMES),
        }, indent=2), encoding="utf-8")

    def score(self, features: list[float]) -> float:
        try:
            z = self.bias
            for w, x in zip(self.weights, features):
                z += w * x
            # σ(z), clamped to avoid overflow on large |z|.
            if z > 30.0:
                return 1.0
            if z < -30.0:
                return 0.0
            return 1.0 / (1.0 + math.exp(-z))
        except Exception:
            return 0.5

    def fit(self, X: list[list[float]], y: list[int],
             epochs: int = 200, lr: float = 0.05,
             l2: float = 0.001) -> None:
        """Hand-rolled gradient descent. Numpy when available for
        speed; pure Python fallback otherwise. y is 0/1."""
        if _NUMPY_AVAILABLE:
            self._fit_numpy(X, y, epochs, lr, l2)
        else:
            self._fit_pure(X, y, epochs, lr, l2)

    def _fit_numpy(self, X, y, epochs, lr, l2) -> None:
        Xn = np.asarray(X, dtype=np.float64)
        yn = np.asarray(y, dtype=np.float64)
        if Xn.ndim != 2 or Xn.shape[1] != N_FEATURES:
            return
        w = np.asarray(self.weights, dtype=np.float64)
        b = float(self.bias)
        m = Xn.shape[0]
        for _ in range(epochs):
            z = Xn @ w + b
            # numerically stable sigmoid
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            err = p - yn
            grad_w = (Xn.T @ err) / m + l2 * w
            grad_b = err.mean()
            w -= lr * grad_w
            b -= lr * grad_b
        self.weights = w.tolist()
        self.bias = float(b)

    def _fit_pure(self, X, y, epochs, lr, l2) -> None:
        w = list(self.weights)
        b = float(self.bias)
        m = len(X)
        if not m:
            return
        for _ in range(epochs):
            grad_w = [0.0] * N_FEATURES
            grad_b = 0.0
            for xi, yi in zip(X, y):
                z = b + sum(w[k] * xi[k] for k in range(N_FEATURES))
                z = max(-30.0, min(30.0, z))
                p = 1.0 / (1.0 + math.exp(-z))
                err = p - yi
                for k in range(N_FEATURES):
                    grad_w[k] += err * xi[k]
                grad_b += err
            for k in range(N_FEATURES):
                grad_w[k] = grad_w[k] / m + l2 * w[k]
                w[k] -= lr * grad_w[k]
            b -= lr * (grad_b / m)
        self.weights = w
        self.bias = b


# ─── Synthetic baseline ────────────────────────────────────────────────────


def baseline_weights() -> list[float]:
    """Deterministic non-trivial weights so a fresh install isn't
    sitting on all-zeros. Chosen to roughly approximate the
    heuristic: source_tag and same_project contribute moderately,
    recency and overlap matter, popularity_boost gets a small
    initial pull. Real feedback overwrites these via `train`."""
    return [
        0.40,   # bm25_score
        0.40,   # hnsw_score
        0.20,   # recency (negative log of days; 'recent' is positive)
        0.80,   # gist_token_overlap (strong signal)
        0.60,   # same_project (moderate)
        0.10,   # source_tag_weaviate (weak)
        0.30,   # popularity_boost (moderate)
    ]


def baseline_bias() -> float:
    return -0.5


def generate_synthetic_training_set(n: int = 5000,
                                       seed: int = 42
                                       ) -> tuple[list[list[float]],
                                                    list[int]]:
    """Generate a synthetic labeled dataset for the baseline. Each
    sample is a feature vector; the label is 1 when the
    `heuristic-but-noisy` ranking would say "relevant", 0 otherwise.

    This gives the LR a sensible starting point without ever touching
    user data — useful for the synthetic-baseline ship-time fit."""
    import random as _r
    rng = _r.Random(seed)
    X: list[list[float]] = []
    y: list[int] = []
    for _ in range(n):
        bm25 = max(0.0, rng.gauss(0.5, 0.3))
        hnsw = max(0.0, rng.gauss(0.5, 0.3))
        recency = -rng.uniform(0, 6)  # -log1p(days), 0..6 = today..~400 days
        overlap = rng.random()
        same_project = float(rng.random() < 0.4)
        src_wv = float(rng.random() < 0.5)
        pop_boost = 1.0 + rng.expovariate(2.0)
        x = [bm25, hnsw, recency, overlap, same_project,
              src_wv, pop_boost]
        # Heuristic relevance: high overlap + same_project + good
        # recency + at least one of BM25/HNSW + popularity. A
        # noisy threshold over the convex combination.
        score = (0.30 * bm25 + 0.30 * hnsw + 0.15 * recency
                 + 0.50 * overlap + 0.25 * same_project
                 + 0.05 * src_wv + 0.20 * math.log1p(pop_boost))
        # Add label noise so the LR doesn't just memorise the
        # heuristic exactly — we want a generalisation baseline.
        threshold = 0.6 + rng.gauss(0, 0.1)
        label = 1 if score > threshold else 0
        X.append(x)
        y.append(label)
    return X, y


def fit_baseline_ranker() -> Ranker:
    """Fit a Ranker on the deterministic synthetic set. Idempotent —
    same seed → same weights every call. Used at install time and
    when `<data_root>/ranker/weights.json` is missing.

    v0.10.1 — when numpy isn't available the pure-Python fit is
    O(epochs × samples × features). 200 × 5000 × 7 = 7M inner ops
    blocks for ~30s on a fresh install. Time-box the pure-Python
    path to 50 epochs and 1000 samples so the first MCP startup
    stays sub-second. The numpy path keeps full epochs/samples
    because it finishes in <1s either way."""
    if _NUMPY_AVAILABLE:
        X, y = generate_synthetic_training_set()
        r = Ranker(baseline_weights(), baseline_bias())
        r.fit(X, y, epochs=200, lr=0.05, l2=0.001)
    else:
        X, y = generate_synthetic_training_set(n=1000)
        r = Ranker(baseline_weights(), baseline_bias())
        r.fit(X, y, epochs=50, lr=0.05, l2=0.001)
    return r


# ─── Module-level singleton ────────────────────────────────────────────────


_RANKER: Optional[Ranker] = None
_RANKER_LOADED_FROM: Optional[Path] = None


def get_ranker(data_root: Path) -> Ranker:
    global _RANKER, _RANKER_LOADED_FROM
    fp = data_root / "ranker" / "weights.json"
    if _RANKER is not None and _RANKER_LOADED_FROM == fp:
        return _RANKER
    if fp.exists():
        _RANKER = Ranker.from_file(fp)
    else:
        _RANKER = fit_baseline_ranker()
        try:
            _RANKER.to_file(fp)
        except Exception:
            pass
    _RANKER_LOADED_FROM = fp
    return _RANKER


def reset_ranker() -> None:
    """Drop the cached singleton — next get_ranker() reloads from
    disk. Called from CLI `ranker train` after a re-fit so the MCP
    process picks up the new weights without a restart."""
    global _RANKER, _RANKER_LOADED_FROM
    _RANKER = None
    _RANKER_LOADED_FROM = None


def ranker_status(data_root: Path) -> dict:
    """Reporting helper for dejavu_status."""
    mode = _resolve_mode()
    r = get_ranker(data_root)
    return {
        "mode": mode,
        "promote_win_rate": _promote_win_rate(),
        "promote_min_n": _promote_min_n(),
        "demote_window_n": _demote_window_n(),
        "weights_file": str(
            data_root / "ranker" / "weights.json"),
        "weights_present": (
            data_root / "ranker" / "weights.json").exists(),
        "numpy_available": _NUMPY_AVAILABLE,
        "n_features": N_FEATURES,
        "feature_names": list(_FEATURE_NAMES),
        "weights_summary": {
            name: round(w, 3)
            for name, w in zip(_FEATURE_NAMES, r.weights)
        },
    }
