"""v0.8.18 unit + integration tests — recall-completeness bundle.

Covers:
  - C: IPv6 identifier pattern
  - H: opaque-token threshold lowered for 8-char session_id prefixes
  - D: dejavu_global_search reorders identifier-literal hits to the
       top when the primary FTS+hybrid path returned 0
  - B: dejavu_session_export total-char cap (mode='full' safety)
  - E: dejavu_status reports Weaviate count + PG/WV drift warning
  - F: PreCompact systemMessage now describes tools instead of
       prescribing one call shape
  - A: JIT `_pg_substring_jit` literal-LIKE fallback merges hits
       when identifiers are present
  - NEW: dejavu_find_sessions tool
  - G: integration test for `_lexical_pg_substring` against real PG
       (gated — skipped when PG unreachable)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "mcp"))

# Prefer the bench config when present (real PG with content) so PG-
# dependent tests actually run instead of skipping. Falls back to a
# default empty dir otherwise — PG tests skip gracefully when PG is
# truly unreachable.
_bench_cfg = Path("/tmp/dejavu-bench-cli/config.env")
if _bench_cfg.exists():
    os.environ.setdefault(
        "CLAUDE_DEJAVU_DATA_ROOT", "/tmp/dejavu-bench-cli")
else:
    os.environ.setdefault(
        "CLAUDE_DEJAVU_DATA_ROOT", "/tmp/dejavu-v0818-test")
    os.makedirs("/tmp/dejavu-v0818-test", exist_ok=True)

import server  # type: ignore[import]
import user_prompt_submit as ups  # type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []
SKIP: list[str] = []


def _ok(msg: str) -> None:
    PASS.append(msg)
    print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, detail: str = "") -> None:
    FAIL.append((msg, detail))
    print(f"  \033[31m✗\033[0m {msg}")
    if detail:
        print(f"      {detail[:300]}")


def _skip(msg: str) -> None:
    SKIP.append(msg)
    print(f"  \033[33m⊘ SKIP\033[0m {msg}", flush=True)


def _aT(name: str, cond: bool, detail: str = "") -> None:
    _ok(name) if cond else _fail(name, detail)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _pg_reachable() -> bool:
    """1-second PG probe so PG-dependent tests can skip gracefully."""
    try:
        import psycopg2  # type: ignore
    except Exception:
        return False
    try:
        c = psycopg2.connect(server.PG_DSN, connect_timeout=1)
        c.close()
        return True
    except Exception:
        return False


# ─── C: IPv6 detection ─────────────────────────────────────────────────────

def test_ipv6_full_form() -> None:
    _step("C: IPv6 full 8-group form is detected")
    needle = server._looks_like_identifier(
        "ssh to 2001:0db8:85a3:0000:0000:8a2e:0370:7334")
    _aT("full IPv6 detected",
        needle == "2001:0db8:85a3:0000:0000:8a2e:0370:7334", repr(needle))


def test_ipv6_compressed_form() -> None:
    _step("C: IPv6 `::`-compressed form is detected")
    for prompt, expected in [
        ("ping fe80::1234", "fe80::1234"),
        ("just ::1 on localhost", "::1"),
        ("router 2001:db8::1", "2001:db8::1"),
    ]:
        needle = server._looks_like_identifier(prompt)
        _aT(f"compressed IPv6 detected for {prompt!r}",
            needle == expected, repr(needle))


def test_time_string_does_not_trigger_ipv6() -> None:
    _step("C: clock-time strings like 12:34:56 do NOT trigger IPv6")
    # 2 colons, no `::` → should not match IPv6.
    needle = server._looks_like_identifier("at 12:34:56 yesterday")
    _aT("clock time is NOT identified as IPv6",
        needle != "12:34:56", repr(needle))


# ─── H: opaque-token threshold lowered to 7 for session-prefix shape ──────

def test_8char_session_prefix_triggers_identifier() -> None:
    _step("H: 8-char session_id prefix (digits + lowercase letters) "
          "triggers the identifier path")
    # Sample resembling what dejavu_status emits as the 8-char prefix.
    out = server._looks_like_identifier("cmp1q2a8")
    _aT("8-char digit+letter token treated as identifier",
        out == "cmp1q2a8", repr(out))


def test_short_letter_only_does_not_trigger() -> None:
    _step("H: plain short words like 'filesystem' or 'cilium' DO NOT "
          "trigger the identifier path")
    for word in ["filesystem", "cilium", "bootstrap", "kubernetes"]:
        out = server._looks_like_identifier(word)
        _aT(f"{word!r} → None (not an identifier)",
            out is None, repr(out))


# ─── D: dejavu_global_search ordering ──────────────────────────────────────

def test_global_search_reorders_when_primary_empty() -> None:
    _step("D: when primary FTS/hybrid returns 'No matches', "
          "identifier-literal hits lead the response")
    orig_search = server.dejavu_search
    orig_sub = server._lexical_pg_substring

    def primary_zero(query: str, scope=None, limit=10, **kw) -> str:
        return "No matches in scope: global [via weaviate+pg]"

    def sub_hit(needle: str, limit: int, sc) -> list[dict]:
        return [{
            "session_id": "deadbeef-0000-0000-0000-000000000000",
            "turn_id": 999, "project_slug": "p1", "role": "user",
            "ts": "2026-05-21T10:00:00",
            "content": "trace 203.0.113.42 broke things",
            "gist": "ip trace", "_additional": {"score": 1.0,
                                                  "source": "pg-substring"},
        }]
    server.dejavu_search = primary_zero  # type: ignore[assignment]
    server._lexical_pg_substring = sub_hit  # type: ignore[assignment]
    try:
        out = server.dejavu_global_search(
            query="what about 203.0.113.42", limit=10)
        # The identifier-literal header should appear BEFORE any
        # "No matches" line — i.e. the substring block leads.
        idx_hits = out.find("identifier-literal hits")
        idx_nomatch = out.find("No matches in scope")
        _aT("response contains the identifier-literal block",
            idx_hits >= 0, out[:200])
        _aT("identifier-literal hits lead (No matches doesn't appear "
            "first or at all)",
            idx_nomatch == -1 or idx_hits < idx_nomatch,
            f"hits at {idx_hits}, nomatch at {idx_nomatch}")
        _aT("the IP needle is mentioned in the lead block",
            "203.0.113.42" in out, out[:300])
    finally:
        server.dejavu_search = orig_search  # type: ignore[assignment]
        server._lexical_pg_substring = orig_sub  # type: ignore[assignment]


# ─── B: dejavu_session_export full-mode total-char cap ────────────────────

def test_session_export_full_mode_total_char_cap() -> None:
    _step("B: dejavu_session_export full mode honors a total-char cap "
          "and emits a next-page hint")
    if not _pg_reachable():
        _skip("PG not reachable — full-mode cap test needs a "
              "session row")
        return
    # We need at least one session in PG to exercise the path.
    import psycopg2  # type: ignore
    c = psycopg2.connect(server.PG_DSN, connect_timeout=2)
    try:
        cur = c.cursor()
        cur.execute(
            "SELECT id::text FROM sessions "
            "WHERE total_turns IS NOT NULL "
            "  AND total_turns > 0 "
            "ORDER BY total_turns DESC LIMIT 1")
        row = cur.fetchone()
    finally:
        c.close()
    if not row:
        _skip("no sessions in PG — needs at least one to test")
        return
    sid = row[0]
    os.environ["CLAUDE_DEJAVU_SESSION_EXPORT_CHARS"] = "5000"
    try:
        out = server.dejavu_session_export(
            session_id=sid[:8], mode="full", offset=0, limit=200)
    finally:
        os.environ.pop("CLAUDE_DEJAVU_SESSION_EXPORT_CHARS", None)
    _aT("response stays within ~7 KB even with limit=200 (cap=5000 "
        "+ small overhead)",
        len(out) < 7_500, f"len={len(out)}")
    _aT("response contains a 'truncated at' or 'more turns' hint",
        ("response truncated at" in out
         or "more turns" in out
         or "end of session" in out),
        out[-400:])


# ─── E: dejavu_status reports Weaviate count line ─────────────────────────

def test_status_includes_wv_count_line() -> None:
    _step("E: dejavu_status output includes a 'turns (WV)' line")
    out = server.dejavu_status()
    _aT("status mentions 'turns (PG)'", "turns (PG):" in out,
        out[:300])
    _aT("status mentions 'turns (WV)'", "turns (WV):" in out,
        out[:600])


# ─── F: PreCompact memo is less prescriptive ──────────────────────────────

def test_precompact_memo_lists_tools_not_one_call_shape() -> None:
    _step("F: PreCompact systemMessage lists tool options without "
          "prescribing a single call shape")
    payload = json.dumps({
        "session_id": "f4358269-1234-4abc-8def-0123456789ab",
        "transcript_path": "/tmp/x",
    })
    r = subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "pre_compact.py")],
        input=payload, capture_output=True, text=True, timeout=5)
    _aT("hook exits 0", r.returncode == 0,
        f"stderr={r.stderr[:200]}")
    envelope = json.loads(r.stdout)
    msg = envelope.get("systemMessage") or ""
    _aT("memo mentions multiple recovery tools (export + search "
        "+ summary + find_sessions)",
        all(name in msg for name in (
            "dejavu_session_export",
            "dejavu_global_search",
            "dejavu_summary",
            "dejavu_find_sessions",
        )), msg[:400])
    _aT("memo does NOT prescribe a specific mode+offset+limit call shape",
        "mode='gist', offset=0, limit=50" not in msg
         and "mode='full', offset=0, limit=50" not in msg, msg[:400])


# ─── A: JIT `_pg_substring_jit` is wired and merges hits ──────────────────

def test_jit_substring_helper_is_defined() -> None:
    _step("A: JIT hook exposes `_pg_substring_jit`")
    _aT("function exists and is callable",
        callable(getattr(ups, "_pg_substring_jit", None)),
        repr(getattr(ups, "_pg_substring_jit", None)))


def test_jit_merges_substring_hits_when_identifiers_present() -> None:
    _step("A: when identifiers are present, JIT merges substring hits "
          "even if standard search returned 0")
    captured_calls: list[str] = []

    def empty_search(query: str, top_n: int, deadline_s: float):
        captured_calls.append(query)
        return []

    captured_subs: list[list[str]] = []

    def fake_sub(needles, top_n, deadline_s):
        captured_subs.append(needles)
        return [{
            "session_id": "deadbeef-0000-0000-0000-000000000000",
            "turn_id": 42, "ts": "2026-05-21T10:00:00",
            "gist": "ip trace",
            "ai_title": "fleet bootstrap",
            "score": 1.0,
            "project_slug": "p1",
        }]

    orig = ups._pg_substring_jit
    ups._pg_substring_jit = fake_sub  # type: ignore[assignment]
    try:
        out = ups.compute_recall_block(
            "trace 203.0.113.42 please",
            cfg={"JIT_RECALL_ENABLED": "true",
                 "JIT_RECALL_MIN_PROMPT_CHARS": "100",
                 "JIT_RECALL_SCORE_THRESHOLD": "0.5"},
            search_fn=empty_search,
        )
    finally:
        ups._pg_substring_jit = orig  # type: ignore[assignment]
    _aT("standard search WAS called",
        len(captured_calls) == 1, f"calls={captured_calls}")
    _aT("substring helper WAS called with the IPv4 needle",
        captured_subs and "203.0.113.42" in captured_subs[0],
        f"subs={captured_subs}")
    _aT("recall block is non-empty (substring hits merged)",
        bool(out and out.strip()), out[:200])


# ─── NEW: dejavu_find_sessions tool ───────────────────────────────────────

def test_find_sessions_tool_is_registered() -> None:
    _step("NEW: dejavu_find_sessions is importable + callable")
    _aT("server.dejavu_find_sessions exists",
        callable(getattr(server, "dejavu_find_sessions", None)),
        repr(getattr(server, "dejavu_find_sessions", None)))


def test_find_sessions_no_match_path() -> None:
    _step("NEW: dejavu_find_sessions returns 'No matches' on empty "
          "hybrid response")
    orig = server._hybrid_search
    server._hybrid_search = lambda q, n, s: []  # type: ignore[assignment]
    try:
        out = server.dejavu_find_sessions(query="nothingburger",
                                              scope="global", limit=10)
        _aT("returns a No-matches message",
            "No matches" in out, out)
    finally:
        server._hybrid_search = orig  # type: ignore[assignment]


# ─── G: integration test for _lexical_pg_substring (PG-gated) ─────────────

def test_lexical_pg_substring_integration() -> None:
    _step("G: _lexical_pg_substring round-trips against real PG "
          "(skipped when PG unreachable)")
    if not _pg_reachable():
        _skip("PG not reachable; integration test cannot run")
        return
    import psycopg2  # type: ignore
    # Pick a needle that's very likely to be present in any real
    # corpus: the literal string 'dejavu'. If even that isn't there,
    # the install is empty and the test reports zero hits — still
    # not a failure, just inconclusive.
    sc = server._scope("global")
    hits = server._lexical_pg_substring("dejavu", 5, sc)
    _aT("call returns a list (may be empty)",
        isinstance(hits, list), repr(type(hits)))
    if hits:
        # Each hit has the documented shape.
        h = hits[0]
        for k in ("turn_id", "session_id", "project_slug",
                   "content", "_additional"):
            _aT(f"hit dict has key {k!r}",
                k in h, repr(list(h.keys())))
        _aT("each hit's content actually CONTAINS the needle "
            "(case-insensitive)",
            all("dejavu" in (h.get("content") or "").lower()
                for h in hits), repr([h.get("turn_id") for h in hits]))
        _aT("source_tag is pg-substring",
            hits[0].get("_additional", {}).get("source")
              == "pg-substring",
            repr(hits[0].get("_additional")))


def main() -> int:
    print("v0.8.18 recall-completeness bundle tests")
    tests = [
        test_ipv6_full_form,
        test_ipv6_compressed_form,
        test_time_string_does_not_trigger_ipv6,
        test_8char_session_prefix_triggers_identifier,
        test_short_letter_only_does_not_trigger,
        test_global_search_reorders_when_primary_empty,
        test_session_export_full_mode_total_char_cap,
        test_status_includes_wv_count_line,
        test_precompact_memo_lists_tools_not_one_call_shape,
        test_jit_substring_helper_is_defined,
        test_jit_merges_substring_hits_when_identifiers_present,
        test_find_sessions_tool_is_registered,
        test_find_sessions_no_match_path,
        test_lexical_pg_substring_integration,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            _fail(f"{t.__name__} raised", repr(e))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}     SKIP: {len(SKIP)}")
    if FAIL:
        for n, d in FAIL:
            print(f"  ✗ {n}  {d[:120]}")
    if SKIP:
        for n in SKIP:
            print(f"  ⊘ {n}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
