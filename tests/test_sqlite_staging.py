#!/usr/bin/env python3
"""Unit tests for code/sqlite_staging.py — VACUUM INTO-based DB staging.

Style: homemade _step / _ok / _f / _aT (no pytest).
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""): FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}"); d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


def _make_wal_db(path: Path, rows: int = 100) -> None:
    """Create a WAL-mode SQLite DB with ``rows`` initial rows."""
    with sqlite3.connect(str(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany(
            "INSERT INTO t (v) VALUES (?)",
            [(f"row-{i}",) for i in range(rows)])
        conn.commit()


def _count_rows(path: Path) -> int:
    with sqlite3.connect(str(path)) as conn:
        cur = conn.execute("SELECT count(*) FROM t")
        return int(cur.fetchone()[0])


# ── Tests ──────────────────────────────────────────────────────────────────

def test_python_sqlite_version_supports_vacuum_into():
    _step("python sqlite3 module >= 3.27.0 (VACUUM INTO supported)")
    v = sqlite3.sqlite_version_info
    _aT(f"sqlite_version_info {v} >= (3, 27, 0)",
         v >= (3, 27, 0), str(v))


def test_vacuum_into_sanity_no_error():
    _step("VACUUM INTO: write txn in flight, staged DB opens without "
           "'disk image is malformed'")
    import sqlite_staging
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "live.db"
        staging = td / "staging"
        _make_wal_db(src, rows=50)

        stop = threading.Event()
        def writer():
            conn = sqlite3.connect(str(src))
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN")
                # Trickle rows until told to stop
                i = 1000
                while not stop.is_set():
                    conn.execute("INSERT INTO t (v) VALUES (?)",
                                  (f"w-{i}",))
                    i += 1
                    time.sleep(0.001)
                conn.commit()
            finally:
                conn.close()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        time.sleep(0.05)
        try:
            staged = sqlite_staging.stage_sqlite_db(src, staging)
        finally:
            stop.set(); t.join(timeout=5)

        _aT("staged path returned", staged is not None
             and Path(staged).is_file(), str(staged))
        # Open the staged DB and read it — must not raise "malformed".
        try:
            n = _count_rows(Path(staged))
            _aT(f"staged DB opens cleanly (rows={n})", True)
        except sqlite3.DatabaseError as e:
            _aT("staged DB opens cleanly", False, str(e))


def test_vacuum_into_committed_or_pre_commit_only():
    _step("VACUUM INTO torn-snapshot proof (20 iterations): staged "
           "row count is ALWAYS pre-txn or post-commit, never in between")
    import sqlite_staging

    INSERTS = 1000
    ITER = 20
    torn = 0
    for it in range(ITER):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "live.db"
            staging = td / "staging"
            _make_wal_db(src, rows=100)
            N0 = _count_rows(src)

            start_evt = threading.Event()
            done_evt = threading.Event()

            def writer():
                conn = sqlite3.connect(str(src))
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("BEGIN")
                    for i in range(INSERTS):
                        conn.execute(
                            "INSERT INTO t (v) VALUES (?)",
                            (f"w-{i}",))
                        if i == INSERTS // 4:
                            # Signal: staging may run now
                            start_evt.set()
                        if i % 50 == 0:
                            time.sleep(0.0005)
                    conn.commit()
                finally:
                    conn.close()
                    done_evt.set()

            t = threading.Thread(target=writer, daemon=True)
            t.start()
            # Wait until writer is mid-transaction
            start_evt.wait(timeout=5)
            staged = sqlite_staging.stage_sqlite_db(src, staging)
            done_evt.wait(timeout=10)
            t.join(timeout=5)

            if staged is None:
                _f(f"iter {it}: stage returned None")
                continue
            Ns = _count_rows(Path(staged))
            if Ns != N0 and Ns != N0 + INSERTS:
                torn += 1
                _f(f"iter {it}: TORN — Ns={Ns}, "
                    f"expected {N0} or {N0 + INSERTS}")
    _aT(f"0 torn snapshots out of {ITER} iterations",
         torn == 0, f"torn count = {torn}")
    # Also surface the iteration count to the report.
    print(f"      iterations: {ITER}, torn: {torn}")


def test_vacuum_into_size_smaller_than_or_equal_source():
    _step("VACUUM INTO: staged file size <= source size (defragmented)")
    import sqlite_staging
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "live.db"
        staging = td / "staging"
        # Use enough rows that the file is multi-page so deletion +
        # vacuum has measurable defrag impact. ~50k rows with a ~100-byte
        # payload pushes the file well past 1 MB; deleting half should
        # then yield a measurably smaller staged file.
        with sqlite3.connect(str(src)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
            payload = "x" * 200
            conn.executemany(
                "INSERT INTO t (v) VALUES (?)",
                [(payload,) for _ in range(50_000)])
            conn.commit()
            conn.execute("DELETE FROM t WHERE id % 2 = 0")
            conn.commit()
            # Force WAL checkpoint so source size reflects the deletion
            # (otherwise the free pages just sit unused in the main file).
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        src_size = src.stat().st_size
        staged = sqlite_staging.stage_sqlite_db(src, staging)
        staged_size = Path(staged).stat().st_size
        _aT(f"staged ({staged_size}) <= source ({src_size})",
             staged_size <= src_size,
             f"src={src_size} staged={staged_size}")


def test_vacuum_into_missing_source_returns_none():
    _step("VACUUM INTO: source missing → returns None, no exception")
    import sqlite_staging
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "does-not-exist.db"
        staging = td / "staging"
        try:
            r = sqlite_staging.stage_sqlite_db(src, staging)
            _aT("returns None on missing source",
                 r is None, str(r))
        except Exception as e:
            _aT("returns None on missing source", False, str(e))


def main() -> int:
    print("claude-dejavu v0.8.0 sqlite_staging tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_python_sqlite_version_supports_vacuum_into,
        test_vacuum_into_sanity_no_error,
        test_vacuum_into_committed_or_pre_commit_only,
        test_vacuum_into_size_smaller_than_or_equal_source,
        test_vacuum_into_missing_source_returns_none,
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
    print(f"PASS: {len(PASS)}  FAIL: {len(FAIL)}")
    for m, d in FAIL:
        print(f"  ✗ {m}  {d[:80]}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
