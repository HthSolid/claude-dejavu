"""v0.9.6 unit tests — recall_feedback loop.

The feedback loop has two halves: capture (implicit via
dejavu_expand, explicit via dejavu_rate) and application (popularity
boost in dejavu_search). All tests are infra-free — they monkey-
patch the PG-touching helpers + the popularity cache directly.
"""
from __future__ import annotations

import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "mcp"))

os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT",
                       "/tmp/dejavu-v0906-test")
os.makedirs("/tmp/dejavu-v0906-test", exist_ok=True)

import server  # type: ignore[import]


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


# ─── Recent-query memory ──────────────────────────────────────────────────

def test_recent_query_push_and_lookup() -> None:
    _step("recent query: push records; lookup within window returns "
          "most-recent")
    server._RECENT_QUERIES.clear()
    server._push_recent_query("first")
    server._push_recent_query("second")
    _aT("most-recent within 60s window = 'second'",
        server._most_recent_query_within(60.0) == "second")


def test_recent_query_window_expiry() -> None:
    _step("recent query: lookup outside the time window returns None")
    server._RECENT_QUERIES.clear()
    # Manually backdate
    server._RECENT_QUERIES.append(("old", time.time() - 120.0))
    _aT("60s window excludes a 120s-old query",
        server._most_recent_query_within(60.0) is None)
    _aT("300s window includes it",
        server._most_recent_query_within(300.0) == "old")


def test_recent_query_cap() -> None:
    _step("recent query: list capped at 20 entries")
    server._RECENT_QUERIES.clear()
    for i in range(50):
        server._push_recent_query(f"q{i}")
    _aT(f"len ≤ 20 (got {len(server._RECENT_QUERIES)})",
        len(server._RECENT_QUERIES) == 20)
    _aT("oldest preserved entry is q30",
        server._RECENT_QUERIES[0][0] == "q30")


# ─── Popularity boost math ────────────────────────────────────────────────

def test_popularity_boost_zero_no_change() -> None:
    _step("popularity: unfeedback'd turn → boost = 1.0 (no change)")
    server._FEEDBACK_POPULARITY.clear()
    _aT("boost = 1.0 for unknown turn_id",
        server._popularity_boost(99999) == 1.0)


def test_popularity_boost_log1p_shape() -> None:
    _step("popularity: boost = 1 + log1p(pop) — bounded growth")
    server._FEEDBACK_POPULARITY[1] = 0.5
    server._FEEDBACK_POPULARITY[10] = 10.0
    server._FEEDBACK_POPULARITY[100] = 100.0
    b1 = server._popularity_boost(1)
    b10 = server._popularity_boost(10)
    b100 = server._popularity_boost(100)
    _aT(f"pop=0.5 → boost ≈ 1.41 (got {b1:.2f})",
        abs(b1 - (1 + math.log1p(0.5))) < 1e-4)
    _aT(f"pop=10 → boost ≈ 3.40 (got {b10:.2f})",
        abs(b10 - (1 + math.log1p(10.0))) < 1e-4)
    _aT(f"pop=100 → boost ≈ 5.62 (got {b100:.2f})",
        abs(b100 - (1 + math.log1p(100.0))) < 1e-4)
    _aT("boost grows monotonically with popularity",
        b1 < b10 < b100)
    server._FEEDBACK_POPULARITY.clear()


def test_popularity_boost_negative_pop_no_inflation() -> None:
    _step("popularity: negative popularity (net-down-voted) → boost "
          "= 1.0 (don't penalize via this branch — let the negative "
          "signal accumulate to drown the turn naturally)")
    server._FEEDBACK_POPULARITY[5] = -10.0
    _aT("negative popularity → boost = 1.0",
        server._popularity_boost(5) == 1.0)
    server._FEEDBACK_POPULARITY.clear()


# ─── Query hash ───────────────────────────────────────────────────────────

def test_query_hash_deterministic() -> None:
    _step("query_hash: same input → same output (blake2b 16 bytes)")
    h1 = server._query_hash("hello world")
    h2 = server._query_hash("hello world")
    h3 = server._query_hash("hello world!")
    _aT("identical queries → identical hashes", h1 == h2)
    _aT("different queries → different hashes", h1 != h3)
    _aT("hash is 16 bytes", len(h1) == 16, f"got {len(h1)}")


# ─── dejavu_rate MCP tool ─────────────────────────────────────────────────

def test_rate_rejects_invalid_score() -> None:
    _step("dejavu_rate: rejects score not in {+1, -1}")
    server._RECENT_QUERIES.clear()
    server._push_recent_query("test query")
    out = server.dejavu_rate(turn_id=123, score=0)
    _aT("score=0 → error", "error" in out.lower(), out)
    out = server.dejavu_rate(turn_id=123, score=42)
    _aT("score=42 → error", "error" in out.lower(), out)


def test_rate_rejects_without_attributable_query() -> None:
    _step("dejavu_rate: rejects when there's no recent search query "
          "to attribute the rating to (and no query= passed)")
    server._RECENT_QUERIES.clear()
    out = server.dejavu_rate(turn_id=123, score=1)
    _aT("no recent query → error",
        "error" in out.lower() and "query" in out.lower(), out)


# ─── Tool registration ───────────────────────────────────────────────────

def test_dejavu_rate_is_registered() -> None:
    _step("MCP: dejavu_rate is importable + callable")
    _aT("server.dejavu_rate exists",
        callable(getattr(server, "dejavu_rate", None)),
        repr(getattr(server, "dejavu_rate", None)))


def test_write_implicit_feedback_signature() -> None:
    _step("module: _write_implicit_feedback accepts a list[int]")
    _aT("callable",
        callable(getattr(server, "_write_implicit_feedback", None)))
    # Without a recent query it's a no-op — never raises.
    server._RECENT_QUERIES.clear()
    try:
        server._write_implicit_feedback([1, 2, 3])
        _ok("no-op when no recent query (didn't raise)")
    except Exception as e:
        _fail("no-op shouldn't raise", repr(e))


def test_refresh_feedback_cache_is_defined() -> None:
    _step("module: _refresh_feedback_cache_if_stale callable")
    _aT("function defined",
        callable(getattr(server,
                          "_refresh_feedback_cache_if_stale", None)))


# ─── Schema migration ─────────────────────────────────────────────────────

def test_schema_includes_recall_feedback() -> None:
    _step("schema.sql includes the recall_feedback table")
    sql = (ROOT / "code" / "schema.sql").read_text(
        encoding="utf-8")
    _aT("CREATE TABLE recall_feedback present",
        "CREATE TABLE IF NOT EXISTS recall_feedback" in sql)
    _aT("signal_type CHECK constraint present",
        "CHECK (signal_type IN (" in sql)
    _aT("turn+ts index present",
        "idx_recall_feedback_turn_ts" in sql)


def test_health_picks_up_new_table() -> None:
    _step("health.expected_pg_tables() includes recall_feedback")
    sys.path.insert(0, str(ROOT / "code"))
    import health  # type: ignore[import]
    _aT("recall_feedback in expected tables",
        "recall_feedback" in health.expected_pg_tables(),
        repr(sorted(health.expected_pg_tables())))


# ─── CLI handlers ─────────────────────────────────────────────────────────

def test_feedback_cli_handlers_defined() -> None:
    _step("CLI: cmd_feedback_stats / cmd_feedback_export exist")
    sys.path.insert(0, str(ROOT / "code"))
    import recall  # type: ignore[import]
    _aT("cmd_feedback_stats defined",
        callable(getattr(recall, "cmd_feedback_stats", None)))
    _aT("cmd_feedback_export defined",
        callable(getattr(recall, "cmd_feedback_export", None)))


def main() -> int:
    print("v0.9.6 recall feedback loop tests")
    tests = [
        test_recent_query_push_and_lookup,
        test_recent_query_window_expiry,
        test_recent_query_cap,
        test_popularity_boost_zero_no_change,
        test_popularity_boost_log1p_shape,
        test_popularity_boost_negative_pop_no_inflation,
        test_query_hash_deterministic,
        test_rate_rejects_invalid_score,
        test_rate_rejects_without_attributable_query,
        test_dejavu_rate_is_registered,
        test_write_implicit_feedback_signature,
        test_refresh_feedback_cache_is_defined,
        test_schema_includes_recall_feedback,
        test_health_picks_up_new_table,
        test_feedback_cli_handlers_defined,
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
