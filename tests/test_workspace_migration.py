"""workspace migrate: slug → repo_id mapping by label, orphans surfaced."""
from __future__ import annotations
import os, sys, tempfile, uuid
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
        print("SKIP: DEJAVU_TEST_PG_DSN unset"); sys.exit(0)
    return dsn


def test_migrate_maps_slugs_to_repo_ids_by_label():
    _step("workspace migrate: slug X → repos.id where label == X")
    dsn = _pg_or_skip()
    import psycopg2
    # Import the migration impl directly.
    from recall import _workspace_migrate_impl
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        # Ensure tables exist (schema.sql is the source of truth).
        with open(ROOT / "code" / "schema.sql") as f:
            cur.execute(f.read())
        # Seed: clear + insert.
        cur.execute("DELETE FROM workspaces")
        cur.execute("DELETE FROM repos")
        a_id, g_id = str(uuid.uuid4()), str(uuid.uuid4())
        cur.execute("INSERT INTO repos (id, label, last_seen_path) "
                     "VALUES (%s, %s, %s)",
                     (a_id, "alpha", "/tmp/alpha"))
        cur.execute("INSERT INTO repos (id, label, last_seen_path) "
                     "VALUES (%s, %s, %s)",
                     (g_id, "gamma", "/tmp/gamma"))
        cur.execute("INSERT INTO workspaces (name, repo_ids) "
                     "VALUES (%s, %s)",
                     ("ws1", ["alpha", "beta"]))
        conn.commit()

        report = _workspace_migrate_impl(conn)
        cur.execute("SELECT repo_ids FROM workspaces WHERE name='ws1'")
        result = cur.fetchone()[0]
        _aT("alpha mapped to its uuid", a_id in result, str(result))
        _aT("beta surfaced as orphan",
             "beta" in report.get("orphans", []), str(report))
        _aT("gamma untouched (not in workspace)",
             g_id not in result)


def test_migrate_skips_already_uuid_entries():
    _step("workspace migrate: existing UUID entries pass through unchanged")
    dsn = _pg_or_skip()
    import psycopg2
    from recall import _workspace_migrate_impl
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        # Ensure clean state.
        cur.execute("DELETE FROM workspaces")
        cur.execute("DELETE FROM repos")
        a_id = str(uuid.uuid4())
        b_id = str(uuid.uuid4())  # not in repos table, but still a valid UUID
        cur.execute("INSERT INTO repos (id, label, last_seen_path) "
                     "VALUES (%s, %s, %s)",
                     (a_id, "alpha", "/tmp/alpha"))
        cur.execute("INSERT INTO workspaces (name, repo_ids) "
                     "VALUES (%s, %s)",
                     ("ws2", [a_id, b_id, "stray-slug"]))
        conn.commit()
        report = _workspace_migrate_impl(conn)
        cur.execute("SELECT repo_ids FROM workspaces WHERE name='ws2'")
        result = cur.fetchone()[0]
        _aT("a_id preserved (already UUID)", a_id in result, str(result))
        _aT("b_id preserved (UUID, not in repos but valid form)",
             b_id in result, str(result))
        _aT("'stray-slug' is an orphan",
             "stray-slug" in report.get("orphans", []), str(report))


def main() -> int:
    tests = [test_migrate_maps_slugs_to_repo_ids_by_label,
              test_migrate_skips_already_uuid_entries]
    for t in tests:
        try: t()
        except Exception as e:
            import traceback; traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print(f"\n{'='*60}\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
