"""Verifies migration 0001 creates the expected tables/indexes
and is idempotent. Runs against a real Postgres (skipped if
psycopg2/PG unavailable)."""
from __future__ import annotations
import os, sys, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS, FAIL = [], []
def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""): FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}"); d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


def _pg_or_skip():
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        print("SKIP: psycopg2 not installed"); sys.exit(0)
    dsn = os.environ.get("DEJAVU_TEST_PG_DSN")
    if not dsn:
        print("SKIP: set DEJAVU_TEST_PG_DSN=postgres://... to run"); sys.exit(0)
    return dsn


def test_migration_creates_tables_and_indexes() -> None:
    _step("0001 creates repos + doc_file_state + renames workspaces.projects")
    dsn = _pg_or_skip()
    import psycopg2  # safe — _pg_or_skip would have exited if missing
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        # Reset to base schema before applying migration.
        cur.execute("DROP TABLE IF EXISTS doc_file_state, repos CASCADE")
        cur.execute("DROP TABLE IF EXISTS workspaces CASCADE")
        with open(ROOT / "code" / "schema.sql") as f:
            cur.execute(f.read())
        # Pretend we're at pre-0001 state: rename back.
        cur.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='workspaces' AND column_name='repo_ids') THEN "
            "ALTER TABLE workspaces RENAME COLUMN repo_ids TO projects; END IF; END $$;"
        )
        # Apply migration.
        with open(ROOT / "code" / "migrations" / "0001_repos_and_workspace_repo_ids.sql") as f:
            cur.execute(f.read())
        conn.commit()

        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        tables = {r[0] for r in cur.fetchall()}
        _aT("repos table present", "repos" in tables)
        _aT("doc_file_state table present", "doc_file_state" in tables)

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='workspaces' AND column_name='repo_ids'"
        )
        _aT("workspaces.repo_ids column exists", cur.fetchone() is not None)

        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='workspaces'"
        )
        idx_names = {r[0] for r in cur.fetchall()}
        _aT("new idx_workspaces_repo_ids index present",
             "idx_workspaces_repo_ids" in idx_names)
        _aT("old idx_workspaces_projects gone",
             "idx_workspaces_projects" not in idx_names)


def test_migration_is_idempotent() -> None:
    _step("re-applying 0001 doesn't fail")
    dsn = _pg_or_skip()
    import psycopg2  # safe — _pg_or_skip would have exited if missing
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        with open(ROOT / "code" / "migrations" / "0001_repos_and_workspace_repo_ids.sql") as f:
            sql = f.read()
        cur.execute(sql)  # second apply
        conn.commit()
        _aT("idempotent second run", True)


def main() -> int:
    tests = [test_migration_creates_tables_and_indexes,
              test_migration_is_idempotent]
    for t in tests:
        try: t()
        except Exception as e:
            import traceback; traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print(f"\n{'='*60}\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
