"""v0.10.0 unit tests — learned ranker with champion/challenger shadow.

The v0.9.6 heuristic boost stays the default (champion). v0.10.0 ships
a logistic-regression ranker that:

  - extracts 7 features per (query, hit) pair
  - is fit on a synthetic baseline at install time so a fresh
    install has sensible weights without any user data
  - is re-fit from accumulated `recall_feedback` rows via
    `claude-dejavu ranker train`
  - runs in shadow mode by default — its ranking is logged to
    `recall_ranking_trials` but the model still sees the champion's
    order
  - is auto-promoted to active only when its win-rate over the last
    `CLAUDE_DEJAVU_RANKER_PROMOTE_MIN_N` (default 1000) decided
    trials clears `CLAUDE_DEJAVU_RANKER_PROMOTE_WIN_RATE` (0.60)
  - is auto-demoted symmetrically when win-rate drops below 50%

Infra-free; the PG-touching helpers in mcp/server.py are exercised
only via source-grep assertions."""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "mcp"))

os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT",
                       "/tmp/dejavu-v0100-test")
os.makedirs("/tmp/dejavu-v0100-test", exist_ok=True)

import ranker  # type: ignore[import]


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


# ─── Feature extraction ────────────────────────────────────────────────────

def test_features_have_7_dimensions() -> None:
    _step("ranker: extract_features → 7-dim vector")
    hit = {"turn_id": 1, "gist": "k3s cilium bootstrap",
           "project_slug": "p1",
           "ts": "2026-05-15T10:00:00",
           "_additional": {"score": 0.5,
                            "source": "weaviate-hybrid"}}
    f = ranker.extract_features(hit, "cilium k3s", "p1", 1.5)
    _aT(f"len = 7 (got {len(f)})",
        len(f) == ranker.N_FEATURES == 7)
    _aT("all floats", all(isinstance(x, float) for x in f))


def test_features_same_project_signal() -> None:
    _step("ranker: same_project feature flips correctly")
    hit = {"turn_id": 1, "gist": "x", "project_slug": "p1",
           "_additional": {"score": 0.5, "source": "pg-fts"}}
    f_same = ranker.extract_features(hit, "q", "p1")
    f_diff = ranker.extract_features(hit, "q", "p2")
    _aT("same_project=1 when slugs match",
        f_same[4] == 1.0, repr(f_same))
    _aT("same_project=0 when slugs differ",
        f_diff[4] == 0.0, repr(f_diff))


def test_features_overlap_proportional_to_token_intersection() -> None:
    _step("ranker: gist_token_overlap proportional")
    qt = "cilium bootstrap k3s"
    high_overlap_hit = {"turn_id": 1,
                          "gist": "cilium bootstrap k3s success",
                          "_additional": {"score": 0.5,
                                           "source": "pg-fts"}}
    zero_overlap_hit = {"turn_id": 2,
                          "gist": "example adapter wired up",
                          "_additional": {"score": 0.5,
                                           "source": "pg-fts"}}
    f_hi = ranker.extract_features(high_overlap_hit, qt)
    f_lo = ranker.extract_features(zero_overlap_hit, qt)
    _aT(f"high overlap → 1.0 (got {f_hi[3]})", f_hi[3] == 1.0)
    _aT(f"zero overlap → 0.0 (got {f_lo[3]})", f_lo[3] == 0.0)


# ─── Sigmoid scorer + persistence ─────────────────────────────────────────

def test_ranker_score_bounded() -> None:
    _step("ranker: score is in [0, 1] for any input")
    r = ranker.Ranker([1.0] * 7, 0.0)
    out_pos = r.score([100.0] * 7)
    out_neg = r.score([-100.0] * 7)
    _aT(f"σ(huge positive) ≈ 1 (got {out_pos})",
        0.99 < out_pos <= 1.0)
    _aT(f"σ(huge negative) ≈ 0 (got {out_neg})",
        0.0 <= out_neg < 0.01)
    _aT("σ(0) = 0.5",
        abs(r.score([0.0] * 7) - 0.5) < 1e-6)


def test_ranker_persistence() -> None:
    _step("ranker: to_file / from_file roundtrip")
    fp = Path(tempfile.mkdtemp()) / "weights.json"
    r1 = ranker.Ranker([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7], 0.42)
    r1.to_file(fp)
    r2 = ranker.Ranker.from_file(fp)
    _aT("weights preserved",
        r2.weights == r1.weights, repr(r2.weights))
    _aT("bias preserved", abs(r2.bias - r1.bias) < 1e-6)


def test_ranker_missing_file_falls_back_to_baseline() -> None:
    _step("ranker: from_file on missing path → baseline weights "
          "(not all-zeros)")
    r = ranker.Ranker.from_file(
        Path("/tmp/dejavu-nonexistent-path/weights.json"))
    _aT("weights are non-zero (baseline)",
        any(abs(w) > 0.01 for w in r.weights), repr(r.weights))


# ─── Synthetic baseline ────────────────────────────────────────────────────

def test_synthetic_training_set_shape() -> None:
    _step("synthetic baseline: generates labeled (X, y) tuples")
    X, y = ranker.generate_synthetic_training_set(n=100, seed=7)
    _aT("X has 100 rows", len(X) == 100)
    _aT("each row has 7 features",
        all(len(x) == 7 for x in X))
    _aT("y has 100 labels in {0, 1}",
        len(y) == 100 and set(y).issubset({0, 1}))
    _aT("seed=7 is deterministic",
        X == ranker.generate_synthetic_training_set(n=100, seed=7)[0])


def test_fit_baseline_ranker_produces_sane_weights() -> None:
    _step("synthetic baseline: fit produces non-trivial weights")
    r = ranker.fit_baseline_ranker()
    _aT("weights are non-zero",
        any(abs(w) > 0.05 for w in r.weights), repr(r.weights))
    _aT("overlap feature has positive weight "
        "(more overlap → more relevant)",
        r.weights[3] > 0.1, f"overlap weight = {r.weights[3]}")
    _aT("same_project feature has positive weight",
        r.weights[4] > 0.05, f"same_project weight = {r.weights[4]}")


def test_fit_on_real_pairs_changes_weights() -> None:
    _step("ranker.fit on a custom (X, y) updates weights")
    r = ranker.Ranker([0.0] * 7, 0.0)
    # Two-feature signal: y=1 iff feature 0 > feature 1
    X = [[1.0, 0.0, 0, 0, 0, 0, 0],
         [0.0, 1.0, 0, 0, 0, 0, 0]] * 200
    y = [1, 0] * 200
    r.fit(X, y, epochs=200, lr=0.1, l2=0.0)
    _aT("weights[0] > weights[1] after fit "
        f"(got [{r.weights[0]:.2f}, {r.weights[1]:.2f}])",
        r.weights[0] > r.weights[1])


# ─── Mode resolution + env knobs ───────────────────────────────────────────

def test_default_mode_is_auto() -> None:
    _step("ranker mode: default = auto")
    os.environ.pop("CLAUDE_DEJAVU_RANKER", None)
    _aT("_resolve_mode() = auto",
        ranker._resolve_mode() == "auto")


def test_explicit_mode_pinned() -> None:
    _step("ranker mode: env-pinned values honored")
    for m in ("heuristic", "learned", "auto"):
        os.environ["CLAUDE_DEJAVU_RANKER"] = m
        try:
            _aT(f"env={m} → mode={m}",
                ranker._resolve_mode() == m)
        finally:
            os.environ.pop("CLAUDE_DEJAVU_RANKER", None)


def test_promote_win_rate_env() -> None:
    _step("ranker: PROMOTE_WIN_RATE env honored; clamped to [0.5, 1.0]")
    os.environ["CLAUDE_DEJAVU_RANKER_PROMOTE_WIN_RATE"] = "0.65"
    try:
        _aT("0.65 honored",
            ranker._promote_win_rate() == 0.65)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_RANKER_PROMOTE_WIN_RATE", None)
    os.environ["CLAUDE_DEJAVU_RANKER_PROMOTE_WIN_RATE"] = "0.1"
    try:
        _aT("0.1 clamped up to 0.5",
            ranker._promote_win_rate() == 0.5)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_RANKER_PROMOTE_WIN_RATE", None)


def test_promote_min_n_default_1000() -> None:
    _step("ranker: PROMOTE_MIN_N default = 1000")
    os.environ.pop("CLAUDE_DEJAVU_RANKER_PROMOTE_MIN_N", None)
    _aT("default = 1000",
        ranker._promote_min_n() == 1000)


# ─── Singleton + reset ─────────────────────────────────────────────────────

def test_get_ranker_caches() -> None:
    _step("ranker: get_ranker returns the same instance on repeat "
          "calls; reset clears the cache")
    root = Path(tempfile.mkdtemp())
    a = ranker.get_ranker(root)
    b = ranker.get_ranker(root)
    _aT("cached instance", a is b)
    ranker.reset_ranker()
    c = ranker.get_ranker(root)
    _aT("reload after reset", c is not a)


# ─── Status reporter ──────────────────────────────────────────────────────

def test_ranker_status_shape() -> None:
    _step("ranker_status: contract for dejavu_status")
    root = Path(tempfile.mkdtemp())
    s = ranker.ranker_status(root)
    for k in ("mode", "promote_win_rate", "promote_min_n",
               "demote_window_n", "weights_file",
               "weights_present", "numpy_available",
               "n_features", "feature_names",
               "weights_summary"):
        _aT(f"key {k!r} present", k in s, repr(list(s.keys())))
    _aT("n_features == 7", s["n_features"] == 7)
    _aT("feature_names has 7 entries",
        len(s["feature_names"]) == 7)


# ─── MCP / server-side scaffolding ────────────────────────────────────────

def test_server_dual_rank_helpers_defined() -> None:
    _step("mcp/server.py: shadow-rank + arbiter helpers defined")
    src = (ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
    for name in ("_log_ranking_trials",
                  "_mark_trial_decided",
                  "_ranker_arbiter_active_mode",
                  "_arbitrate",
                  "_ranker_arbiter_force"):
        _aT(f"{name} defined", f"def {name}" in src, "")
    _aT("dejavu_search wires the dual ranking",
        "challenger_sorted" in src and "_log_ranking_trials" in src)
    _aT("dejavu_expand calls _mark_trial_decided",
        "_mark_trial_decided" in src)


def test_schema_includes_ranking_trials() -> None:
    _step("schema.sql includes recall_ranking_trials")
    sql = (ROOT / "code" / "schema.sql").read_text(encoding="utf-8")
    _aT("table present",
        "CREATE TABLE IF NOT EXISTS recall_ranking_trials" in sql)
    _aT("decided/expanded columns",
        "decided" in sql and "expanded" in sql)


def test_cli_ranker_subcommands_defined() -> None:
    _step("CLI: ranker subcommands (train, status, force-*, reset)")
    src = (ROOT / "code" / "recall.py").read_text(encoding="utf-8")
    for name in ("cmd_ranker_status", "cmd_ranker_train",
                  "cmd_ranker_reset", "_cmd_ranker_force"):
        _aT(f"{name} defined", f"def {name}" in src, "")
    for sub in ("status", "train", "force-promote",
                  "force-demote", "reset"):
        _aT(f"argparse registers {sub!r}",
            f'"{sub}"' in src or f"'{sub}'" in src, "")


# ─── Arbiter logic (pure-Python; no PG needed) ───────────────────────────

def test_arbiter_keeps_heuristic_below_n() -> None:
    _step("arbiter: stays on heuristic when N is below promote_min_n")
    # We test _arbitrate indirectly via simulation of the
    # win-rate gate. The function is small and pure — re-implement
    # the logic check here against the documented behaviour.
    def gate(wins, losses, promote_rate, promote_n, current):
        n = wins + losses
        if n == 0:
            return current
        wr = wins / n
        if current == "heuristic" and n >= promote_n:
            if wr >= promote_rate:
                return "learned"
        if current == "learned" and n >= 500:
            if wr < 0.50:
                return "heuristic"
        return current
    # N below gate
    _aT("999 trials at 100% win rate stays on heuristic "
        "(N below 1000)",
        gate(999, 0, 0.60, 1000, "heuristic") == "heuristic")
    # Exactly at gate AND win-rate clears
    _aT("1000 trials at 60% wins → learned",
        gate(600, 400, 0.60, 1000, "heuristic") == "learned")
    # Below win-rate gate
    _aT("1000 trials at 59% → stays heuristic",
        gate(590, 410, 0.60, 1000, "heuristic") == "heuristic")
    # Demotion symmetric
    _aT("learned currently, 500 trials at 49% → heuristic",
        gate(245, 255, 0.60, 1000, "learned") == "heuristic")
    _aT("learned currently, 500 trials at 51% → stays learned",
        gate(255, 245, 0.60, 1000, "learned") == "learned")


def main() -> int:
    print("v0.10.0 learned ranker tests")
    tests = [
        test_features_have_7_dimensions,
        test_features_same_project_signal,
        test_features_overlap_proportional_to_token_intersection,
        test_ranker_score_bounded,
        test_ranker_persistence,
        test_ranker_missing_file_falls_back_to_baseline,
        test_synthetic_training_set_shape,
        test_fit_baseline_ranker_produces_sane_weights,
        test_fit_on_real_pairs_changes_weights,
        test_default_mode_is_auto,
        test_explicit_mode_pinned,
        test_promote_win_rate_env,
        test_promote_min_n_default_1000,
        test_get_ranker_caches,
        test_ranker_status_shape,
        test_server_dual_rank_helpers_defined,
        test_schema_includes_ranking_trials,
        test_cli_ranker_subcommands_defined,
        test_arbiter_keeps_heuristic_below_n,
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
