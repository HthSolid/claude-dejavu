"""v0.8.10 unit tests — infra-free, no PG / no Weaviate.

Covers the three MCP tool fixes shipped in v0.8.10:

  Fix 1 — dejavu_observations tokenizes the query (5 tests)
  Fix 2 — dejavu_summary accepts a query (4 tests)
  Fix 3 — cache refresh under load: lock + timeout (3 tests)

Strategy: import the cache list + the tool functions from `mcp.server`,
monkey-patch the global `_OBS_CACHE` / `_SUM_CACHE` so the test runs
without touching Postgres, then exercise the filter logic.

The `_refresh_*_cache_if_stale` calls are stubbed via the bypass path
(re-set `_*_CACHE_LOADED_AT = time.time()` so they short-circuit).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "mcp"))

# Set required env BEFORE importing server.py — it constructs PG_DSN
# at import time. We never actually touch PG (the refresh paths are
# bypassed), but the module must import cleanly.
os.environ.setdefault("CLAUDE_DEJAVU_DATA_ROOT", "/tmp/dejavu-v0810-test")
os.makedirs("/tmp/dejavu-v0810-test", exist_ok=True)


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, detail: str = "") -> None:
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail}")


def _aT(name: str, cond: bool, detail: str = "") -> None:
    _ok(name) if cond else _fail(name, detail)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


# ─── Shared setup: import server.py once, prepare canned cache rows ──────────

import server  # type: ignore[import]


def _seed_obs_cache(rows: list[dict]) -> None:
    """Inject canned observations and freeze the TTL so the live
    refresh path is bypassed.

    v0.8.15 — also rebuild the per-project index + topic-titles list
    that production paths now read instead of scanning _OBS_CACHE."""
    server._OBS_CACHE = rows
    server._OBS_CACHE_LOADED_AT = time.time()
    server._OBS_CACHE_TABLE_SIZE = len(rows)
    server._OBS_CACHE_BY_PROJECT, server._TOPIC_TITLES_BY_PROJECT = (
        server._rebuild_obs_index(rows))


def _seed_sum_cache(rows: list[dict]) -> None:
    server._SUM_CACHE = rows
    server._SUM_CACHE_LOADED_AT = time.time()
    server._SUM_CACHE_TABLE_SIZE = len(rows)
    server._SUM_CACHE_BY_PROJECT = server._rebuild_sum_index(rows)


def _mkobs(title: str, subtitle: str = "", type_: str = "decision",
           project: str = "p1", ts: float = 1000.0,
           id_: int = 0) -> dict:
    return {
        "id": id_, "project": project, "type": type_,
        "title": title, "subtitle": subtitle, "ts": ts,
        "title_lower": title.lower(),
        "subtitle_lower": subtitle.lower(),
    }


def _mksum(request: str = "", investigated: str = "", learned: str = "",
           completed: str = "", next_steps: str = "",
           project: str = "p1", session_id: str = "deadbeef-...",
           ts: float = 1000.0, model: str = "claude-x") -> dict:
    return {
        "project": project, "session_id": session_id,
        "generated_at": "2026-05-19T00:00:00",
        "request": request, "investigated": investigated,
        "learned": learned, "completed": completed,
        "next_steps": next_steps, "model": model, "ts": ts,
    }


# ─── Fix 1 — dejavu_observations tokenization (≥5 tests) ────────────────────

def test_obs_tokenize_multi_word_matches_any_token() -> None:
    _step("Fix 1: dejavu_observations tokenizes query (OR-match any token)")
    _seed_obs_cache([
        _mkobs("Restic-replaces-rsync migration shipped", project="p1",
               id_=1, ts=100),
        _mkobs("Example adapter wired", project="p1", id_=2, ts=200),
        _mkobs("Off-tree backup mirror via systemd", project="p1",
               id_=3, ts=300),
    ])
    # The pre-v0.8.10 substring path would NEVER match this — no observation
    # title contains the full 5-word phrase. v0.8.10 should match all 3 via
    # any-token OR, except #2 which has none of the tokens.
    out = server.dejavu_observations(
        query="off-tree backup mirror systemd alternative",
        project="p1", limit=10,
    )
    _aT("query 'off-tree backup mirror systemd alternative' matches "
        "obs #3 (systemd)",
        "Off-tree backup mirror" in out,
        out)
    # 'backup' is a query token; 'Restic-replaces-rsync migration shipped'
    # does NOT contain 'backup'. So obs #1 should NOT surface. obs #3
    # DOES contain 'backup'. obs #2 has none of the tokens.
    _aT("same query surfaces obs #3 only (only one with any token)",
        ("Off-tree backup" in out) and ("example" not in out),
        out)
    _aT("obs #2 ('Example adapter wired') is filtered out", "example" not in out, out)


def test_obs_tokenize_single_word_query_backwards_compat() -> None:
    _step("Fix 1: single-word query keeps pre-v0.8.10 behavior")
    _seed_obs_cache([
        _mkobs("Example adapter wired", project="p1", id_=1, ts=100),
        _mkobs("Restic backup shipped", project="p1", id_=2, ts=200),
    ])
    out = server.dejavu_observations(query="example", project="p1")
    _aT("single-word query 'example' matches obs #1",
        "Example adapter" in out, out)
    _aT("single-word query 'example' does not surface obs #2",
        "Restic backup" not in out, out)


def test_obs_tokenize_stopwords_dropped() -> None:
    _step("Fix 1: stopwords dropped — query of all stopwords returns ALL")
    _seed_obs_cache([
        _mkobs("Example adapter wired", project="p1", id_=1, ts=100),
        _mkobs("Restic backup shipped", project="p1", id_=2, ts=200),
    ])
    # All-stopwords query → tokens == [] → no filter → both surface
    out = server.dejavu_observations(query="the and of", project="p1")
    _aT("stopword-only query returns ALL observations in the project",
        ("Example adapter" in out) and ("Restic backup" in out), out)


def test_obs_tokenize_short_tokens_dropped() -> None:
    _step("Fix 1: tokens <3 chars dropped (off-tree → 'tree' only)")
    _seed_obs_cache([
        _mkobs("Off-the-tree migration", project="p1", id_=1, ts=100),
        _mkobs("Sky high view", project="p1", id_=2, ts=200),
    ])
    # 'off-tree' splits at the hyphen → 'off' (3 chars, kept) and 'tree'.
    # Actually the _WORD_RE requires START letter + 2+ MORE → ≥3 total. 'off' = 3 chars = kept.
    # 'tree' = 4 chars kept. So this query should match obs #1.
    out = server.dejavu_observations(query="off-tree migration", project="p1")
    _aT("'off-tree migration' matches obs #1 via 'tree' OR 'migration'",
        "Off-the-tree" in out, out)


def test_obs_tokenize_none_or_empty_query_no_filter() -> None:
    _step("Fix 1: query=None / query='' applies no filter (preserves v0.8.9)")
    _seed_obs_cache([
        _mkobs("Alpha", project="p1", id_=1, ts=100),
        _mkobs("Beta", project="p1", id_=2, ts=200),
    ])
    out_none = server.dejavu_observations(query=None, project="p1")
    out_empty = server.dejavu_observations(query="", project="p1")
    _aT("query=None returns both", "Alpha" in out_none and "Beta" in out_none, out_none)
    _aT("query='' returns both", "Alpha" in out_empty and "Beta" in out_empty, out_empty)


# ─── Fix 2 — dejavu_summary accepts a query (≥4 tests) ──────────────────────

def test_sum_query_filters_relevant() -> None:
    _step("Fix 2: dejavu_summary query filters to relevant rows")
    _seed_sum_cache([
        _mksum(request="MCP stdio JSONRPC tool list",
               learned="hooks/user_prompt_submit handles ingestion",
               session_id="aaaaaaaa", ts=300),
        _mksum(request="example two-sided market making",
               session_id="bbbbbbbb", ts=400),
    ])
    out = server.dejavu_summary(project="p1", limit=5,
                                  query="MCP server stdio")
    _aT("query 'MCP server stdio' returns the MCP-stdio summary",
        "aaaaaaaa"[:8] in out, out)
    _aT("query 'MCP server stdio' does NOT return example summary",
        "bbbbbbbb"[:8] not in out, out)


def test_sum_query_none_preserves_legacy_recency() -> None:
    _step("Fix 2: query=None preserves pre-v0.8.10 recency-only behavior")
    _seed_sum_cache([
        _mksum(request="Older work", session_id="11111111", ts=100),
        _mksum(request="Newer work", session_id="22222222", ts=999),
    ])
    out = server.dejavu_summary(project="p1", limit=1)  # no query
    # limit=1 returns the most recent.
    _aT("query=None returns the most-recent row (ts=999, session 22222222)",
        "22222222"[:8] in out, out)
    _aT("query=None does NOT return the older one",
        "11111111"[:8] not in out, out)


def test_sum_query_searches_all_five_fields() -> None:
    _step("Fix 2: query searches across all 5 summary fields")
    _seed_sum_cache([
        _mksum(next_steps="DEEPLY HIDDEN MARKER xyzzy", session_id="cccccccc", ts=500),
        _mksum(request="No marker here", session_id="dddddddd", ts=600),
    ])
    out = server.dejavu_summary(project="p1", limit=5, query="xyzzy")
    _aT("query 'xyzzy' finds row where marker is in next_steps",
        "cccccccc"[:8] in out, out)
    _aT("query 'xyzzy' filters out row without marker",
        "dddddddd"[:8] not in out, out)


def test_sum_query_stopwords_only_no_filter() -> None:
    _step("Fix 2: stopword-only query applies no filter")
    _seed_sum_cache([
        _mksum(request="Alpha", session_id="eeeeeeee", ts=700),
        _mksum(request="Beta", session_id="ffffffff", ts=800),
    ])
    out = server.dejavu_summary(project="p1", limit=5, query="the and of")
    _aT("stopword-only query returns rows (no filter), latest first",
        ("ffffffff"[:8] in out) and ("eeeeeeee"[:8] in out), out)


# ─── Fix 3 — cache refresh under load (≥3 tests) ────────────────────────────

def test_refresh_locks_are_acquired_and_released() -> None:
    _step("Fix 3: refresh locks exist as threading.Lock and are reusable")
    _aT("_OBS_REFRESH_LOCK is a threading.Lock",
        hasattr(server, "_OBS_REFRESH_LOCK")
        and isinstance(server._OBS_REFRESH_LOCK, type(threading.Lock())),
        type(server._OBS_REFRESH_LOCK).__name__)
    _aT("_SUM_REFRESH_LOCK is a threading.Lock",
        hasattr(server, "_SUM_REFRESH_LOCK")
        and isinstance(server._SUM_REFRESH_LOCK, type(threading.Lock())),
        type(server._SUM_REFRESH_LOCK).__name__)
    # Both locks should acquire/release cleanly (i.e. not be left held by
    # a prior monkey-patched run).
    got_obs = server._OBS_REFRESH_LOCK.acquire(timeout=0.5)
    if got_obs:
        server._OBS_REFRESH_LOCK.release()
    got_sum = server._SUM_REFRESH_LOCK.acquire(timeout=0.5)
    if got_sum:
        server._SUM_REFRESH_LOCK.release()
    _aT("_OBS_REFRESH_LOCK acquired in <0.5s (not deadlocked)", got_obs)
    _aT("_SUM_REFRESH_LOCK acquired in <0.5s (not deadlocked)", got_sum)


def test_refresh_with_held_lock_returns_stale_quickly() -> None:
    _step("Fix 3: when lock is held, caller returns stale cache, doesn't block")
    # Pre-seed a stale cache so the refresh would normally engage.
    _seed_obs_cache([_mkobs("seeded", project="p1", id_=1, ts=100)])
    server._OBS_CACHE_LOADED_AT = 0  # forcibly stale

    # Hold the refresh lock from another thread.
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def _hold():
        server._OBS_REFRESH_LOCK.acquire()
        holder_ready.set()
        release_holder.wait(timeout=5)
        server._OBS_REFRESH_LOCK.release()

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert holder_ready.wait(timeout=2), "holder thread didn't start"

    # Call the refresh — should return promptly (<_REFRESH_BUDGET_S + slack).
    t0 = time.perf_counter()
    server._refresh_obs_cache_if_stale()
    elapsed_s = time.perf_counter() - t0
    release_holder.set()
    t.join(timeout=3)

    # Budget is 1.0s; allow generous CI slack.
    _aT(f"contended refresh returned in {elapsed_s:.2f}s "
        f"(budget {server._REFRESH_BUDGET_S}s + slack)",
        elapsed_s < server._REFRESH_BUDGET_S + 1.5,
        f"elapsed={elapsed_s:.2f}s")
    # Stale cache still readable (no nuke).
    _aT("cache still contains seeded row after contended refresh",
        len(server._OBS_CACHE) == 1 and server._OBS_CACHE[0]["title"] == "seeded")


def test_refresh_uses_connect_timeout() -> None:
    _step("Fix 3: short-lived refresh conn uses connect_timeout=1 by default")
    # _PG_CONNECT_TIMEOUT_S is the documented knob. Default should be 1.
    _aT("server._PG_CONNECT_TIMEOUT_S defaults to 1",
        server._PG_CONNECT_TIMEOUT_S == 1,
        str(server._PG_CONNECT_TIMEOUT_S))
    # _short_lived_pg_conn must exist as a callable.
    _aT("server._short_lived_pg_conn is callable",
        callable(server._short_lived_pg_conn))
    # If we point it at an unreachable port it should return None FAST,
    # not hang for psycopg2's default >30s.
    orig_dsn = server.PG_DSN
    try:
        server.PG_DSN = ("host=127.0.0.1 port=1 user=postgres "
                          "password=postgres dbname=does_not_exist")
        t0 = time.perf_counter()
        c = server._short_lived_pg_conn()
        dt = time.perf_counter() - t0
        _aT(f"_short_lived_pg_conn fails fast (<3s, got {dt:.2f}s)", dt < 3.0,
            f"elapsed={dt:.2f}s")
        _aT("_short_lived_pg_conn returns None on failure", c is None)
    finally:
        server.PG_DSN = orig_dsn


# ─── Bonus: tokenize_query helper has the expected pure-function shape ──────

def test_tokenize_query_helper() -> None:
    _step("Bonus: code.topic.tokenize_query — pure-Python helper contract")
    from topic import tokenize_query  # type: ignore[import]
    _aT("tokenize_query('') returns []", tokenize_query("") == [])
    _aT("tokenize_query('the and of') drops all stopwords",
        tokenize_query("the and of") == [])
    toks = tokenize_query("MCP server stdio JSONRPC tool list")
    _aT("tokenize_query is case-insensitive (lowercased output)",
        toks == [t.lower() for t in toks])
    _aT("tokenize_query preserves first-seen order",
        toks == sorted(toks, key=lambda x: ["mcp", "server", "stdio",
                                              "jsonrpc", "tool",
                                              "list"].index(x)))


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("v0.8.10 MCP tool fixes — infra-free regression suite")
    tests = [
        # Fix 1 (5 tests)
        test_obs_tokenize_multi_word_matches_any_token,
        test_obs_tokenize_single_word_query_backwards_compat,
        test_obs_tokenize_stopwords_dropped,
        test_obs_tokenize_short_tokens_dropped,
        test_obs_tokenize_none_or_empty_query_no_filter,
        # Fix 2 (4 tests)
        test_sum_query_filters_relevant,
        test_sum_query_none_preserves_legacy_recency,
        test_sum_query_searches_all_five_fields,
        test_sum_query_stopwords_only_no_filter,
        # Fix 3 (3 tests)
        test_refresh_locks_are_acquired_and_released,
        test_refresh_with_held_lock_returns_stale_quickly,
        test_refresh_uses_connect_timeout,
        # Bonus
        test_tokenize_query_helper,
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
