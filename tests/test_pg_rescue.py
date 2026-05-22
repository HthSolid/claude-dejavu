#!/usr/bin/env python3
"""Unit tests for code/pg_rescue.py — the v0.8.3 PG rescue toolkit.

Psycopg2 is fully mocked via a tiny FakeCursor / FakeConn pair so the
tests run without a live Postgres. Style: homemade _step / _ok / _f / _aT.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""):
    FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}")
    d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


# ─── Mini fake psycopg2 surface ───────────────────────────────────────


class FakeCursor:
    """Minimal cursor that records every executed SQL + supports a
    canned-response queue keyed on substring match.

    Tests register expectations via ``conn.expect("SELECT id FROM turns",
    rows=[(1,), (2,)])`` and assertions inspect ``conn.executed`` for
    SAVEPOINT discipline.
    """

    def __init__(self, conn):
        self.conn = conn
        self._last_rows: list = []
        self._last_one: tuple = ()

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql.strip(), params))
        # Apply optional "make this execute raise" hooks (per-row corruption sim)
        for substr, exc in self.conn.raise_on:
            if substr in sql:
                # Single-fire raise
                self.conn.raise_on.remove((substr, exc))
                raise exc
        rows = []
        for substr, val in self.conn.responses:
            if substr in sql:
                rows = val
                break
        self._last_rows = list(rows)
        self._last_one = rows[0] if rows else None

    def fetchone(self):
        return self._last_one

    def fetchall(self):
        return list(self._last_rows)


class FakeConn:
    def __init__(self):
        self.executed: list = []
        self.responses: list = []   # [(substring, rows)]
        self.raise_on: list = []    # [(substring, Exception)]
        self.autocommit = False
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def expect(self, substring, *, rows):
        self.responses.append((substring, rows))

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self): pass


# ─── scan ─────────────────────────────────────────────────────────────


def test_scan_returns_zero_corruption_on_clean_db():
    import pg_rescue
    _step("scan: clean DB → zero corrupt rows / indexes / pages")
    conn = FakeConn()
    # information_schema columns lookups (validate-before-probe path,
    # added 2026-05-17 after a column-drift false-positive incident).
    conn.expect("information_schema.columns",
                 rows=[("content",), ("tool_input",), ("tool_output",),
                       ("title",), ("subtitle",)])
    # Each table's id query returns an empty list
    conn.expect("SELECT id FROM turns", rows=[])
    conn.expect("SELECT id FROM tool_calls", rows=[])
    conn.expect("SELECT id FROM observations", rows=[])
    # No invalid indexes
    conn.expect("FROM pg_index", rows=[])
    # Page-level counts all succeed
    conn.expect("SELECT count(*) FROM turns", rows=[(0,)])
    conn.expect("SELECT count(*) FROM tool_calls", rows=[(0,)])
    conn.expect("SELECT count(*) FROM observations", rows=[(0,)])
    r = pg_rescue.scan(conn=conn)
    _aT("zero corrupt rows", len(r["corrupt_rows"]) == 0)
    _aT("zero corrupt indexes", len(r["corrupt_indexes"]) == 0)
    _aT("zero corrupt pages", len(r["corrupt_pages"]) == 0)
    _aT("savepoint pattern present",
         any("SAVEPOINT" in s.upper() for s, _ in conn.executed),
         "scan must use savepoints around per-row probes")


def test_scan_flags_bad_toast_row():
    import pg_rescue
    _step("scan: a row whose length() probe raises is reported")
    conn = FakeConn()
    conn.expect("information_schema.columns",
                 rows=[("content",), ("tool_input",), ("tool_output",),
                       ("title",), ("subtitle",)])
    conn.expect("SELECT id FROM turns", rows=[(42,)])
    conn.expect("SELECT id FROM tool_calls", rows=[])
    conn.expect("SELECT id FROM observations", rows=[])
    # length probe on turns#42 raises
    conn.raise_on.append(("FROM turns WHERE id =",
                            RuntimeError("missing chunk number 0 for toast value")))
    conn.expect("FROM pg_index", rows=[])
    conn.expect("SELECT count(*) FROM turns", rows=[(1,)])
    conn.expect("SELECT count(*) FROM tool_calls", rows=[(0,)])
    conn.expect("SELECT count(*) FROM observations", rows=[(0,)])
    r = pg_rescue.scan(conn=conn)
    _aT("one corrupt row found", len(r["corrupt_rows"]) == 1)
    _aT("error message captured",
         "missing chunk" in r["corrupt_rows"][0]["error"],
         str(r["corrupt_rows"][0]))


def test_scan_flags_invalid_index():
    import pg_rescue
    _step("scan: pg_index.indisvalid=false → corrupt_indexes")
    conn = FakeConn()
    for tbl in ("turns", "tool_calls", "observations"):
        conn.expect(f"SELECT id FROM {tbl}", rows=[])
    conn.expect("FROM pg_index", rows=[("idx_bad", "public")])
    for tbl in ("turns", "tool_calls", "observations"):
        conn.expect(f"SELECT count(*) FROM {tbl}", rows=[(0,)])
    r = pg_rescue.scan(conn=conn)
    _aT("one corrupt index",
         len(r["corrupt_indexes"]) == 1
         and r["corrupt_indexes"][0]["name"] == "idx_bad")


# ─── patch_toast ──────────────────────────────────────────────────────


def _mk_jsonl_dir(tmp: Path, *, project: str, session: str,
                    uuid: str, text: str) -> Path:
    pdir = tmp / project
    pdir.mkdir(parents=True, exist_ok=True)
    jsonl = pdir / f"{session}.jsonl"
    entry = {
        "uuid": uuid,
        "message": {"content": [{"type": "text", "text": text}]},
    }
    jsonl.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return tmp


def test_patch_toast_dry_run_does_not_commit():
    import pg_rescue
    _step("patch-toast: dry-run does NOT commit and reports 'would_patch'")
    conn = FakeConn()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _mk_jsonl_dir(td, project="proj-a", session="s1",
                        uuid="u1", text="recovered content")
        conn.expect("FROM turns t", rows=[
            ("s1", "u1", 0, "user", "text", "proj-a"),
        ])
        r = pg_rescue.patch_toast(conn=conn,
                                       bad_ids=[111],
                                       dry_run=True,
                                       projects_root=td)
    _aT("conn.commit() NOT called in dry-run",
         not conn.committed, "dry-run must NOT commit")
    _aT("conn.rollback() called",
         conn.rolled_back, "dry-run must rollback at end")
    _aT("one would-patch reported",
         r["patched"] == 1 and r["per_row"][0]["status"] == "would_patch")


def test_patch_toast_apply_commits_and_uses_savepoints():
    import pg_rescue
    _step("patch-toast: --apply commits + uses per-row SAVEPOINT")
    conn = FakeConn()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _mk_jsonl_dir(td, project="proj-a", session="s1",
                        uuid="u1", text="recovered content")
        conn.expect("FROM turns t", rows=[
            ("s1", "u1", 0, "user", "text", "proj-a"),
        ])
        r = pg_rescue.patch_toast(conn=conn, bad_ids=[111],
                                       dry_run=False,
                                       projects_root=td)
    _aT("conn.commit() called",
         conn.committed, "apply must commit")
    _aT("patched count is 1", r["patched"] == 1)
    sql_concat = " ".join(s for s, _ in conn.executed)
    _aT("SAVEPOINT row_sp used",
         "SAVEPOINT row_sp" in sql_concat,
         "per-row savepoint discipline missing")
    _aT("UPDATE turns issued",
         "UPDATE turns" in sql_concat)
    _aT("gist nulled in UPDATE",
         "gist = NULL" in sql_concat)


def test_patch_toast_missing_jsonl_is_reported_not_crashed():
    import pg_rescue
    _step("patch-toast: missing JSONL → per_row status='jsonl_entry_missing'")
    conn = FakeConn()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # NO JSONL file is created.
        conn.expect("FROM turns t", rows=[
            ("s1", "u1", 0, "user", "text", "proj-a"),
        ])
        r = pg_rescue.patch_toast(conn=conn, bad_ids=[111],
                                       dry_run=False,
                                       projects_root=td)
    _aT("errored count == 1", r["errored"] == 1)
    _aT("status is jsonl_entry_missing",
         r["per_row"][0]["status"] == "jsonl_entry_missing")


def test_patch_toast_per_row_savepoint_isolates_failures():
    import pg_rescue
    _step("patch-toast: error on one row does NOT poison the batch")
    conn = FakeConn()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _mk_jsonl_dir(td, project="proj-a", session="s1",
                        uuid="u1", text="ok")
        # Two SELECTs (one per bad id), both return a row.
        conn.expect("FROM turns t", rows=[
            ("s1", "u1", 0, "user", "text", "proj-a"),
        ])
        # First call to UPDATE turns will raise.
        conn.raise_on.append(("UPDATE turns",
                                 RuntimeError("toast tear")))
        r = pg_rescue.patch_toast(conn=conn, bad_ids=[111],
                                       dry_run=False,
                                       projects_root=td)
    _aT("errored == 1", r["errored"] == 1)
    sql_concat = " ".join(s for s, _ in conn.executed)
    _aT("ROLLBACK TO SAVEPOINT issued after failure",
         "ROLLBACK TO SAVEPOINT row_sp" in sql_concat)


# ─── reset_flags ──────────────────────────────────────────────────────


def test_reset_flags_dry_run_rolls_back():
    import pg_rescue
    _step("reset-flags: dry-run rolls back, doesn't commit")
    conn = FakeConn()
    conn.expect("SELECT id FROM turns", rows=[(1,), (2,), (3,)])
    r = pg_rescue.reset_flags(conn=conn, dry_run=True)
    _aT("rollback called", conn.rolled_back)
    _aT("commit NOT called", not conn.committed)
    _aT("total == 3", r["total"] == 3)
    _aT("reset == 3", r["reset"] == 3)


def test_reset_flags_apply_commits():
    import pg_rescue
    _step("reset-flags: apply commits + savepoints per row")
    conn = FakeConn()
    conn.expect("SELECT id FROM turns", rows=[(1,), (2,)])
    r = pg_rescue.reset_flags(conn=conn, dry_run=False)
    _aT("committed", conn.committed)
    _aT("per-row savepoints present",
         sum(1 for s, _ in conn.executed
             if "SAVEPOINT row_sp" in s and "ROLLBACK" not in s) >= 2,
         "expected ≥2 SAVEPOINT row_sp issues, one per row")


def test_reset_flags_skips_corrupt_rows():
    import pg_rescue
    _step("reset-flags: row that errors is recorded, batch continues")
    conn = FakeConn()
    conn.expect("SELECT id FROM turns", rows=[(1,), (2,)])
    conn.raise_on.append(("UPDATE turns",
                            RuntimeError("toast tear on id=1")))
    r = pg_rescue.reset_flags(conn=conn, dry_run=False)
    _aT("at least one error recorded", r["errored"] >= 1)
    _aT("at least one success", r["reset"] >= 1)


# ─── rebuild_table ────────────────────────────────────────────────────


def _seed_dependents(conn: FakeConn) -> None:
    # FKs
    conn.expect("WHERE c.contype = 'f'", rows=[
        ("tool_calls_turn_id_fkey", "tool_calls",
          "FOREIGN KEY (turn_id) REFERENCES turns(id) "
          "ON DELETE CASCADE"),
    ])
    conn.expect("FROM pg_views v", rows=[
        ("user_prompts", "SELECT * FROM turns WHERE role='user'"),
    ])
    conn.expect("FROM pg_indexes", rows=[
        ("turns_pkey", "CREATE UNIQUE INDEX turns_pkey ON turns(id)"),
    ])
    conn.expect("FROM pg_class s", rows=[
        ("turns_id_seq", "id"),
    ])


def test_rebuild_table_dry_run_reports_dependents():
    import pg_rescue
    _step("rebuild-table: dry-run discovers FKs + views + sequences")
    conn = FakeConn()
    _seed_dependents(conn)
    conn.expect("SELECT count(*) FROM turns", rows=[(42,)])
    r = pg_rescue.rebuild_table("turns", exclude_ids=[],
                                       conn=conn, dry_run=True)
    deps = r["dependents"]
    _aT("FK discovered", len(deps["fks"]) == 1)
    _aT("view discovered", len(deps["views"]) == 1)
    _aT("sequence discovered", len(deps["sequences"]) == 1)
    _aT("copied count from dry-run select", r["copied"] == 42)
    _aT("rollback in dry-run", conn.rolled_back)


def test_rebuild_table_apply_executes_full_plan():
    import pg_rescue
    _step("rebuild-table: apply path runs LOCK + DROP + CREATE + INSERT + swap")
    conn = FakeConn()
    _seed_dependents(conn)
    conn.expect("SELECT count(*) FROM turns_new", rows=[(41,)])
    conn.expect("conrelid =", rows=[("turns_new_check", )])
    conn.expect("to_regclass", rows=[(True,)])
    r = pg_rescue.rebuild_table("turns", exclude_ids=[7],
                                       conn=conn, dry_run=False)
    sqls = " ".join(s for s, _ in conn.executed).upper()
    _aT("ACCESS EXCLUSIVE LOCK acquired",
         "ACCESS EXCLUSIVE MODE" in sqls)
    _aT("DROP CONSTRAINT issued",
         "DROP CONSTRAINT IF EXISTS TOOL_CALLS_TURN_ID_FKEY" in sqls)
    _aT("DROP VIEW issued",
         "DROP VIEW IF EXISTS USER_PROMPTS" in sqls)
    _aT("CREATE TABLE turns_new ... LIKE",
         "CREATE TABLE TURNS_NEW (LIKE TURNS INCLUDING ALL)" in sqls)
    _aT("INSERT INTO turns_new with WHERE exclusion",
         "INSERT INTO TURNS_NEW SELECT" in sqls
         and "WHERE ID <> ALL" in sqls)
    _aT("DROP TABLE + RENAME swap",
         "DROP TABLE TURNS" in sqls and "RENAME TO TURNS" in sqls)
    _aT("Sequence reassigned",
         "ALTER SEQUENCE TURNS_ID_SEQ" in sqls)
    _aT("FK recreated",
         "ADD CONSTRAINT TOOL_CALLS_TURN_ID_FKEY" in sqls)
    _aT("View recreated",
         "CREATE VIEW USER_PROMPTS" in sqls)
    _aT("copied count surfaced", r["copied"] == 41)
    _aT("committed", conn.committed)


def test_rebuild_table_rolls_back_on_error():
    import pg_rescue
    _step("rebuild-table: any error → rollback + RescueError")
    conn = FakeConn()
    _seed_dependents(conn)
    conn.raise_on.append(("LOCK TABLE", RuntimeError("permission denied")))
    raised = False
    try:
        pg_rescue.rebuild_table("turns", exclude_ids=[7],
                                       conn=conn, dry_run=False)
    except pg_rescue.RescueError:
        raised = True
    _aT("RescueError raised", raised)
    _aT("rollback called", conn.rolled_back)


# ─── reindex ──────────────────────────────────────────────────────────


def test_reindex_filters_to_invalid_indexes_by_default():
    import pg_rescue
    _step("reindex: default only picks indexes flagged invalid")
    conn = FakeConn()
    conn.expect("indisvalid = FALSE", rows=[("idx_bad",)])
    r = pg_rescue.reindex(conn=conn, dry_run=False,
                                all_indexes=False,
                                concurrently=True)
    _aT("idx_bad reindexed", "idx_bad" in r["reindexed"])
    sqls = " ".join(s for s, _ in conn.executed).upper()
    _aT("REINDEX INDEX CONCURRENTLY",
         "REINDEX INDEX CONCURRENTLY" in sqls)


def test_reindex_all_indexes_path():
    import pg_rescue
    _step("reindex --all: picks every public-schema index")
    conn = FakeConn()
    conn.expect("WHERE n.nspname = 'public'",
                 rows=[("idx_a",), ("idx_b",)])
    r = pg_rescue.reindex(conn=conn, dry_run=False,
                                all_indexes=True,
                                concurrently=True)
    _aT("two indexes reindexed", len(r["reindexed"]) == 2)


def test_reindex_dry_run_reports_without_executing():
    import pg_rescue
    _step("reindex --dry-run: reports candidates without REINDEX")
    conn = FakeConn()
    conn.expect("indisvalid = FALSE", rows=[("idx_bad",)])
    r = pg_rescue.reindex(conn=conn, dry_run=True)
    sqls = " ".join(s for s, _ in conn.executed).upper()
    _aT("no REINDEX issued in dry-run",
         "REINDEX INDEX" not in sqls)
    _aT("idx_bad in reindexed list (would-do)",
         "idx_bad" in r["reindexed"])


def test_scan_columns_constant_includes_known_content_cols():
    import pg_rescue
    _step("scan: SCAN_COLUMNS includes the canonical content columns")
    _aT("turns.content listed",
         "content" in pg_rescue.SCAN_COLUMNS.get("turns", ()))
    _aT("tool_calls probes tool_input (jsonb cast to text)",
         "tool_input::text" in pg_rescue.SCAN_COLUMNS.get("tool_calls", ()))
    _aT("observations.title listed",
         "title" in pg_rescue.SCAN_COLUMNS.get("observations", ()))


def test_scan_skips_drifted_probe_columns():
    """Regression: a SCAN_COLUMNS entry whose column does NOT exist in
    the live schema must be skipped + reported under schema_mismatches,
    NOT counted as N false 'corrupt' rows. Caught 2026-05-17 when
    tool_calls.input was being probed against a schema that uses
    tool_input — produced 20168 phantom corrupt rows per probe column."""
    import pg_rescue
    _step("scan: drifted probe column is skipped + reported, not counted as corrupt")
    conn = FakeConn()
    # information_schema returns ONLY the turns.content column. The
    # tool_calls / observations probes should be skipped entirely
    # because the schema check finds no matching column name.
    conn.expect("information_schema.columns", rows=[("content",)])
    conn.expect("SELECT id FROM turns", rows=[])
    conn.expect("SELECT id FROM tool_calls", rows=[])
    conn.expect("SELECT id FROM observations", rows=[])
    conn.expect("FROM pg_index", rows=[])
    conn.expect("SELECT count(*) FROM turns", rows=[(0,)])
    conn.expect("SELECT count(*) FROM tool_calls", rows=[(0,)])
    conn.expect("SELECT count(*) FROM observations", rows=[(0,)])
    r = pg_rescue.scan(conn=conn)
    _aT("zero corrupt rows (drift surfaces as schema_mismatches, not corruption)",
         len(r["corrupt_rows"]) == 0,
         f"got {len(r['corrupt_rows'])} corrupt rows")
    _aT("schema_mismatches reported for drifted cols",
         len(r["schema_mismatches"]) >= 2,
         f"got {len(r['schema_mismatches'])} mismatches")
    bad_probes = {m["probe"] for m in r["schema_mismatches"]}
    _aT("tool_calls drift surfaced",
         any(p.startswith("tool_input") or p == "tool_output"
             for p in bad_probes),
         f"mismatches: {r['schema_mismatches']}")


def test_extract_content_handles_text_block():
    import pg_rescue
    _step("_extract_content_from_block: text block → block.text")
    out = pg_rescue._extract_content_from_block(
        {"text": "hello"}, "text")
    _aT("text extracted", out == "hello")


def test_extract_content_handles_tool_use_block():
    import pg_rescue
    _step("_extract_content_from_block: tool_use → JSON name+input")
    out = pg_rescue._extract_content_from_block(
        {"name": "Bash", "input": {"command": "ls"}}, "tool_use")
    d = json.loads(out)
    _aT("name preserved", d["name"] == "Bash")
    _aT("input preserved", d["input"] == {"command": "ls"})


def test_extract_content_handles_tool_result_list():
    import pg_rescue
    _step("_extract_content_from_block: tool_result content list → joined")
    out = pg_rescue._extract_content_from_block(
        {"content": [{"type": "text", "text": "a"},
                       {"type": "text", "text": "b"}]},
        "tool_result")
    _aT("joined with newline", out == "a\nb")


def test_find_jsonl_entry_locates_by_uuid():
    import pg_rescue
    _step("_find_jsonl_entry: returns the right line by uuid")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _mk_jsonl_dir(td, project="p1", session="s1", uuid="A",
                        text="alpha")
        # Append another entry
        (td / "p1" / "s1.jsonl").write_text(
            json.dumps({"uuid": "A", "message": {
                "content": [{"type": "text", "text": "alpha"}]}}) + "\n"
            + json.dumps({"uuid": "B", "message": {
                "content": [{"type": "text", "text": "beta"}]}}) + "\n",
            encoding="utf-8")
        e = pg_rescue._find_jsonl_entry("s1", "p1", "B",
                                              projects_root=td)
    _aT("found B", e is not None and e["uuid"] == "B")


def test_default_db_name_falls_back_to_user_pattern():
    import pg_rescue
    _step("_default_db_name uses claude_dejavu_<user> when no PG_DB cfg")
    # Force-clear PG_DB from any in-tree config.env by passing a path
    with mock.patch.object(pg_rescue, "_load_cfg", return_value={}):
        with mock.patch.dict("os.environ", {"USER": "alice"}, clear=False):
            name = pg_rescue._default_db_name()
    _aT("matches pattern", name.startswith("claude_dejavu_"),
         f"got {name!r}")


def main() -> int:
    print("claude-dejavu v0.8.3 pg_rescue tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_scan_returns_zero_corruption_on_clean_db,
        test_scan_flags_bad_toast_row,
        test_scan_flags_invalid_index,
        test_patch_toast_dry_run_does_not_commit,
        test_patch_toast_apply_commits_and_uses_savepoints,
        test_patch_toast_missing_jsonl_is_reported_not_crashed,
        test_patch_toast_per_row_savepoint_isolates_failures,
        test_reset_flags_dry_run_rolls_back,
        test_reset_flags_apply_commits,
        test_reset_flags_skips_corrupt_rows,
        test_rebuild_table_dry_run_reports_dependents,
        test_rebuild_table_apply_executes_full_plan,
        test_rebuild_table_rolls_back_on_error,
        test_reindex_filters_to_invalid_indexes_by_default,
        test_reindex_all_indexes_path,
        test_reindex_dry_run_reports_without_executing,
        test_scan_columns_constant_includes_known_content_cols,
        test_scan_skips_drifted_probe_columns,
        test_extract_content_handles_text_block,
        test_extract_content_handles_tool_use_block,
        test_extract_content_handles_tool_result_list,
        test_find_jsonl_entry_locates_by_uuid,
        test_default_db_name_falls_back_to_user_pattern,
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
        print(f"  ✗ {m}  {d[:120]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
