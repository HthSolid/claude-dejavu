#!/usr/bin/env python3
"""Unit tests for code/migrate_rsync.py — at-risk inventory + signal
detection (H4 of v0.8.0).

Style: homemade _step / _ok / _f / _aT. Subprocess + DB are mocked.
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
def _f(m, d=""): FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}"); d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


UUID_AT_RISK = "42bc895b-f449-4c5a-ac78-2a1d3fb28fd5"
UUID_SAFE_PG = "00000000-0000-0000-0000-000000000001"  # has PG copy, 5 turns
UUID_ZERO_TURNS = "00000000-0000-0000-0000-000000000002"  # PG row, 0 turns → at-risk


def _build_legacy_tree(root: Path) -> Path:
    """Build a synthetic 3-snapshot rsync chain under ``root``.

    Returns the snapshots dir. Layout matches the
    ``tests/fixtures/migrate_rsync/`` README.
    """
    snaps = root / "snapshots"
    proj = "-fake-proj"
    # OLDEST: contains all three UUIDs (the at-risk one only lives here)
    old = snaps / "20260101-000000" / ".claude" / "projects" / proj
    old.mkdir(parents=True, exist_ok=True)
    (old / f"{UUID_SAFE_PG}.jsonl").write_text(
        '{"type":"user","content":"safe-1"}\n', encoding="utf-8")
    (old / f"{UUID_ZERO_TURNS}.jsonl").write_text(
        '{"type":"user","content":"zero-turns"}\n', encoding="utf-8")
    (old / f"{UUID_AT_RISK}.jsonl").write_text(
        '{"type":"user","content":"at-risk-1"}\n'
        '{"type":"assistant","content":"at-risk-2"}\n',
        encoding="utf-8")
    # MID + NEWEST: no at-risk UUID
    for ts in ("20260101-000500", "20260101-001000"):
        d = snaps / ts / ".claude" / "projects" / proj
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{UUID_SAFE_PG}.jsonl").write_text(
            '{"type":"user","content":"safe"}\n', encoding="utf-8")
        (d / f"{UUID_ZERO_TURNS}.jsonl").write_text(
            '{"type":"user","content":"zero"}\n', encoding="utf-8")
    return snaps


def _build_live_tree(root: Path, uuids: list[str]) -> Path:
    """Build a ``~/.claude/projects/`` look-alike at ``root``."""
    proj = "-fake-proj"
    live = root / "live" / ".claude" / "projects" / proj
    live.mkdir(parents=True, exist_ok=True)
    for u in uuids:
        (live / f"{u}.jsonl").write_text(
            '{"type":"user","content":"live"}\n', encoding="utf-8")
    return root / "live"


class _FakeCursor:
    """Minimal psycopg2-cursor-shaped mock.

    Records the SQL it sees so tests can introspect (e.g. assert
    ``::text`` cast present). ``fetchall()`` returns rows configured
    by the constructor.
    """

    def __init__(self, returns: list[tuple], collect: list[str]):
        self._returns = returns
        self._collect = collect
        self._args: tuple | None = None

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, args=()):
        self._collect.append(sql)
        self._args = args

    def fetchall(self): return self._returns

    def close(self): pass


class _FakeConn:
    def __init__(self, rows: list[tuple]):
        self.captured_sql: list[str] = []
        self._rows = rows

    def cursor(self):  # used as context manager
        return _FakeCursor(self._rows, self.captured_sql)


# ── tests ───────────────────────────────────────────────────────────────────

def test_detect_legacy_state_returns_true_with_artifacts():
    _step("detect_legacy_state: returns True when snapshots dir + script exist")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # build fake legacy artifacts under a synthetic $HOME
        home = td / "home"
        snaps = home / ".claude-backups" / "snapshots" / "20260101-000000"
        snaps.mkdir(parents=True, exist_ok=True)
        script = home / ".claude-backups" / "scripts" / "snapshot.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\necho legacy\n", encoding="utf-8")
        data_root = td / "data"
        data_root.mkdir(parents=True, exist_ok=True)
        result = M.detect_legacy_state(home, data_root, conn=None)
        _aT("detect=True with snapshots+script", result is True)


def test_detect_legacy_state_returns_false_clean():
    _step("detect_legacy_state: returns False with no artifacts")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        home = td / "home"
        home.mkdir(parents=True, exist_ok=True)
        data_root = td / "data"
        data_root.mkdir(parents=True, exist_ok=True)
        result = M.detect_legacy_state(home, data_root, conn=None)
        _aT("detect=False on clean env", result is False)


def test_at_risk_inventory_finds_uniquely_historical():
    _step("at_risk_inventory: UUID present only in OLDEST snapshot + missing "
          "from live + missing from PG → flagged at-risk")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        snaps = _build_legacy_tree(td)
        live = _build_live_tree(td, [UUID_SAFE_PG])
        # PG knows about UUID_SAFE_PG (5 turns). Not about UUID_AT_RISK.
        # UUID_ZERO_TURNS: row exists but 0 turns → still at-risk (zero-turns
        # filter excludes it from the safe set).
        conn = _FakeConn(rows=[(UUID_SAFE_PG,)])
        items = M.at_risk_inventory(snaps, live / ".claude" / "projects", conn)
        ids = {it.session_id for it in items}
        _aT(f"UUID_AT_RISK in at-risk set (got {ids})",
             UUID_AT_RISK in ids)
        _aT(f"UUID_SAFE_PG NOT in at-risk set",
             UUID_SAFE_PG not in ids)


def test_at_risk_query_with_zero_turns_counts_as_at_risk():
    _step("at_risk_inventory: PG session with 0 turns is treated as at-risk")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        snaps = _build_legacy_tree(td)
        live = _build_live_tree(td, [UUID_SAFE_PG])
        # The SQL filters by count(*) > 0, so a 0-turns session row is
        # never returned in the safe set. We simulate by NOT including
        # UUID_ZERO_TURNS in the cursor's fetchall.
        conn = _FakeConn(rows=[(UUID_SAFE_PG,)])
        items = M.at_risk_inventory(snaps, live / ".claude" / "projects", conn)
        ids = {it.session_id for it in items}
        _aT(f"UUID_ZERO_TURNS (DB row + 0 turns) flagged at-risk "
             f"(got {ids})",
             UUID_ZERO_TURNS in ids)


def test_at_risk_query_uses_text_cast():
    _step("at_risk_inventory: generated SQL contains ::text cast "
          "(spec §3.6 Step 2)")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        snaps = _build_legacy_tree(td)
        live = _build_live_tree(td, [UUID_SAFE_PG])
        conn = _FakeConn(rows=[])
        _ = M.at_risk_inventory(snaps, live / ".claude" / "projects", conn)
        all_sql = "\n".join(conn.captured_sql).lower()
        _aT(f"::text cast appears in SQL (got SQL: {all_sql[:200]!r})",
             "::text" in all_sql)
        _aT("count(*) > 0 filter in SQL (zero-turns gate)",
             "count(" in all_sql and "> 0" in all_sql)


def main() -> int:
    print("migrate_rsync detection tests")
    tests = [
        test_detect_legacy_state_returns_true_with_artifacts,
        test_detect_legacy_state_returns_false_clean,
        test_at_risk_inventory_finds_uniquely_historical,
        test_at_risk_query_with_zero_turns_counts_as_at_risk,
        test_at_risk_query_uses_text_cast,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback; traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print()
    print("=" * 60)
    print(f"PASS: {len(PASS)}     FAIL: {len(FAIL)}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
