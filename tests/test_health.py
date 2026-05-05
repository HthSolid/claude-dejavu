"""Unit tests for code/health.py — schema + Weaviate-class
integrity checks + auto-repair plumbing. Mocks PG connection +
Weaviate HTTP so it runs infra-free."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg): PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)
def _fail_(msg, d=""): FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}"); d and print(f"      {d}")
def _step(name): print(f"\n\033[36m── {name} ──\033[0m", flush=True)
def _aT(name, cond, d=""): _ok(name) if cond else _fail_(name, d)


def test_expected_pg_tables_parses_schema() -> None:
    _step("expected_pg_tables() parses every CREATE TABLE in schema.sql")
    import health
    tables = health.expected_pg_tables()
    _aT("non-empty result", len(tables) > 20,
         f"got {len(tables)} tables")
    # Spot-check key tables we know are in schema.sql
    for required in ("sessions", "turns", "tool_calls",
                       "session_summaries", "observations",
                       "symbols", "api_routes", "env_var_decls",
                       "feature_flags"):
        _aT(f"expected `{required}` present",
             required in tables)


def test_expected_weaviate_class() -> None:
    _step("expected_weaviate_class() reads weaviate-class.json")
    import health
    cname = health.expected_weaviate_class()
    _aT("class name is ClaudeDejavuTurn",
         cname == "ClaudeDejavuTurn",
         f"got {cname!r}")


def _fake_conn(actual_tables):
    """Build a minimal psycopg2-connection mock that returns
    `actual_tables` for the schema query."""
    cur = mock.MagicMock()
    cur.fetchall.return_value = [(t,) for t in actual_tables]
    cur.__enter__ = mock.MagicMock(return_value=cur)
    cur.__exit__ = mock.MagicMock(return_value=False)
    conn = mock.MagicMock()
    conn.cursor.return_value = cur
    conn.commit = mock.MagicMock()
    conn.rollback = mock.MagicMock()
    return conn, cur


def test_check_pg_schema_all_present() -> None:
    _step("check_pg_schema: all expected tables present → ok")
    import health
    expected = health.expected_pg_tables()
    conn, _ = _fake_conn(expected)
    r = health.check_pg_schema(conn)
    _aT("ok=True", r["ok"], f"got {r}")
    _aT("missing list empty", r["missing"] == [])
    _aT("error is None", r["error"] is None)


def test_check_pg_schema_some_missing() -> None:
    _step("check_pg_schema: some missing → ok=False, missing populated")
    import health
    expected = health.expected_pg_tables()
    actual = list(expected)[:5]  # only 5 of the expected tables
    conn, _ = _fake_conn(actual)
    r = health.check_pg_schema(conn)
    _aT("ok=False", not r["ok"])
    _aT("missing has expected count",
         len(r["missing"]) == len(expected) - 5,
         f"missing={len(r['missing'])} expected_diff="
         f"{len(expected) - 5}")


def test_check_pg_schema_handles_query_error() -> None:
    _step("check_pg_schema: query error → returns error in dict, "
            "no exception raised")
    import health
    conn = mock.MagicMock()
    cur = mock.MagicMock()
    cur.execute.side_effect = RuntimeError("connection lost")
    cur.__enter__ = mock.MagicMock(return_value=cur)
    cur.__exit__ = mock.MagicMock(return_value=False)
    conn.cursor.return_value = cur
    r = health.check_pg_schema(conn)
    _aT("ok=False", not r["ok"])
    _aT("error mentions RuntimeError",
         "RuntimeError" in (r.get("error") or ""))


def test_check_weaviate_class_404() -> None:
    _step("check_weaviate_class: 404 from /v1/schema/<name> → "
            "exists=False, error=None (clean miss)")
    import health
    import urllib.error
    with mock.patch("urllib.request.urlopen") as m:
        m.side_effect = urllib.error.HTTPError(
            "u", 404, "Not Found", {}, None)
        r = health.check_weaviate_class("http://x", "C")
    _aT("ok=False", not r["ok"])
    _aT("exists=False", not r["exists"])
    _aT("error is None for 404", r["error"] is None,
         f"got error={r['error']!r}")


def test_check_weaviate_class_unreachable() -> None:
    _step("check_weaviate_class: unreachable Weaviate → "
            "exists=False, error populated")
    import health
    with mock.patch("urllib.request.urlopen") as m:
        m.side_effect = ConnectionRefusedError("nope")
        r = health.check_weaviate_class("http://x", "C")
    _aT("ok=False", not r["ok"])
    _aT("error contains exception type",
         "ConnectionRefusedError" in (r.get("error") or ""))


def test_check_and_repair_skips_when_healthy() -> None:
    _step("check_and_repair_storage: all green → no repair "
            "attempted, ok=True")
    import health
    expected = health.expected_pg_tables()
    conn, _ = _fake_conn(expected)
    with mock.patch.object(health, "check_weaviate_class") as wm:
        wm.return_value = {"ok": True, "exists": True,
                              "url": "http://x", "class": "C",
                              "error": None}
        r = health.check_and_repair_storage(
            conn=conn, wv_url="http://x", auto_repair=True)
    _aT("ok=True overall", r["ok"])
    _aT("any_repair_applied=False", not r["any_repair_applied"])
    _aT("any_repair_failed=False", not r["any_repair_failed"])
    _aT("pg.repair is None (nothing to do)",
         r["pg"]["repair"] is None)


def test_format_repair_notice_no_issues() -> None:
    _step("format_repair_notice: returns None when everything is ok")
    import health
    r = health.format_repair_notice({
        "ok": True, "any_repair_applied": False,
        "any_repair_failed": False, "pg": None, "weaviate": None})
    _aT("returns None", r is None)


def test_format_repair_notice_missing_tables() -> None:
    _step("format_repair_notice: renders a usable message when "
            "PG tables are missing")
    import health
    report = {
        "ok": False, "any_repair_applied": False,
        "any_repair_failed": True,
        "pg": {
            "check": {"ok": False, "missing": ["t1", "t2"],
                        "error": None},
            "repair": {"ok": False, "applied": False,
                         "error": "psql exited 1"},
        },
        "weaviate": None,
    }
    msg = health.format_repair_notice(report)
    _aT("non-None notice", msg is not None)
    _aT("mentions missing count", "2 table(s) missing" in (msg or ""))
    _aT("mentions repair failure", "FAILED" in (msg or ""))


def test_assess_recovery_sources() -> None:
    _step("assess_recovery_sources counts jsonl files at both paths")
    import health
    r = health.assess_recovery_sources()
    _aT("returns live_source_count", "live_source_count" in r)
    _aT("returns off_tree_backup_count", "off_tree_backup_count" in r)
    _aT("preferred_source is one of expected values",
         r["preferred_source"] in ("live", "backup", None))


def test_detect_data_loss_empty_db() -> None:
    _step("detect_session_data_loss: count=0 + source available "
            "→ data_loss=True")
    import health
    conn = mock.MagicMock()
    cur = mock.MagicMock()
    cur.fetchone.return_value = (0,)
    cur.__enter__ = mock.MagicMock(return_value=cur)
    cur.__exit__ = mock.MagicMock(return_value=False)
    conn.cursor.return_value = cur
    with mock.patch.object(health, "assess_recovery_sources") as m:
        m.return_value = {"live_source_count": 5,
                            "off_tree_backup_count": 0,
                            "preferred_source": "live",
                            "live_source_path": "/x",
                            "off_tree_backup_path": "/y"}
        r = health.detect_session_data_loss(conn)
    _aT("data_loss=True", r["data_loss"])
    _aT("sessions_in_db=0", r["sessions_in_db"] == 0)


def test_detect_data_loss_populated_db() -> None:
    _step("detect_session_data_loss: count>0 → data_loss=False")
    import health
    conn = mock.MagicMock()
    cur = mock.MagicMock()
    cur.fetchone.return_value = (42,)
    cur.__enter__ = mock.MagicMock(return_value=cur)
    cur.__exit__ = mock.MagicMock(return_value=False)
    conn.cursor.return_value = cur
    with mock.patch.object(health, "assess_recovery_sources") as m:
        m.return_value = {"live_source_count": 5,
                            "off_tree_backup_count": 0,
                            "preferred_source": "live",
                            "live_source_path": "/x",
                            "off_tree_backup_path": "/y"}
        r = health.detect_session_data_loss(conn)
    _aT("data_loss=False (db has rows)", not r["data_loss"])


def test_detect_data_loss_no_source() -> None:
    _step("detect_session_data_loss: empty DB but no source → "
            "data_loss=False (nothing to recover from)")
    import health
    conn = mock.MagicMock()
    cur = mock.MagicMock()
    cur.fetchone.return_value = (0,)
    cur.__enter__ = mock.MagicMock(return_value=cur)
    cur.__exit__ = mock.MagicMock(return_value=False)
    conn.cursor.return_value = cur
    with mock.patch.object(health, "assess_recovery_sources") as m:
        m.return_value = {"live_source_count": 0,
                            "off_tree_backup_count": 0,
                            "preferred_source": None,
                            "live_source_path": "/x",
                            "off_tree_backup_path": "/y"}
        r = health.detect_session_data_loss(conn)
    _aT("data_loss=False (no recovery source)", not r["data_loss"])


def test_kickoff_recovery_unknown_source() -> None:
    _step("kickoff_recovery_ingest: unknown source → ok=False")
    import health
    r = health.kickoff_recovery_ingest(
        plugin_root=Path("/tmp"), venv_python=Path("/tmp/python"),
        source="bogus")
    _aT("ok=False", not r["ok"])
    _aT("error mentions unknown source",
         "bogus" in (r.get("error") or ""))


def test_kickoff_recovery_missing_ingester() -> None:
    _step("kickoff_recovery_ingest: missing ingester.py → ok=False")
    import health
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        r = health.kickoff_recovery_ingest(
            plugin_root=Path(tmp),
            venv_python=Path("/tmp/python"),
            source="live")
        _aT("ok=False", not r["ok"])
        _aT("error mentions ingester.py",
             "ingester.py" in (r.get("error") or ""))


def test_format_recovery_notice() -> None:
    _step("format_repair_notice: data-loss + auto-recovery → "
            "renders re-ingest message")
    import health
    report = {
        "ok": True, "any_repair_applied": False,
        "any_repair_failed": False,
        "pg": {"check": {"ok": True, "missing": [], "error": None,
                            "expected": 30, "actual": 30, "extra": []}},
        "weaviate": None,
        "data_loss": {
            "data_loss": True, "sessions_in_db": 0,
            "recovery": {"live_source_count": 7,
                            "off_tree_backup_count": 0,
                            "preferred_source": "live",
                            "live_source_path": "/x",
                            "off_tree_backup_path": "/y"},
        },
        "recovery": {
            "triggered": True, "source": "live",
            "spawn": {"ok": True, "pid": 1234,
                        "log": "/tmp/recovery.log",
                        "error": None,
                        "command": ["python", "ingester.py", "--bootstrap"]},
        },
    }
    msg = health.format_repair_notice(report)
    _aT("non-None notice", msg is not None)
    _aT("mentions sessions empty",
         "sessions table empty" in (msg or ""))
    _aT("mentions live source", "live source" in (msg or ""))
    _aT("mentions log path", "/tmp/recovery.log" in (msg or ""))


def main() -> int:
    print("claude-dejavu health-module tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_expected_pg_tables_parses_schema,
        test_expected_weaviate_class,
        test_check_pg_schema_all_present,
        test_check_pg_schema_some_missing,
        test_check_pg_schema_handles_query_error,
        test_check_weaviate_class_404,
        test_check_weaviate_class_unreachable,
        test_check_and_repair_skips_when_healthy,
        test_format_repair_notice_no_issues,
        test_format_repair_notice_missing_tables,
        test_assess_recovery_sources,
        test_detect_data_loss_empty_db,
        test_detect_data_loss_populated_db,
        test_detect_data_loss_no_source,
        test_kickoff_recovery_unknown_source,
        test_kickoff_recovery_missing_ingester,
        test_format_recovery_notice,
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

# (Recovery / data-loss tests — appended after main() above run on import; the
# main() runner picks them up via the explicit `tests = [...]` list, so we
# extend that list before main() runs. The cleanest way is to redefine the
# module via a fresh script. Instead, here we add additional test functions
# AND patch into the runner.)
