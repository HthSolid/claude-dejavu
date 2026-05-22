"""dejavu_bridge_stub.py — degraded-mode fallback for the closed-source
`dejavu_bridge` Python wheel (v0.8.6+).

The real `dejavu_bridge` ships pre-compiled binary code from the private
`dejavu` project (not redistributable except as the published wheel
artifacts). When that wheel is unavailable at install time —
`pip install` couldn't reach DEJAVU_BRIDGE_INDEX_URL, the URL was
unset, the user opted out — claude-dejavu still has to function. This
module re-exposes the same six primitives with naive deterministic
Python implementations so every import site can use the same API path.

API surface (byte-equivalent to the real wheel — locked by
``tests/test_bridge_signature_parity.py`` since the v0.8.7 audit)
================================================================
- distill(text: str, tier: int = 2) -> (label, body)
- disclose(candidates: list[dict], budget_tokens: int) -> list[dict]
- gate(text: str) -> bool
- retrieve_rrf(rankings: list[list[dict]]) -> list[dict]
- sem_cache_lookup(query_vec: list[float],
                   cache_entries: list[dict],
                   high: float = 0.97,
                   low: float = 0.92) -> dict | None
- topic_reweight(hits: list[dict],
                 topic_id: str,
                 hit_topics: dict[str, str],
                 same_topic_factor: float = 0.7,
                 cross_topic_factor: float = 1.3) -> list[dict]
- __version__: str                                "stub" marker

Naive implementations
=====================
None of these compress. None of them embed. None of them learn. The
goal is *correct shape* and *graceful degradation*, not parity:

- distill() returns the input verbatim (no compression). Caller's
  load-bearing-token round-trip still passes, but the gist won't be
  shorter than the source.
- disclose() does a simple greedy fill: keep hits in input order
  until char_budget = token_budget * 4 runs out.
- gate() implements a permissive degraded approximation of the
  real wheel's threshold: accept text that is ≥ 50 chars AND has
  ≥ 30% alphanumeric density. This is the same default the real
  wheel uses; standard mode matches spec.
- retrieve_rrf() merges by simple RRF score k=60 (well-known
  default). Single positional `ranked_lists` arg matches the real
  wheel.
- sem_cache_lookup() does a real linear-scan cosine-similarity
  probe against `{key_vec, value_json}` entries. Returns the
  best entry as `{kind, value_json, similarity}` if similarity
  >= low (0.92 by default), else None. `kind` is "hit" if
  similarity >= high (0.97), else "fuzzy". This is genuinely
  useful: callers can pre-compute query vectors and the cache
  works without a wheel.
- topic_reweight() returns hits unchanged.

The presence of `__version__ == "stub"` lets every caller log that
they're running degraded.
"""
from __future__ import annotations

from typing import Any


__version__ = "stub"


def distill(text: str, tier: int = 2):
    """Stub: identity-pass through. Signature matches the real wheel
    ``distill(text, tier=2)`` exactly so callers using either positional
    or keyword form work in both modes.

    Returns the tuple ``(tier_label, body)`` the real wheel emits.

    The real wheel's `distill` strips boilerplate, normalizes
    whitespace, and tier-selects a compression aggressiveness. We
    just return the source so the caller's round-trip check (does
    the gist contain every load-bearing anchor?) is guaranteed to
    pass. Net effect: `turns.gist` ends up equal to `turns.content`
    in stub mode, which is the worst-case storage outcome but the
    safest semantic one.
    """
    label = f"L{int(tier) if 0 <= int(tier) <= 3 else 2}"
    return (label, text or "")


def disclose(candidates: list[dict[str, Any]],
             budget_tokens: int) -> list[dict[str, Any]]:
    """Stub: greedy fill in input order until the token budget runs out.

    Signature matches the real wheel: ``disclose(candidates,
    budget_tokens)`` — both positional, no keyword-only marker.
    Each candidate is ``{id: str, text: str, score: float}``. Real
    wheel sizes by an identity-tokenizer (1 char = 1 token); we
    do the same so stub-mode callers see equivalent budget math.

    v0.8.7 audit fix (2026-05-19): pre-fix the stub used ``hits``
    + ``token_budget`` kwarg-only, which silently diverged from the
    real wheel and let bug-class drift through every JIT recall
    invocation. tests/test_bridge_signature_parity.py now locks
    both surfaces to identical call shapes.
    """
    if not candidates:
        return []
    budget = max(0, int(budget_tokens))
    out: list[dict[str, Any]] = []
    used = 0
    for c in candidates:
        body = (c.get("text") if isinstance(c, dict) else "") or ""
        size = len(body) if body else 1
        if used + size > budget and out:
            break
        out.append(c)
        used += size
    return out


def gate(text: str) -> bool:
    """Stub: real-wheel-default gate. Accept text iff
    ``len(text) >= 50`` AND alphanumeric density ``>= 0.30``.

    The real wheel rejects low-entropy / boilerplate / dupe turns
    with the same default thresholds. Matching them here keeps the
    stub semantically aligned with the wheel (instead of leaking
    every short / numeric-only turn through), but still
    deterministic and stateless — no embeddings required.
    """
    if not text:
        return False
    s = text.strip()
    if len(s) < 50:
        return False
    n_alnum = sum(1 for c in s if c.isalnum())
    return (n_alnum / len(s)) >= 0.30


def retrieve_rrf(rankings: list[list[dict[str, Any]]]
                 ) -> list[dict[str, Any]]:
    """Stub: textbook reciprocal-rank-fusion with k=60.

    Signature matches the real wheel: ``retrieve_rrf(rankings)`` —
    single positional, no kwargs. ``score = sum_over_lists(1 /
    (60 + rank))``. Each ranking entry is either a dict with an
    ``id`` field, or a bare string id (real wheel accepts both;
    stub mirrors).

    Returns hits in descending fused-score order. Like the real
    wheel, only the ``id`` field is preserved in the output — the
    caller re-attaches its own payload via an id→record map.
    """
    K = 60
    scores: dict[Any, float] = {}
    next_synthetic = 0
    for lst in rankings or []:
        if not lst:
            continue
        for rank, h in enumerate(lst):
            if isinstance(h, dict):
                hid = h.get("id")
            else:
                hid = h
            if hid is None:
                hid = ("__syn__", next_synthetic)
                next_synthetic += 1
            scores[hid] = scores.get(hid, 0.0) + 1.0 / (K + rank + 1)
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return [{"id": hid} for hid, _ in ordered
            if not isinstance(hid, tuple)]


def _cosine(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity. Returns 0.0 on shape mismatch or
    zero-magnitude vectors — silent degradation so a single bad
    entry doesn't poison a whole cache scan."""
    if not a or not b or len(a) != len(b):
        return 0.0
    num = 0.0
    da = 0.0
    db = 0.0
    for x, y in zip(a, b):
        num += x * y
        da += x * x
        db += y * y
    if da <= 0.0 or db <= 0.0:
        return 0.0
    import math
    return num / (math.sqrt(da) * math.sqrt(db))


def sem_cache_lookup(query_vec: list[float],
                     cache_entries: list[dict[str, Any]],
                     high: float = 0.97,
                     low: float = 0.92,
                     ) -> dict[str, Any] | None:
    """Stub: linear-scan cosine-similarity probe.

    ``cache_entries`` is a list of dicts with at least ``key_vec``
    (the cached query embedding) and ``value_json`` (the cached
    payload). Returns the best-matching entry as ``{kind,
    value_json, similarity}`` if its similarity to ``query_vec``
    is ``>= low``; ``kind`` is ``"hit"`` if ``>= high``, else
    ``"fuzzy"``. Returns None if no entry clears ``low``.

    Matches the real wheel's behaviour modulo no quantization /
    SIMD acceleration — small caches (≤ 128 entries) run in
    well under a millisecond on Python, which is exactly the
    use case (per-process LRU).
    """
    if not query_vec or not cache_entries:
        return None
    best: dict[str, Any] | None = None
    best_sim = -1.0
    for ent in cache_entries:
        if not isinstance(ent, dict):
            continue
        kv = ent.get("key_vec") or ent.get("vector")
        if not kv:
            continue
        sim = _cosine(query_vec, list(kv))
        if sim > best_sim:
            best_sim = sim
            best = ent
    if best is None or best_sim < low:
        return None
    kind = "hit" if best_sim >= high else "fuzzy"
    return {
        "kind": kind,
        "value_json": best.get("value_json"),
        "similarity": best_sim,
    }


def topic_reweight(hits: list[dict[str, Any]],
                   topic_id: str,
                   hit_topics: dict[str, str],
                   same_topic_factor: float = 0.7,
                   cross_topic_factor: float = 1.3,
                   ) -> list[dict[str, Any]]:
    """Apply topic-aware multiplicative re-weighting to a list of hits.

    Signature + semantics match the real wheel
    (`dejavu_bridge.topic_reweight`) verified at v0.8.9 Step 0 smoke
    test:
      - hit id IN hit_topics + topic matches → same_topic_factor
      - hit id IN hit_topics + topic differs → cross_topic_factor
      - hit id NOT IN hit_topics → factor 1.0 (NEUTRAL)

    The neutral case is the important one: the stub historically
    treated unmapped ids as cross-topic, which diverged from the real
    wheel (which is neutral). Same class of audit-bug v0.8.7 caught
    on `disclose`. Fixed in v0.8.9.
    """
    out: list[dict[str, Any]] = []
    topics = hit_topics or {}
    for h in hits or []:
        if not isinstance(h, dict):
            continue
        copy = dict(h)
        hid = str(copy.get("id", ""))
        if hid in topics:
            factor = (same_topic_factor if topics[hid] == topic_id
                      else cross_topic_factor)
        else:
            factor = 1.0
        if "score" in copy:
            try:
                copy["score"] = float(copy["score"]) * factor
            except (TypeError, ValueError):
                pass
        copy["factor"] = factor
        out.append(copy)
    return out


__all__ = [
    "__version__",
    "disclose",
    "distill",
    "gate",
    "retrieve_rrf",
    "sem_cache_lookup",
    "topic_reweight",
]
