#!/usr/bin/env python3
"""
v0.8.11 one-time restoration script for the bench corpus.

What it does (atomic in one PG transaction):

  1. Wipes existing rows in `observations` for every session whose
     `project_slug = 'claude-mem-observer-sessions'`. These were
     59 domain-registrar-only rows produced by an earlier ingest that
     dominated the corpus and capped recall at 2/8 on the bench.

  2. Re-runs the deterministic `code/observations.extract` regex over
     every assistant `turns.content` row for those same sessions. The
     source turns DO contain `<observation>` / `<summary>` tags (verified
     during v0.8.11 investigation — 45 tag-bearing assistant turns across
     9 sessions). Re-extracting them yields ~62 observations spanning
     domain pricing AND the migration/MCP/PostgreSQL content the bench
     was designed around.

  3. Renames `sessions.project_slug` from `claude-mem-observer-sessions`
     to `claude-mem-observer-sessions-bench`. The `-bench` suffix pins
     the corpus as a fixture: real CWD-derived ingest paths cannot land
     here because no real directory ends in `*-bench`. The bench harness
     `OBS_PROJECT` constant tracks the new name.

Strict honesty constraints (no goalpost-moving):

  - No engineered/synthetic observation rows. The extractor sees only
    the same turn content that was already in PG before this script ran.
  - The bench's 8 queries and their marker words are unchanged.
  - The new recall number is whatever falls out of the re-extracted
    corpus + bench harness — paste it verbatim, don't tune.

Idempotency:

  - Re-running after a successful migration is a no-op because the
    old slug no longer exists. The script prints "ABORT" and exits.

Usage:

    python3 tests/_restore_bench_corpus_0811.py
"""
from __future__ import annotations

import os
import sys

# Make the in-tree code/ importable for the extractor.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "code"))

import psycopg2  # noqa: E402

from observations import extract  # noqa: E402

OLD_SLUG = "claude-mem-observer-sessions"
NEW_SLUG = "claude-mem-observer-sessions-bench"


def main() -> int:
    conn = psycopg2.connect(
        host="localhost", port=5450, user="postgres",
        password="postgres", dbname="claude_audit",
    )
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("SELECT count(*) FROM sessions WHERE project_slug=%s", (OLD_SLUG,))
    n_old = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM sessions WHERE project_slug=%s", (NEW_SLUG,))
    n_new = cur.fetchone()[0]
    print(f"Before: {OLD_SLUG} sessions={n_old}, {NEW_SLUG} sessions={n_new}")
    if n_old == 0:
        print(f"ABORT: no sessions under {OLD_SLUG} — already migrated or empty.")
        return 0
    if n_new > 0:
        print(f"ABORT: {NEW_SLUG} already has sessions — would collide.")
        return 1

    cur.execute(
        """
        DELETE FROM observations
        WHERE session_id IN (SELECT id FROM sessions WHERE project_slug=%s)
        """,
        (OLD_SLUG,),
    )
    n_deleted = cur.rowcount
    print(f"Deleted {n_deleted} stale observations.")

    cur.execute(
        """
        SELECT t.id, t.session_id::text, t.content
        FROM turns t JOIN sessions s ON s.id=t.session_id
        WHERE s.project_slug=%s AND t.role='assistant'
        """,
        (OLD_SLUG,),
    )
    rows = cur.fetchall()
    inserted = 0
    sample: list[str] = []
    for tid, sid, content in rows:
        for obs in extract(content or ""):
            cur.execute(
                """
                INSERT INTO observations (session_id, turn_id, type, title, subtitle, facts, files_touched, ts)
                VALUES (%s::uuid, %s, %s::observation_type, %s, %s, %s, %s, now())
                RETURNING id
                """,
                (
                    sid, tid, obs["type"], obs["title"], obs.get("subtitle"),
                    obs.get("facts"), obs.get("files_touched"),
                ),
            )
            inserted += 1
            if len(sample) < 5:
                sample.append(obs["title"])
    print(f"Inserted {inserted} re-extracted observations.")
    print("Sample titles:")
    for t in sample:
        print(f"  - {t[:110]}")

    cur.execute(
        "UPDATE sessions SET project_slug=%s WHERE project_slug=%s",
        (NEW_SLUG, OLD_SLUG),
    )
    n_renamed = cur.rowcount
    print(f"Renamed {n_renamed} sessions: {OLD_SLUG} -> {NEW_SLUG}.")

    cur.execute(
        """
        SELECT count(*) FROM observations o
        JOIN sessions s ON s.id=o.session_id
        WHERE s.project_slug=%s
        """,
        (NEW_SLUG,),
    )
    final = cur.fetchone()[0]
    print(f"Final observation count under {NEW_SLUG}: {final}")

    conn.commit()
    conn.close()
    print("COMMITTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
