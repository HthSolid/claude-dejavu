"""Unit tests for hooks/user_prompt_submit.py (v0.8.5 G2).

Mocks the Weaviate + Postgres search path so the suite stays
infra-free and runnable on a laptop with nothing running.

Coverage:
  - keyword extraction (stopwords / dedupe / lowercasing)
  - JIT_RECALL_ENABLED gate
  - JIT_RECALL_MIN_PROMPT_CHARS gate
  - JIT_RECALL_SCORE_THRESHOLD gate (top-hit floor + per-hit drop)
  - JIT_RECALL_TOP_N cap
  - JIT_RECALL_TOKEN_CAP truncation
  - graceful failure on PG / Weaviate exceptions
  - hook always exits 0, never raises
  - jit-recall-preview CLI subcommand
  - output block format parses back into the expected shape
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "user_prompt_submit.py"
RECALL = ROOT / "code" / "recall.py"

# Make `import user_prompt_submit` work in-process.
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "code"))

import user_prompt_submit as ups  # type: ignore[import]  # noqa: E402

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, d: str = "") -> None:
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    if d:
        print(f"      {d}")


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _aT(name: str, cond: bool, d: str = "") -> None:
    _ok(name) if cond else _fail(name, d)


def _seed_cfg(data_root: Path, kv: dict[str, str]) -> None:
    """Write a minimal config.env with the given key=value pairs."""
    (data_root).mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{k}={v}" for k, v in kv.items()) + "\n"
    (data_root / "config.env").write_text(body, encoding="utf-8")


def _run_hook(data_root: Path, payload: dict
              ) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(data_root)}
    # Strip any inherited search-fn injection that doesn't survive
    # subprocess boundary anyway.
    return subprocess.run(
        [sys.executable, str(HOOK)],
        capture_output=True, text=True,
        input=json.dumps(payload),
        env=env,
    )


# ─────────────────────────── keyword extraction ────────────────────────────

def test_extracts_keywords() -> None:
    _step("extract_keywords: stopwords / dedupe / lowercasing")
    out = ups.extract_keywords(
        "Should we switch from rsync to restic for backups?"
    )
    _aT("drops stopwords",
        "we" not in out and "from" not in out and "for" not in out,
        f"got: {out}")
    _aT("keeps content words",
        "rsync" in out and "restic" in out and "backups" in out,
        f"got: {out}")
    _aT("all lowercased", all(t == t.lower() for t in out),
        f"got: {out}")

    dedup = ups.extract_keywords("Rsync rsync RSYNC backups backups")
    _aT("dedupes case-insensitively",
        len([t for t in dedup if t == "rsync"]) == 1,
        f"got: {dedup}")
    _aT("dedupes exact repeats",
        len([t for t in dedup if t == "backups"]) == 1,
        f"got: {dedup}")

    empty = ups.extract_keywords("")
    _aT("empty input → empty list", empty == [], f"got: {empty}")

    short = ups.extract_keywords("of a is to")
    _aT("only-stopwords input → empty list", short == [],
        f"got: {short}")


# ─────────────────────────── flag / guard tests ────────────────────────────

def test_respects_enabled_flag() -> None:
    _step("JIT_RECALL_ENABLED=false → empty block")
    block = ups.compute_recall_block(
        "Should we switch from rsync to restic for backups?",
        cfg={"JIT_RECALL_ENABLED": "false"},
        search_fn=lambda *a, **kw: [
            {"session_id": "x", "ts": "2026-05-17", "score": 0.99,
             "gist": "irrelevant"}],
    )
    _aT("empty when disabled", block == "", f"got: {block!r}")


def test_respects_min_prompt_chars() -> None:
    _step("JIT_RECALL_MIN_PROMPT_CHARS — short prompts skipped")
    cfg = {"JIT_RECALL_ENABLED": "true",
           "JIT_RECALL_MIN_PROMPT_CHARS": "40"}
    short = ups.compute_recall_block(
        "ok", cfg=cfg,
        search_fn=lambda *a, **kw: [
            {"session_id": "x", "ts": "2026-05-17", "score": 0.99,
             "gist": "irrelevant"}],
    )
    _aT("empty for 'ok'", short == "", f"got: {short!r}")
    yes = ups.compute_recall_block(
        "yes", cfg=cfg,
        search_fn=lambda *a, **kw: [
            {"session_id": "x", "ts": "2026-05-17", "score": 0.99,
             "gist": "irrelevant"}],
    )
    _aT("empty for 'yes'", yes == "", f"got: {yes!r}")


def test_respects_score_threshold() -> None:
    _step("JIT_RECALL_SCORE_THRESHOLD — silences below-floor top hits")
    cfg = {"JIT_RECALL_ENABLED": "true",
           "JIT_RECALL_MIN_PROMPT_CHARS": "10",
           "JIT_RECALL_SCORE_THRESHOLD": "0.6"}
    block = ups.compute_recall_block(
        "should we switch from rsync to restic for backups",
        cfg=cfg,
        search_fn=lambda *a, **kw: [
            {"session_id": "abc12345", "ts": "2026-05-17",
             "score": 0.30, "gist": "weak hit"},
        ],
    )
    _aT("empty when top hit < threshold", block == "",
        f"got: {block!r}")

    # Top hit clears threshold but the second one doesn't — the
    # weak one should be dropped, strong one kept.
    block2 = ups.compute_recall_block(
        "should we switch from rsync to restic for backups",
        cfg={**cfg, "JIT_RECALL_TOP_N": "5"},
        search_fn=lambda *a, **kw: [
            {"session_id": "abc12345", "ts": "2026-05-17",
             "score": 0.95, "gist": "strong"},
            {"session_id": "def67890", "ts": "2026-05-16",
             "score": 0.30, "gist": "weak"},
        ],
    )
    _aT("strong hit retained", "strong" in block2,
        f"got: {block2!r}")
    _aT("weak hit dropped", "weak" not in block2,
        f"got: {block2!r}")


def test_respects_top_n() -> None:
    _step("JIT_RECALL_TOP_N — caps at N hits")
    cfg = {"JIT_RECALL_ENABLED": "true",
           "JIT_RECALL_MIN_PROMPT_CHARS": "10",
           "JIT_RECALL_SCORE_THRESHOLD": "0.5",
           "JIT_RECALL_TOP_N": "2"}
    hits = [
        {"session_id": f"sess{i:04d}", "ts": "2026-05-17",
         "score": 0.9 - i * 0.05, "gist": f"hit-{i}"}
        for i in range(10)
    ]
    block = ups.compute_recall_block(
        "should we switch from rsync to restic for backups",
        cfg=cfg, search_fn=lambda *a, **kw: hits,
    )
    n_body_lines = sum(1 for ln in block.splitlines()
                       if ln.startswith("  [sess"))
    _aT("exactly 2 body lines", n_body_lines == 2,
        f"got {n_body_lines} in: {block!r}")
    _aT("hit-0 and hit-1 present",
        "hit-0" in block and "hit-1" in block, f"got: {block!r}")
    _aT("hit-2..hit-9 absent",
        all(f"hit-{i}" not in block for i in range(2, 10)),
        f"got: {block!r}")


def test_respects_token_cap() -> None:
    _step("JIT_RECALL_TOKEN_CAP — truncates with marker when over")
    cfg = {"JIT_RECALL_ENABLED": "true",
           "JIT_RECALL_MIN_PROMPT_CHARS": "10",
           "JIT_RECALL_SCORE_THRESHOLD": "0.5",
           "JIT_RECALL_TOP_N": "20",
           "JIT_RECALL_TOKEN_CAP": "50"}  # 50 tokens × 4 = 200 chars cap
    big_gist = "x" * 400
    hits = [
        {"session_id": f"sess{i:04d}", "ts": "2026-05-17",
         "score": 0.9, "gist": big_gist}
        for i in range(20)
    ]
    block = ups.compute_recall_block(
        "should we switch from rsync to restic for backups",
        cfg=cfg, search_fn=lambda *a, **kw: hits,
    )
    _aT("within hard cap (200 chars)", len(block) <= 200,
        f"len={len(block)}, body={block!r}")
    _aT("truncate marker present", "truncated" in block,
        f"got: {block!r}")


# ─────────────────────────── failure-mode tests ────────────────────────────

def test_graceful_when_pg_down() -> None:
    _step("PG raises → empty block, no exception")

    def _raises(*a, **kw):
        raise RuntimeError("simulated PG unreachable")

    block = ups.compute_recall_block(
        "should we switch from rsync to restic for backups",
        cfg={"JIT_RECALL_ENABLED": "true",
             "JIT_RECALL_MIN_PROMPT_CHARS": "10"},
        search_fn=_raises,
    )
    _aT("empty (no exception escaped)", block == "",
        f"got: {block!r}")


def test_graceful_when_weaviate_down() -> None:
    _step("Weaviate raises → empty block, no exception")

    def _raises(*a, **kw):
        # Mimic urllib's connection-refused style failure.
        raise OSError("simulated Weaviate refused")

    block = ups.compute_recall_block(
        "should we switch from rsync to restic for backups",
        cfg={"JIT_RECALL_ENABLED": "true",
             "JIT_RECALL_MIN_PROMPT_CHARS": "10"},
        search_fn=_raises,
    )
    _aT("empty (no exception escaped)", block == "",
        f"got: {block!r}")


def test_graceful_when_no_hits() -> None:
    _step("search returns [] → empty block")
    block = ups.compute_recall_block(
        "should we switch from rsync to restic for backups",
        cfg={"JIT_RECALL_ENABLED": "true",
             "JIT_RECALL_MIN_PROMPT_CHARS": "10"},
        search_fn=lambda *a, **kw: [],
    )
    _aT("empty when no hits", block == "", f"got: {block!r}")


def test_exit_code_always_zero() -> None:
    _step("hook always exits 0, every failure mode")

    # 1. Empty stdin
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            capture_output=True, text=True, input="",
            env={**os.environ,
                  "CLAUDE_DEJAVU_DATA_ROOT": str(d)},
        )
        _aT("empty stdin → rc=0", r.returncode == 0,
            f"rc={r.returncode} stderr={r.stderr!r}")
        _aT("empty stdin → empty stdout", r.stdout.strip() == "",
            f"got: {r.stdout!r}")

    # 2. Garbage stdin
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            capture_output=True, text=True,
            input="not json at all {{{",
            env={**os.environ,
                  "CLAUDE_DEJAVU_DATA_ROOT": str(d)},
        )
        _aT("garbage stdin → rc=0", r.returncode == 0,
            f"rc={r.returncode} stderr={r.stderr!r}")

    # 3. Missing 'prompt' key
    with tempfile.TemporaryDirectory() as d:
        r = _run_hook(Path(d), {"session_id": "x"})
        _aT("missing prompt → rc=0", r.returncode == 0,
            f"rc={r.returncode} stderr={r.stderr!r}")

    # 4. Disabled flag (the default)
    with tempfile.TemporaryDirectory() as d:
        r = _run_hook(Path(d), {
            "prompt": "should we switch from rsync to restic for backups?"
        })
        _aT("disabled-by-default → rc=0", r.returncode == 0,
            f"rc={r.returncode} stderr={r.stderr!r}")
        _aT("disabled-by-default → empty stdout",
            r.stdout.strip() == "", f"got: {r.stdout!r}")

    # 5. Enabled but backend down (no Weaviate listener at the URL).
    with tempfile.TemporaryDirectory() as d:
        _seed_cfg(Path(d), {
            "JIT_RECALL_ENABLED": "true",
            "JIT_RECALL_MIN_PROMPT_CHARS": "10",
            "WV_URL": "http://127.0.0.1:1",  # nothing listens here
        })
        r = _run_hook(Path(d), {
            "prompt": "should we switch from rsync to restic for backups?"
        })
        _aT("backend-down → rc=0", r.returncode == 0,
            f"rc={r.returncode} stderr={r.stderr!r}")


# ─────────────────────────── CLI preview test ──────────────────────────────

def test_jit_recall_preview_cli_works() -> None:
    _step("claude-dejavu jit-recall-preview — CLI smoke test")
    with tempfile.TemporaryDirectory() as d:
        # Disabled case
        r = subprocess.run(
            [sys.executable, str(RECALL), "jit-recall-preview",
             "should we switch from rsync to restic for backups"],
            capture_output=True, text=True,
            env={**os.environ,
                  "CLAUDE_DEJAVU_DATA_ROOT": str(d)},
        )
        _aT("disabled → rc=0", r.returncode == 0,
            f"rc={r.returncode} stderr={r.stderr!r}")
        _aT("disabled → tells user feature is off",
            "JIT_RECALL_ENABLED=false" in r.stdout,
            f"stdout={r.stdout!r}")

        # Enabled but no backend reachable — still rc=0
        _seed_cfg(Path(d), {
            "JIT_RECALL_ENABLED": "true",
            "JIT_RECALL_MIN_PROMPT_CHARS": "10",
            "WV_URL": "http://127.0.0.1:1",
        })
        r2 = subprocess.run(
            [sys.executable, str(RECALL), "jit-recall-preview",
             "should we switch from rsync to restic for backups"],
            capture_output=True, text=True,
            env={**os.environ,
                  "CLAUDE_DEJAVU_DATA_ROOT": str(d)},
        )
        _aT("enabled+backend-down → rc=0", r2.returncode == 0,
            f"rc={r2.returncode} stderr={r2.stderr!r}")
        _aT("enabled+backend-down → tells user nothing cleared",
            "no hits cleared" in r2.stdout
            or "below JIT_RECALL_MIN_PROMPT_CHARS" in r2.stdout,
            f"stdout={r2.stdout!r}")


# ─────────────────────────── format-parses test ────────────────────────────

def test_hook_output_format_parses() -> None:
    _step("formatted block parses back into expected shape")
    cfg = {"JIT_RECALL_ENABLED": "true",
           "JIT_RECALL_MIN_PROMPT_CHARS": "10",
           "JIT_RECALL_SCORE_THRESHOLD": "0.5",
           "JIT_RECALL_TOP_N": "3"}
    hits = [
        {"session_id": "abc12345xx", "ts": "2026-05-17T10:00:00",
         "score": 0.92,
         "gist": "Switched rsync→restic; saved 60GB and got dedup."},
        {"session_id": "def67890xx", "ts": "2026-05-16T08:00:00",
         "score": 0.71,
         "gist": "Backup retention policy update for restic."},
        {"session_id": "ghi23456xx", "ts": "2026-05-15T11:00:00",
         "score": 0.66,
         "gist": "Verified restic snapshots vs rsync mirror."},
    ]
    block = ups.compute_recall_block(
        "should we switch from rsync to restic for backups",
        cfg=cfg, search_fn=lambda *a, **kw: hits,
    )
    lines = block.splitlines()
    _aT("has header line",
        lines and lines[0].startswith("[claude-dejavu past-context"),
        f"got: {lines[:1]!r}")
    body_lines = [ln for ln in lines if ln.startswith("  [")]
    _aT("3 body lines", len(body_lines) == 3,
        f"got {len(body_lines)}: {body_lines!r}")
    for i, ln in enumerate(body_lines):
        _aT(f"line {i} has 8-char session_id",
            "[" in ln and "]" in ln
            and len(ln.split("]")[0]) == len("  [") + 8,
            f"got: {ln!r}")
        _aT(f"line {i} has score=",
            "score=" in ln, f"got: {ln!r}")


def test_disclose_token_budget_fill() -> None:
    """v0.8.6 Context Economy phase 3 — JIT recall calls
    dejavu_bridge.disclose to do token-budget-aware fill instead of
    fixed top-N truncation. With a small token cap + many same-score
    hits, the result should be fewer than N items.
    """
    _step("v0.8.6: disclose() greedy fill with small token budget")
    cfg = {"JIT_RECALL_ENABLED": "true",
           "JIT_RECALL_MIN_PROMPT_CHARS": "10",
           "JIT_RECALL_SCORE_THRESHOLD": "0.1",
           "JIT_RECALL_TOP_N": "10",
           # 500-char budget (125 tokens × 4). Each hit's gist is
           # 80 chars, so disclose should fit ~6 — fewer than the
           # 10 top_n cap.
           "JIT_RECALL_TOKEN_CAP": "125"}
    big_gist = "X" * 80
    hits = [
        {"session_id": f"sess{i:04d}", "ts": "2026-05-17",
         "score": 0.9, "gist": big_gist}
        for i in range(10)
    ]
    block = ups.compute_recall_block(
        "should we switch from rsync to restic for backups now",
        cfg=cfg, search_fn=lambda *a, **kw: hits,
    )
    body_lines = [ln for ln in block.splitlines()
                  if ln.startswith("  [sess")]
    _aT("fewer than 10 body lines (disclose budgeted)",
        len(body_lines) < 10,
        f"got {len(body_lines)} lines in: {block!r}")
    _aT("at least 1 line (didn't go empty)",
        len(body_lines) >= 1, f"got: {block!r}")


def test_disclose_failure_falls_back_to_top_n() -> None:
    """If dejavu_bridge.disclose raises, the hook must not crash —
    behaviour gracefully reverts to the v0.8.5 top_n truncation."""
    _step("v0.8.6: disclose exception → graceful fallback to top_n")
    cfg = {"JIT_RECALL_ENABLED": "true",
           "JIT_RECALL_MIN_PROMPT_CHARS": "10",
           "JIT_RECALL_SCORE_THRESHOLD": "0.1",
           "JIT_RECALL_TOP_N": "2",
           "JIT_RECALL_TOKEN_CAP": "500"}
    hits = [
        {"session_id": f"sess{i:04d}", "ts": "2026-05-17",
         "score": 0.9, "gist": f"hit-{i}"}
        for i in range(5)
    ]

    # Monkeypatch the bridge module disclose to raise.
    import distill_pipeline as dp
    real = dp._bridge.disclose

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated wheel ICE")

    dp._bridge.disclose = _boom  # type: ignore[assignment]
    try:
        block = ups.compute_recall_block(
            "should we switch from rsync to restic for backups",
            cfg=cfg, search_fn=lambda *a, **kw: hits,
        )
    finally:
        dp._bridge.disclose = real  # type: ignore[assignment]

    body_lines = [ln for ln in block.splitlines()
                  if ln.startswith("  [sess")]
    _aT("hook still produced output (didn't crash)",
        len(body_lines) >= 1, f"got: {block!r}")


def test_v087_rrf_fusion_path_used_when_embed_succeeds() -> None:
    """v0.8.7 Context Economy phase 4: when the caller-side cloud embed
    returns a vector, the JIT hook fuses BM25 + nearVector via
    dejavu_bridge.retrieve_rrf. The breadcrumb logs mode=rrf and the
    fused result includes turn_ids that only nearVector surfaced."""
    _step("v0.8.7: RRF fusion when embed_query succeeds")
    cfg = {"JIT_RECALL_ENABLED": "true",
           "JIT_RECALL_MIN_PROMPT_CHARS": "10",
           "JIT_RECALL_SCORE_THRESHOLD": "0.0",
           "JIT_RECALL_TOP_N": "5",
           "JIT_RECALL_TOKEN_CAP": "1000"}
    # Mock the search_fn directly: simulate that _run_search already
    # ran RRF and returned a fused list with hits unique to each branch
    # (turn 2 unique to nearVector). Done at compute_recall_block layer
    # because that's the unit responsible for "format what _run_search
    # returns"; lower-level RRF wiring is covered in test_search_rrf.py.
    fused_hits = [
        {"session_id": "sess-bm25", "turn_id": 1, "ts": "2026-05-18",
         "score": 5.0, "gist": "bm25-strong-match", "ai_title": "x"},
        {"session_id": "sess-vec", "turn_id": 2, "ts": "2026-05-18",
         "score": 0.9, "gist": "vector-only-match", "ai_title": "y"},
    ]
    block = ups.compute_recall_block(
        "should we migrate from rsync to restic for backups",
        cfg=cfg,
        search_fn=lambda *a, **kw: fused_hits,
    )
    _aT("output block non-empty",
        block.strip() != "", f"got: {block!r}")
    _aT("includes bm25-strong-match gist",
        "bm25-strong-match" in block, f"got: {block!r}")
    _aT("includes vector-only-match gist (RRF surfaced it)",
        "vector-only-match" in block, f"got: {block!r}")


def test_config_settings_registered() -> None:
    _step("config_service.SETTINGS — JIT_RECALL_* registered with right defaults")
    # Import lazily so this suite can run even if config_service has
    # an import-time issue in an unrelated path.
    import config_service as cs  # type: ignore[import]
    expected = {
        "JIT_RECALL_ENABLED":        "false",
        "JIT_RECALL_TOP_N":          "3",
        "JIT_RECALL_TOKEN_CAP":      "500",
        # 2.0 fits the BM25 scoring range (1-20+) the hook uses
        # against the vectorizer:none ClaudeDejavuTurn class. The
        # original 0.6 default was cosine-similarity-shaped and silently
        # filtered ~every real hit (caught in live testing 2026-05-17).
        "JIT_RECALL_SCORE_THRESHOLD": "2.0",
        "JIT_RECALL_MIN_PROMPT_CHARS": "40",
    }
    for key, default in expected.items():
        present = key in cs.SETTINGS
        _aT(f"{key} registered", present, f"keys: {list(cs.SETTINGS)!r}")
        if present:
            s = cs.SETTINGS[key]
            _aT(f"{key} default == {default!r}",
                 s.default == default, f"got: {s.default!r}")


def main() -> int:
    tests = [
        test_extracts_keywords,
        test_respects_enabled_flag,
        test_respects_min_prompt_chars,
        test_respects_score_threshold,
        test_respects_top_n,
        test_respects_token_cap,
        test_graceful_when_pg_down,
        test_graceful_when_weaviate_down,
        test_graceful_when_no_hits,
        test_exit_code_always_zero,
        test_jit_recall_preview_cli_works,
        test_hook_output_format_parses,
        test_disclose_token_budget_fill,
        test_disclose_failure_falls_back_to_top_n,
        test_v087_rrf_fusion_path_used_when_embed_succeeds,
        test_config_settings_registered,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
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
