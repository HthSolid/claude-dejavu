"""Cross-encoder reranker layer (v0.9.0).

Replaces the BM25+vector RRF top-K with a higher-precision rescoring
pass by sending (query, candidate-text) pairs through a cross-encoder
or hosted reranker API. RRF returns the right candidates roughly;
the reranker promotes the BEST of them to the top so the
`limit`-truncated response carries the highest-precision tail.

Provider cascade (first reachable + configured wins):
  1. voyage    — voyageai SDK or HTTP (rerank-2 model by default)
  2. cohere    — cohere SDK or HTTP (rerank-multilingual-v3.0)
  3. bge       — local sentence-transformers (BAAI/bge-reranker-v2-m3
                  by default). No network needed; opt-in via env
                  CLAUDE_DEJAVU_RERANK_LOCAL_MODEL.
  4. off       — no-op, returns hits unchanged. Default.

Activation:
  CLAUDE_DEJAVU_RERANKER=voyage|cohere|bge|auto|off    (default off)
  CLAUDE_DEJAVU_RERANK_TIMEOUT_S=1.5                    (per-provider)

`auto` walks the cascade in order. Specific provider names short-
circuit to that provider only (and skip to off on failure — don't
silently fall back to a different model the user didn't pick).

Public API (one function — kept minimal so wiring stays surgical):

    rerank(query: str, hits: list[dict],
           text_field: str = "content",
           top_k: int | None = None,
           timeout_s: float | None = None) -> list[dict]

Each hit must have the `text_field` set OR a `gist` fallback. The
returned list is `hits` re-ordered by relevance (with each hit's
`_additional` dict carrying a `rerank_score` and `rerank_source`).

Defensive: on ANY failure (no API key / network error / timeout /
malformed response) the original ordering is returned unchanged. We
never break recall to add a rerank.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


_DEFAULT_TIMEOUT_S = 1.5

# v0.9.1 — per-project reranker config. A `.dejavu-rerank` file in
# the current working directory (or any ancestor up to $HOME) can
# set CLAUDE_DEJAVU_RERANKER / RERANK_TIMEOUT_S / model name without
# polluting the process env. File format is the same KEY=VALUE
# shape as `config.env`. Env vars still WIN — file is a per-project
# DEFAULT, not an override.

def _find_project_config_file() -> dict[str, str]:
    """Walk from cwd up to $HOME looking for a `.dejavu-rerank`
    file. Returns parsed config, or {} on any failure."""
    try:
        cwd = Path.cwd()
        home = Path.home()
    except Exception:
        return {}
    current = cwd
    visited = 0
    while current and visited < 40:
        visited += 1
        candidate = current / ".dejavu-rerank"
        if candidate.exists():
            try:
                cfg: dict[str, str] = {}
                for ln in candidate.read_text(
                        encoding="utf-8").splitlines():
                    ln = ln.strip()
                    if not ln or ln.startswith("#") or "=" not in ln:
                        continue
                    k, v = ln.split("=", 1)
                    cfg[k.strip()] = v.strip()
                return cfg
            except Exception:
                return {}
        if current == home or current.parent == current:
            break
        current = current.parent
    return {}


def _get(key: str, default: str = "") -> str:
    """Read a config value, env-first then per-project file."""
    raw = os.environ.get(key)
    if raw is not None and raw != "":
        return raw
    return _find_project_config_file().get(key, default)

_VOYAGE_MODEL = os.environ.get(
    "CLAUDE_DEJAVU_VOYAGE_RERANK_MODEL", "rerank-2")
_COHERE_MODEL = os.environ.get(
    "CLAUDE_DEJAVU_COHERE_RERANK_MODEL",
    "rerank-multilingual-v3.0")
_BGE_MODEL = os.environ.get(
    "CLAUDE_DEJAVU_RERANK_LOCAL_MODEL",
    "BAAI/bge-reranker-v2-m3")


def _text_of(hit: dict, text_field: str) -> str:
    """Pick the text the reranker scores. Prefer the explicit
    `text_field` (usually 'content'), then 'gist', then 'ai_title' —
    so even sparsely-populated hits still get a meaningful score."""
    for k in (text_field, "content", "gist", "ai_title"):
        v = hit.get(k)
        if v:
            return str(v)
    return ""


def _voyage_rerank(query: str, texts: list[str],
                    top_k: int, timeout_s: float
                    ) -> list[tuple[int, float]] | None:
    """POST to Voyage AI's rerank endpoint. Returns [(index, score),
    …] sorted by score desc, or None on any failure.
    """
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        return None
    body = {
        "query": query, "documents": texts,
        "model": _VOYAGE_MODEL, "top_k": top_k,
        "return_documents": False,
    }
    try:
        req = urllib.request.Request(
            "https://api.voyageai.com/v1/rerank",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError,
             TimeoutError, Exception):
        return None
    out: list[tuple[int, float]] = []
    for r in payload.get("data") or []:
        idx = r.get("index")
        score = r.get("relevance_score")
        if isinstance(idx, int) and isinstance(score, (int, float)):
            out.append((idx, float(score)))
    return out or None


def _cohere_rerank(query: str, texts: list[str],
                    top_k: int, timeout_s: float
                    ) -> list[tuple[int, float]] | None:
    """POST to Cohere's rerank v2 endpoint."""
    api_key = os.environ.get("COHERE_API_KEY")
    if not api_key:
        return None
    body = {
        "query": query, "documents": texts,
        "model": _COHERE_MODEL, "top_n": top_k,
        "return_documents": False,
    }
    try:
        req = urllib.request.Request(
            "https://api.cohere.com/v2/rerank",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError,
             TimeoutError, Exception):
        return None
    out: list[tuple[int, float]] = []
    for r in payload.get("results") or []:
        idx = r.get("index")
        score = r.get("relevance_score")
        if isinstance(idx, int) and isinstance(score, (int, float)):
            out.append((idx, float(score)))
    return out or None


# Lazy-loaded BGE model singleton — first call pays the load cost
# (~1-3s for the cross-encoder), subsequent calls are sub-100ms for
# top-K ≤ 50 on a CPU.
_BGE_MODEL_CACHE: object = None
_BGE_LOAD_FAILED = False


def _bge_rerank(query: str, texts: list[str],
                 top_k: int, timeout_s: float
                 ) -> list[tuple[int, float]] | None:
    """Local cross-encoder via sentence-transformers. NO network.
    Skipped when the package isn't installed."""
    global _BGE_MODEL_CACHE, _BGE_LOAD_FAILED
    if _BGE_LOAD_FAILED:
        return None
    if _BGE_MODEL_CACHE is None:
        try:
            # Use the CrossEncoder API — cheap to load, predicts on
            # (query, text) pairs natively.
            from sentence_transformers import (  # type: ignore[import]
                CrossEncoder)
            _BGE_MODEL_CACHE = CrossEncoder(_BGE_MODEL)
        except Exception:
            _BGE_LOAD_FAILED = True
            return None
    try:
        pairs = [(query, t) for t in texts]
        # No native timeout on .predict — assume CPU is fast enough.
        # If a future bench shows otherwise we can move to a thread
        # with a timeout. For now: deterministic and reasonably
        # quick (<200ms for top-K ≤ 50 on a modern CPU).
        scores = _BGE_MODEL_CACHE.predict(pairs)  # type: ignore[union-attr]
        # CrossEncoder returns numpy.ndarray[float32].
        ranked: list[tuple[int, float]] = list(enumerate(
            float(s) for s in scores))
        ranked.sort(key=lambda x: -x[1])
        return ranked[:top_k]
    except Exception:
        return None


_PROVIDERS = {
    "voyage": _voyage_rerank,
    "cohere": _cohere_rerank,
    "bge": _bge_rerank,
}


def _resolve_provider() -> str:
    raw = (_get("CLAUDE_DEJAVU_RERANKER") or "off").lower()
    if raw in ("", "off", "false", "0", "no"):
        return "off"
    if raw in _PROVIDERS:
        return raw
    if raw in ("auto", "true", "1", "yes", "on"):
        return "auto"
    return "off"


def _resolve_timeout() -> float:
    raw = _get("CLAUDE_DEJAVU_RERANK_TIMEOUT_S")
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        return max(0.1, float(raw))
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def rerank(query: str, hits: list[dict],
            text_field: str = "content",
            top_k: int | None = None,
            timeout_s: float | None = None) -> list[dict]:
    """Re-order `hits` by reranker relevance. See module docstring.

    Returns the (possibly re-ordered) list with `_additional`
    annotated. NEVER raises — on any provider failure returns the
    input list unchanged.
    """
    if not hits or not query:
        return hits
    provider = _resolve_provider()
    if provider == "off":
        return hits
    timeout_s = timeout_s if timeout_s is not None else _resolve_timeout()
    k = top_k if top_k is not None else len(hits)
    k = max(1, min(k, len(hits)))

    # Materialize text for each hit. If too few have usable text,
    # skip the rerank — scoring against empty strings dilutes the
    # signal.
    texts = [_text_of(h, text_field) for h in hits]
    usable = sum(1 for t in texts if t.strip())
    if usable < max(2, len(hits) // 3):
        return hits

    cascade = (("voyage", "cohere", "bge")
                if provider == "auto" else (provider,))

    started = time.perf_counter()
    for name in cascade:
        fn = _PROVIDERS.get(name)
        if fn is None:
            continue
        remaining = max(0.1, timeout_s - (time.perf_counter() - started))
        scored = fn(query, texts, k, remaining)
        if not scored:
            continue
        # Apply the new ordering. Hits not in `scored` retain their
        # original relative order at the tail.
        new_indices = [idx for idx, _ in scored]
        in_scored = set(new_indices)
        # Annotate scored hits.
        score_by_idx = {idx: s for idx, s in scored}
        ordered: list[dict] = []
        for idx in new_indices:
            h = dict(hits[idx])
            add = dict(h.get("_additional") or {})
            add["rerank_score"] = score_by_idx[idx]
            add["rerank_source"] = name
            h["_additional"] = add
            ordered.append(h)
        # Append unscored tail in original order.
        for i, h in enumerate(hits):
            if i in in_scored:
                continue
            ordered.append(h)
        return ordered

    # All providers failed — preserve original ordering.
    return hits


def reranker_status() -> dict:
    """Diagnostic helper for dejavu_status / dejavu_doctor. Reports
    which provider is configured and whether keys / models are
    reachable without actually running a rerank."""
    out: dict = {
        "configured": _resolve_provider(),
        "timeout_s": _resolve_timeout(),
        "voyage_key_set": bool(os.environ.get("VOYAGE_API_KEY")),
        "cohere_key_set": bool(os.environ.get("COHERE_API_KEY")),
        "bge_model_id": _BGE_MODEL,
    }
    # Check whether sentence_transformers is importable (without
    # actually instantiating the model — that's expensive).
    try:
        import importlib.util as _ilu
        out["bge_package_available"] = (
            _ilu.find_spec("sentence_transformers") is not None)
    except Exception:
        out["bge_package_available"] = False
    return out
