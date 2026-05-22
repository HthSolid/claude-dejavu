"""Unit tests for v0.8.9 topic_reweight wiring in recall._hybrid +
JIT hook _run_search.

The closed-source bridge primitive `dejavu_bridge.topic_reweight`
multiplicatively rescales hit scores by topic match. v0.8.9 wires it
into BOTH the CLI search path (recall._hybrid) and the JIT recall
hook (user_prompt_submit._run_search). Both wirings are gated by
TOPIC_REWEIGHT_ENABLED — off by default, so v0.8.8 behavior is
preserved bit-for-bit when the user hasn't opted in.

Coverage
========
- topic_reweight off (default): scores unchanged, no bridge call
- topic_reweight on: bridge called with extracted current topic +
  per-hit topic dict built from ai_title fields
- bridge failure / unimportable: falls back to RRF/BM25 order, no crash
- empty query topic (vague query): skipped (no anchor to reweight against)
- override factors via config: respected
- score reweighted in returned dict shape (CLI: _additional.score,
  hook: flat 'score' key)
- ai_title missing for some hits: those treated as unknown topic
  (cross-topic factor applies)
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


def _mk_hit(tid: int, score: float, ai_title: str = "rsync restic backup",
            sess: str = "sess0001") -> dict:
    return {
        "session_id": sess,
        "turn_id": tid,
        "project_slug": "test-proj",
        "role": "user",
        "ts": "2026-05-18",
        "ai_title": ai_title,
        "_additional": {"score": score},
    }


# ─── recall._topic_reweight: CLI path ────────────────────────────────────────

def test_recall_reweight_flag_off_is_noop() -> None:
    """Default (no flag set): scores unchanged, no bridge call. The
    v0.8.8 ordering is preserved bit-for-bit."""
    _step("recall._topic_reweight: flag off → no-op (default v0.8.8)")
    hits = [_mk_hit(1, 10.0), _mk_hit(2, 5.0)]
    # _CFG is the recall module's cached config dict — clear it.
    saved = dict(recall._CFG)
    recall._CFG.pop("TOPIC_REWEIGHT_ENABLED", None)
    try:
        out = recall._topic_reweight(hits, "rsync to restic")
    finally:
        recall._CFG.clear(); recall._CFG.update(saved)
    _aT("returns input unchanged",
        out == hits, f"got: {out!r}")


def test_recall_reweight_flag_on_calls_bridge() -> None:
    """Flag on: bridge.topic_reweight called with extracted topic +
    per-hit topic dict. Scores rewritten."""
    _step("recall._topic_reweight: flag on → bridge called, scores updated")
    hits = [
        _mk_hit(1, 10.0, ai_title="rsync restic backup"),       # same topic
        _mk_hit(2, 10.0, ai_title="example trading bot"),    # cross topic
    ]
    saved = dict(recall._CFG)
    recall._CFG["TOPIC_REWEIGHT_ENABLED"] = "true"
    try:
        out = recall._topic_reweight(hits, "rsync restic migration")
    finally:
        recall._CFG.clear(); recall._CFG.update(saved)
    # Find each by turn_id.
    by_id = {int(h["turn_id"]): float(h["_additional"]["score"])
             for h in out}
    _aT("turn 1 score multiplied by 0.7 (same topic, demote)",
        abs(by_id[1] - 7.0) < 1e-6, f"got: {by_id[1]}")
    _aT("turn 2 score multiplied by 1.3 (cross topic, promote)",
        abs(by_id[2] - 13.0) < 1e-6, f"got: {by_id[2]}")
    _aT("re-sorted by new score: cross-topic first",
        int(out[0]["turn_id"]) == 2, f"got first: {out[0]!r}")


def test_recall_reweight_bridge_failure_returns_input() -> None:
    """When the bridge raises, return the input hits unchanged. Never
    crash recall."""
    _step("recall._topic_reweight: bridge crash → input unchanged")
    hits = [_mk_hit(1, 10.0), _mk_hit(2, 5.0)]
    saved = dict(recall._CFG)
    recall._CFG["TOPIC_REWEIGHT_ENABLED"] = "true"
    import distill_pipeline as dp
    real = dp._bridge.topic_reweight

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated bridge ICE")

    dp._bridge.topic_reweight = _boom  # type: ignore[assignment]
    try:
        out = recall._topic_reweight(hits, "rsync to restic")
    finally:
        dp._bridge.topic_reweight = real  # type: ignore[assignment]
        recall._CFG.clear(); recall._CFG.update(saved)
    _aT("returns input list (no crash)",
        out == hits, f"got: {out!r}")


def test_recall_reweight_empty_query_topic_skipped() -> None:
    """A query too vague to extract a topic (single content word) →
    skip reweighting. The bridge would otherwise treat every hit as
    cross-topic, which isn't useful when there's no anchor."""
    _step("recall._topic_reweight: vague query → skipped")
    hits = [_mk_hit(1, 10.0), _mk_hit(2, 5.0)]
    saved = dict(recall._CFG)
    recall._CFG["TOPIC_REWEIGHT_ENABLED"] = "true"
    try:
        out = recall._topic_reweight(hits, "ok")
    finally:
        recall._CFG.clear(); recall._CFG.update(saved)
    _aT("returns input unchanged when query topic is empty",
        out == hits, f"got: {out!r}")


def test_recall_reweight_factor_overrides() -> None:
    """SAME / CROSS factor overrides from config.env are respected."""
    _step("recall._topic_reweight: factor overrides honored")
    hits = [_mk_hit(1, 10.0, ai_title="rsync restic backup")]
    saved = dict(recall._CFG)
    recall._CFG["TOPIC_REWEIGHT_ENABLED"] = "true"
    recall._CFG["TOPIC_REWEIGHT_SAME_FACTOR"] = "1.5"  # invert (promote same)
    recall._CFG["TOPIC_REWEIGHT_CROSS_FACTOR"] = "0.5"
    try:
        out = recall._topic_reweight(hits, "rsync restic migration")
    finally:
        recall._CFG.clear(); recall._CFG.update(saved)
    score = float(out[0]["_additional"]["score"])
    _aT("same-topic factor 1.5 applied (10 * 1.5 = 15.0)",
        abs(score - 15.0) < 1e-6, f"got: {score}")


def test_recall_reweight_missing_ai_title_treated_as_neutral() -> None:
    """A hit with no ai_title can't have a topic extracted → it's
    omitted from the hit_topics map, and the bridge treats unmapped
    ids as NEUTRAL (factor 1.0). Documented behavior of the real
    dejavu_bridge wheel — verified at v0.8.9 Step 0 smoke test."""
    _step("recall._topic_reweight: hit without ai_title → neutral (factor 1.0)")
    hits = [
        _mk_hit(1, 10.0, ai_title="rsync restic backup"),  # same
        _mk_hit(2, 10.0, ai_title=""),                      # unknown → neutral
    ]
    saved = dict(recall._CFG)
    recall._CFG["TOPIC_REWEIGHT_ENABLED"] = "true"
    try:
        out = recall._topic_reweight(hits, "rsync restic migration")
    finally:
        recall._CFG.clear(); recall._CFG.update(saved)
    by_id = {int(h["turn_id"]): float(h["_additional"]["score"])
             for h in out}
    _aT("turn 1 (same topic) demoted to 7.0",
        abs(by_id[1] - 7.0) < 1e-6, f"got: {by_id[1]}")
    _aT("turn 2 (unknown ai_title) unchanged at 10.0 (neutral factor 1.0)",
        abs(by_id[2] - 10.0) < 1e-6, f"got: {by_id[2]}")


def test_recall_reweight_preserves_hit_fields() -> None:
    """The reweighted hits still carry session_id, project_slug, ts,
    ai_title — only `_additional.score` is rewritten."""
    _step("recall._topic_reweight: non-score fields preserved")
    hits = [_mk_hit(1, 10.0, ai_title="rsync restic", sess="abc12345")]
    saved = dict(recall._CFG)
    recall._CFG["TOPIC_REWEIGHT_ENABLED"] = "true"
    try:
        out = recall._topic_reweight(hits, "rsync restic backup")
    finally:
        recall._CFG.clear(); recall._CFG.update(saved)
    h = out[0]
    _aT("session_id preserved", h["session_id"] == "abc12345")
    _aT("ai_title preserved", h["ai_title"] == "rsync restic")
    _aT("project_slug preserved", h["project_slug"] == "test-proj")


# ─── JIT hook: _topic_reweight_jit ────────────────────────────────────────────

def test_jit_reweight_flag_off_is_noop() -> None:
    _step("JIT _topic_reweight_jit: flag off → no-op")
    hits = [
        {"turn_id": 1, "score": 10.0, "ai_title": "rsync restic"},
        {"turn_id": 2, "score": 5.0, "ai_title": "example bot"},
    ]
    out = ups._topic_reweight_jit(hits, "rsync restic migration", cfg={})
    _aT("returns input unchanged",
        out == hits, f"got: {out!r}")


def test_jit_reweight_flag_on_calls_bridge() -> None:
    _step("JIT _topic_reweight_jit: flag on → bridge called, scores updated")
    hits = [
        {"turn_id": 1, "score": 10.0, "ai_title": "rsync restic backup"},
        {"turn_id": 2, "score": 10.0, "ai_title": "example trading"},
    ]
    cfg = {"TOPIC_REWEIGHT_ENABLED": "true"}
    out = ups._topic_reweight_jit(hits, "rsync restic migration", cfg=cfg)
    by_id = {h["turn_id"]: float(h["score"]) for h in out}
    _aT("same-topic hit (turn 1) demoted to 7.0",
        abs(by_id[1] - 7.0) < 1e-6, f"got: {by_id[1]}")
    _aT("cross-topic hit (turn 2) promoted to 13.0",
        abs(by_id[2] - 13.0) < 1e-6, f"got: {by_id[2]}")
    _aT("re-sorted desc: cross-topic first",
        out[0]["turn_id"] == 2, f"got: {out[0]!r}")


def test_jit_reweight_bridge_failure_returns_input() -> None:
    _step("JIT _topic_reweight_jit: bridge crash → input unchanged")
    hits = [{"turn_id": 1, "score": 10.0, "ai_title": "rsync restic"}]
    cfg = {"TOPIC_REWEIGHT_ENABLED": "true"}
    import distill_pipeline as dp
    real = dp._bridge.topic_reweight

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated bridge ICE")

    dp._bridge.topic_reweight = _boom  # type: ignore[assignment]
    try:
        out = ups._topic_reweight_jit(hits, "rsync restic migration", cfg=cfg)
    finally:
        dp._bridge.topic_reweight = real  # type: ignore[assignment]
    _aT("returns input on crash", out == hits, f"got: {out!r}")


def test_jit_reweight_empty_query_topic_skipped() -> None:
    _step("JIT _topic_reweight_jit: vague query → skipped")
    hits = [{"turn_id": 1, "score": 10.0, "ai_title": "rsync restic"}]
    cfg = {"TOPIC_REWEIGHT_ENABLED": "true"}
    out = ups._topic_reweight_jit(hits, "ok", cfg=cfg)
    _aT("returns input unchanged", out == hits, f"got: {out!r}")


def test_jit_reweight_factor_overrides() -> None:
    _step("JIT _topic_reweight_jit: factor overrides honored")
    hits = [{"turn_id": 1, "score": 10.0, "ai_title": "rsync restic backup"}]
    cfg = {
        "TOPIC_REWEIGHT_ENABLED": "true",
        "TOPIC_REWEIGHT_SAME_FACTOR": "1.5",
        "TOPIC_REWEIGHT_CROSS_FACTOR": "0.5",
    }
    out = ups._topic_reweight_jit(hits, "rsync restic migration", cfg=cfg)
    _aT("same-topic factor 1.5 applied (10 * 1.5 = 15.0)",
        abs(float(out[0]["score"]) - 15.0) < 1e-6,
        f"got: {out[0]['score']}")


def test_jit_run_search_reweights_when_enabled() -> None:
    """End-to-end JIT path: when TOPIC_REWEIGHT_ENABLED=true, the
    hits returned by _run_search reflect the reweighted scores."""
    _step("JIT _run_search: flag on → reweighted scores in returned hits")
    bm25 = [
        {"session_id": "s1", "turn_id": 1, "project_slug": "p", "role": "u",
         "ts": "2026-05-18", "ai_title": "rsync restic backup",
         "_additional": {"score": 10.0}},
        {"session_id": "s2", "turn_id": 2, "project_slug": "p", "role": "u",
         "ts": "2026-05-18", "ai_title": "example trading bot",
         "_additional": {"score": 10.0}},
    ]
    import query_embed as qe
    with patch.object(ups, "_load_cfg",
                       lambda: {"TOPIC_REWEIGHT_ENABLED": "true"}), \
         patch.object(ups, "_bm25_jit",
                       lambda *_a, **_kw: list(bm25)), \
         patch.object(ups, "_near_vector_jit",
                       lambda *_a, **_kw: []), \
         patch.object(qe, "embed_query", lambda *_a, **_kw: None):
        hits = ups._run_search("rsync restic migration",
                                top_n=5, deadline_s=1.5)
    by_id = {h["turn_id"]: float(h["score"]) for h in hits}
    _aT("turn 1 demoted by same-topic factor",
        abs(by_id[1] - 7.0) < 1e-6, f"got: {by_id[1]}")
    _aT("turn 2 promoted by cross-topic factor",
        abs(by_id[2] - 13.0) < 1e-6, f"got: {by_id[2]}")


def test_jit_run_search_default_is_v088() -> None:
    """Default (no flag set): hits keep v0.8.8 scores."""
    _step("JIT _run_search: default → v0.8.8 scores preserved")
    bm25 = [
        {"session_id": "s1", "turn_id": 1, "project_slug": "p", "role": "u",
         "ts": "2026-05-18", "ai_title": "rsync restic backup",
         "_additional": {"score": 10.0}},
    ]
    import query_embed as qe
    with patch.object(ups, "_load_cfg", lambda: {}), \
         patch.object(ups, "_bm25_jit",
                       lambda *_a, **_kw: list(bm25)), \
         patch.object(ups, "_near_vector_jit",
                       lambda *_a, **_kw: []), \
         patch.object(qe, "embed_query", lambda *_a, **_kw: None):
        hits = ups._run_search("rsync restic migration",
                                top_n=5, deadline_s=1.5)
    _aT("score unchanged (10.0)",
        abs(float(hits[0]["score"]) - 10.0) < 1e-6,
        f"got: {hits[0]['score']}")


def main() -> int:
    tests = [
        test_recall_reweight_flag_off_is_noop,
        test_recall_reweight_flag_on_calls_bridge,
        test_recall_reweight_bridge_failure_returns_input,
        test_recall_reweight_empty_query_topic_skipped,
        test_recall_reweight_factor_overrides,
        test_recall_reweight_missing_ai_title_treated_as_neutral,
        test_recall_reweight_preserves_hit_fields,
        test_jit_reweight_flag_off_is_noop,
        test_jit_reweight_flag_on_calls_bridge,
        test_jit_reweight_bridge_failure_returns_input,
        test_jit_reweight_empty_query_topic_skipped,
        test_jit_reweight_factor_overrides,
        test_jit_run_search_reweights_when_enabled,
        test_jit_run_search_default_is_v088,
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
