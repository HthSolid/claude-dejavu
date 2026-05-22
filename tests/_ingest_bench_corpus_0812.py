#!/usr/bin/env python3
"""
v0.8.12 — re-ingest the FULL 745-session source corpus into the bench fixture.

The v0.8.11 fixture pinned the bench slug to ``claude-mem-observer-sessions-bench``
but the underlying corpus was only 9 sessions / 239 turns / 62 observations —
about 0.25% of the available source content. claude-mem ingested the same source
and surfaced 8,103 observations from the same regex extractor over the same
turns. v0.8.12 closes that density gap honestly: re-ingest every source ``.jsonl``
the v0.5 bench was measured against.

What this script does (in order):

  1. Idempotency guard. Aborts if ``observations`` already exceeds 500 rows
     under ``project_slug='claude-mem-observer-sessions-bench'``. A successful
     v0.8.12 run takes the count from 62 to several thousand; a second run
     would do nothing useful.

  2. Writes ``/tmp/dejavu-bench-cli/config.env`` with the bench-pinned
     Weaviate class, the cloud embed credentials inherited from the live
     install, and PG=claude_audit.

  3. Sets ``CLAUDE_DEJAVU_DATA_ROOT`` + ``CLAUDE_DEJAVU_WV_CLASS`` env vars
     BEFORE importing the in-tree ``code/ingester`` module. v0.8.12 ships
     a small ingester patch so ``WEAVIATE_CLASS`` honors that env var
     (parity with the existing ``code/recall.py`` behaviour). Without
     that, the ingester would write turns into the live
     ``ClaudeDejavuTurn`` class — exactly what the bench fixture is meant
     to prevent.

  4. Walks ``~/.claude/projects/<your-claude-projects-encoded-path>/``,
     calling ``ingest_jsonl`` for every ``*.jsonl`` (745 files at writing
     time). Each call upserts the session row, streams turns into PG,
     and batch-upserts vectors into ``ClaudeDejavuTurn_bench`` via the
     dejavu-cloud worker (1024-dim).

  5. After ingest finishes, in a single atomic transaction:
       (a) Rename ``sessions.project_slug`` from
           ``claude-mem-observer-sessions`` →
           ``claude-mem-observer-sessions-bench`` for every newly
           ingested session.
       (b) Walk every assistant ``turns`` row under the bench slug and run
           the deterministic ``code/observations.extract`` regex, inserting
           every match into ``observations``. This is the same code path
           the v0.8.11 fixture used — unchanged regex, unchanged inputs.

Constraints (no goalpost-moving):

  - No engineered observations. The extractor sees only canonical source
    content from ``~/.claude/projects/...``.
  - Bench markers + queries in ``tests/bench_vs_claude_mem.py`` are
    UNCHANGED. Only corpus DENSITY changes.
  - Live Weaviate ``ClaudeDejavuTurn`` class is UNTOUCHED — every vector
    lands in the new ``ClaudeDejavuTurn_bench`` class.
  - Live PG sessions outside the bench slug are untouched.

Honest caveat about embedding dim:

  v0.5 bench numbers came from the local text2vec-transformers stack
  (384-dim). v0.8.12 bench uses the dejavu-cloud worker (1024-dim).
  Different embedding spaces produce different nearest-neighbour
  rankings, so vector-recall numbers across these stacks aren't
  directly comparable. ``dejavu_observations`` and ``dejavu_summary``
  don't depend on Weaviate at all and ARE comparable.

Usage:

    python3 tests/_ingest_bench_corpus_0812.py

Optional flags:

    --pg-only         Skip the Weaviate vector upserts (PG only).
                      Useful if the cloud embed worker is down — the
                      observations + summary recall paths still work
                      because they're PG-native.
    --max-sessions N  Stop after N sessions (smoke test).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# ─── Constants ───────────────────────────────────────────────────────────────
SOURCE_DIR = Path.home() / ".claude" / "projects" / "<your-claude-projects-encoded-path>"
BENCH_DATA_ROOT = Path("/tmp/dejavu-bench-cli")
BENCH_WV_CLASS = "ClaudeDejavuTurn_bench"
OLD_SLUG = "claude-mem-observer-sessions"
NEW_SLUG = "claude-mem-observer-sessions-bench"
PG_DB = "claude_audit"
PG_PORT = "5450"
WV_URL = "http://localhost:8889"

# Live config supplies cloud embed creds. We read it but never write to it.
LIVE_CONFIG = Path.home() / ".local" / "share" / "claude-dejavu" / "config.env"


def _load_live_cloud_creds() -> dict[str, str]:
    """Read DEJAVU_EMBED_MODE / DEJAVU_CLOUD_URL / DEJAVU_CLOUD_KEY from the
    live install config. The bench has no creds of its own — it reuses the
    user's existing cloud account."""
    out: dict[str, str] = {}
    if not LIVE_CONFIG.exists():
        return out
    for line in LIVE_CONFIG.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip(); v = v.strip()
        if k in ("DEJAVU_EMBED_MODE", "DEJAVU_CLOUD_URL", "DEJAVU_CLOUD_KEY"):
            out[k] = v
    return out


def _write_bench_config(pg_only: bool) -> None:
    BENCH_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    cfg = [
        "# bench fixture config — written by tests/_ingest_bench_corpus_0812.py",
        "PG_HOST=localhost",
        f"PG_PORT={PG_PORT}",
        "PG_USER=postgres",
        "PG_PASS=postgres",
        f"PG_DB={PG_DB}",
        f"WV_URL={WV_URL}",
        f"WV_CLASS={BENCH_WV_CLASS}",
        f"DATA_ROOT={BENCH_DATA_ROOT}",
        "OS_USER=bench",
        "SHARED=0",
    ]
    if not pg_only:
        creds = _load_live_cloud_creds()
        if creds.get("DEJAVU_EMBED_MODE") == "cloud":
            cfg.append("DEJAVU_EMBED_MODE=cloud")
            if creds.get("DEJAVU_CLOUD_URL"):
                cfg.append(f"DEJAVU_CLOUD_URL={creds['DEJAVU_CLOUD_URL']}")
            if creds.get("DEJAVU_CLOUD_KEY"):
                cfg.append(f"DEJAVU_CLOUD_KEY={creds['DEJAVU_CLOUD_KEY']}")
    (BENCH_DATA_ROOT / "config.env").write_text("\n".join(cfg) + "\n")


def _idempotency_check() -> int:
    """Return current observation count under the bench slug. Caller decides
    whether to abort. Returning instead of aborting keeps the function pure
    for tests."""
    import psycopg2
    conn = psycopg2.connect(
        host="localhost", port=int(PG_PORT), user="postgres",
        password="postgres", dbname=PG_DB,
    )
    cur = conn.cursor()
    cur.execute(
        """
        SELECT count(*) FROM observations o
        JOIN sessions s ON s.id = o.session_id
        WHERE s.project_slug = %s
        """,
        (NEW_SLUG,),
    )
    n = cur.fetchone()[0]
    conn.close()
    return int(n)


def _rename_slug_and_extract_observations() -> tuple[int, int]:
    """Single atomic transaction. Returns (n_renamed, n_observations)."""
    import psycopg2
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
    from observations import extract  # noqa: E402

    conn = psycopg2.connect(
        host="localhost", port=int(PG_PORT), user="postgres",
        password="postgres", dbname=PG_DB,
    )
    conn.autocommit = False
    cur = conn.cursor()

    # v0.8.15 — PG advisory lock keyed on this script so a second
    # invocation that races into the DELETE/INSERT loop cannot
    # corrupt the bench fixture mid-rebuild. Released automatically
    # when the transaction commits or rolls back.
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext("
        "'dejavu-bench-ingest-0812'))")

    # 1. Rename slug for every fresh ingest (OLD_SLUG → NEW_SLUG).
    cur.execute(
        "UPDATE sessions SET project_slug=%s WHERE project_slug=%s",
        (NEW_SLUG, OLD_SLUG),
    )
    n_renamed = cur.rowcount

    # 2. Run the regex extractor over every assistant turn under the bench
    #    slug. We DELETE any existing observations under the bench slug
    #    first so the re-extract is deterministic — anyone re-running this
    #    script after a partial PG state gets the same final answer.
    cur.execute(
        """
        DELETE FROM observations
        WHERE session_id IN (SELECT id FROM sessions WHERE project_slug=%s)
        """,
        (NEW_SLUG,),
    )

    # Use a SECOND cursor for the SELECT — the INSERT loop below reuses the
    # first cursor, which would clobber a SELECT cursor's row state on each
    # INSERT (psycopg2 raises "no results to fetch"). Two cursors share the
    # same transaction so the rename + delete + inserts are still atomic.
    sel = conn.cursor(name="bench_obs_select")  # server-side, streams rows
    sel.itersize = 1000
    sel.execute(
        """
        SELECT t.id, t.session_id::text, t.content
        FROM turns t JOIN sessions s ON s.id = t.session_id
        WHERE s.project_slug = %s AND t.role = 'assistant'
        """,
        (NEW_SLUG,),
    )
    n_obs = 0
    for tid, sid, content in sel:
        if not content:
            continue
        for obs in extract(content):
            cur.execute(
                """
                INSERT INTO observations
                  (session_id, turn_id, type, title, subtitle, facts, files_touched, ts)
                VALUES (%s::uuid, %s, %s::observation_type, %s, %s, %s, %s, now())
                """,
                (
                    sid, tid, obs["type"], obs["title"], obs.get("subtitle"),
                    obs.get("facts"), obs.get("files_touched"),
                ),
            )
            n_obs += 1
    sel.close()
    conn.commit()
    conn.close()
    return n_renamed, n_obs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pg-only", action="store_true",
                     help="Skip Weaviate vector upserts (PG ingest only).")
    ap.add_argument("--max-sessions", type=int, default=0,
                     help="Stop after N sessions (default: all).")
    args = ap.parse_args()

    if not SOURCE_DIR.is_dir():
        print(f"ABORT: source dir not found: {SOURCE_DIR}", file=sys.stderr)
        return 2

    existing_obs = _idempotency_check()
    if existing_obs > 500:
        print(f"ABORT: observations under {NEW_SLUG} = {existing_obs} (> 500) — "
              f"already populated. Refusing to re-run.")
        return 0
    print(f"Pre-flight: observations under {NEW_SLUG} = {existing_obs}")

    _write_bench_config(pg_only=args.pg_only)

    # CRITICAL: set env BEFORE importing ingester (module-level config reads).
    os.environ["CLAUDE_DEJAVU_DATA_ROOT"] = str(BENCH_DATA_ROOT)
    os.environ["CLAUDE_DEJAVU_WV_CLASS"] = BENCH_WV_CLASS
    if args.pg_only:
        os.environ["DEJAVU_EMBED_MODE"] = "local"  # disables cloud calls

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
    import importlib
    # If a prior import is cached (e.g. from tests), reload so module-level
    # constants pick up our env.
    if "ingester" in sys.modules:
        importlib.reload(sys.modules["ingester"])
    import ingester  # noqa: E402

    # Sanity check: the in-tree patch landed.
    assert ingester.WEAVIATE_CLASS == BENCH_WV_CLASS, (
        f"WEAVIATE_CLASS={ingester.WEAVIATE_CLASS!r} "
        f"!= {BENCH_WV_CLASS!r} — v0.8.12 ingester patch missing")

    import psycopg2  # noqa: E402
    conn = psycopg2.connect(
        host="localhost", port=int(PG_PORT), user="postgres",
        password="postgres", dbname=PG_DB,
    )

    files = sorted(p for p in SOURCE_DIR.iterdir() if p.name.endswith(".jsonl"))
    if args.max_sessions:
        files = files[:args.max_sessions]
    print(f"Found {len(files)} .jsonl files under {SOURCE_DIR}")
    print(f"  ingester.WEAVIATE_CLASS = {ingester.WEAVIATE_CLASS!r}")
    print(f"  ingester._DEJAVU_EMBED_MODE = {ingester._DEJAVU_EMBED_MODE!r}")
    print()

    proj_enc = SOURCE_DIR.name  # <your-claude-projects-encoded-path>
    started = time.time()
    n_ok = n_skip = n_err = 0
    total_turns = total_vecs = 0
    for i, f in enumerate(files, 1):
        sid = f.stem
        try:
            res = ingester.ingest_jsonl(str(f), sid, proj_enc, conn)
        except Exception as e:
            n_err += 1
            print(f"  [{i}/{len(files)}] {sid[:8]} ERROR: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            try:
                conn.rollback()
            except Exception:
                pass
            continue
        if res is None or res == 0:
            n_skip += 1
            continue
        turns, vecs, _last_line = res
        total_turns += turns
        total_vecs += vecs
        n_ok += 1
        if i % 25 == 0 or i == len(files):
            elapsed = time.time() - started
            rate = i / elapsed if elapsed else 0
            print(f"  [{i}/{len(files)}] ok={n_ok} skip={n_skip} err={n_err} "
                  f"turns={total_turns} vec={total_vecs} "
                  f"elapsed={elapsed:.0f}s ({rate:.1f} sess/s)")
    print()
    print(f"Ingest done: {n_ok} sessions ok, {n_skip} skipped, {n_err} errors. "
          f"Total: {total_turns} turns, {total_vecs} vectors.")

    # For sessions whose ingest_cursors row was already at EOF (the 9 sessions
    # the v0.8.11 fixture already ingested), the line-loop above is a no-op
    # and no vectors get uploaded. Sweep them via retry_vectorize, scoped to
    # the bench slugs only.
    if not args.pg_only:
        print()
        print("Retry-vectorize pass over remaining bench turns (cloud embed) …")
        _retry_vectorize_bench(conn, ingester)

    conn.close()

    print()
    print("Renaming slug + extracting observations …")
    n_renamed, n_obs = _rename_slug_and_extract_observations()
    print(f"  renamed {n_renamed} sessions: {OLD_SLUG} → {NEW_SLUG}")
    print(f"  extracted {n_obs} observations")
    print()
    print("DONE")
    return 0


def _retry_vectorize_bench(conn, ingester) -> None:
    """Mini-clone of ingester.cmd_retry_vectorize, narrowed to bench slugs.
    Calling cmd_retry_vectorize directly would also re-vectorize the live
    workspace; we MUST stay isolated to the bench slug + the not-yet-renamed
    OLD_SLUG (renaming happens after this pass)."""
    import psycopg2.extras
    CONTENT_VECTORIZE_MIN = ingester.CONTENT_VECTORIZE_MIN
    CONTENT_VECTORIZE_MAX = ingester.CONTENT_VECTORIZE_MAX
    BATCH = 64

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.session_id::text, t.idx, s.project_slug, t.role,
                   t.content, t.ts, t.model, s.ai_title
            FROM turns t JOIN sessions s ON s.id = t.session_id
            WHERE NOT t.vectorized
              AND s.project_slug IN (%s, %s)
              AND t.role IN ('user','assistant')
              AND t.content_type = 'text'
              AND length(t.content) >= %s
            ORDER BY t.id
            """,
            (OLD_SLUG, NEW_SLUG, CONTENT_VECTORIZE_MIN),
        )
        rows = cur.fetchall()
    print(f"  {len(rows)} turn(s) eligible for vectorization")
    if not rows:
        return
    started = time.time()
    items: list[tuple[int, dict]] = []
    succeeded = 0

    def flush() -> None:
        nonlocal succeeded
        if not items:
            return
        ok = ingester.weaviate_batch_upsert(items)
        if ok:
            with conn.cursor() as cur2:
                psycopg2.extras.execute_values(
                    cur2,
                    "UPDATE turns AS t SET vectorized=TRUE, "
                    "weaviate_id=v.wid::uuid FROM (VALUES %s) AS v(tid, wid) "
                    "WHERE t.id = v.tid",
                    [(tid, wid) for tid, wid in ok.items()],
                )
            conn.commit()
            succeeded += len(ok)
        items.clear()

    for tid, sid, idx, slug, role, content, ts, model, ai_title in rows:
        if not ingester._gate_ok(content or ""):
            continue
        items.append((tid, {
            "session_id": sid,
            "turn_id": idx,
            "project_slug": slug or "",
            "role": role,
            "content": (content or "")[:CONTENT_VECTORIZE_MAX],
            "ts": ts.isoformat() if ts else None,
            "model": model or "",
            "ai_title": ai_title or "",
        }))
        if len(items) >= BATCH:
            flush()
            if succeeded % 500 == 0:
                elapsed = time.time() - started
                rate = succeeded / max(elapsed, 0.001)
                print(f"  vectorized {succeeded}/{len(rows)} "
                      f"({rate:.0f}/s, {elapsed:.0f}s elapsed)")
    flush()
    elapsed = time.time() - started
    print(f"  retry-vectorize done: {succeeded}/{len(rows)} in {elapsed:.0f}s")


if __name__ == "__main__":
    raise SystemExit(main())
