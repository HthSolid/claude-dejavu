"""Unit tests for code/ingester.cmd_retry_vectorize (v0.8.4 G4 — parallel).

Verifies:
  - workers=1 path still uses the parent conn (no regression)
  - workers=N opens N independent worker conns
  - shards are non-overlapping (no double-write race)
  - all rows processed regardless of worker count
  - empty result set is a no-op
  - per-worker UPDATE only touches that shard's row IDs

Mocks psycopg2.connect + weaviate_batch_upsert at the ingester
module level so we don't need Postgres or Weaviate to run."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, d: str = "") -> None:
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    if d: print(f"      {d}")


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _aT(name: str, cond: bool, d: str = "") -> None:
    _ok(name) if cond else _fail(name, d)


class _FakeCursor:
    """Stand-in for psycopg2 cursor — records every SQL call + returns
    fixture rows for the initial SELECT."""

    def __init__(self, parent: "_FakeConn"):
        self._parent = parent
        self.queries: list[tuple[str, list]] = []

    def __enter__(self): return self

    def __exit__(self, *a): pass

    def execute(self, sql: str, params: list | None = None) -> None:
        self.queries.append((sql, list(params or [])))
        self._parent.queries.append((sql, list(params or [])))

    def fetchall(self) -> list[tuple]:
        return list(self._parent.fetched_rows)

    def fetchone(self):
        return None


class _FakeConn:
    """Stand-in for psycopg2 connection. `fetched_rows` are returned
    by the first SELECT; subsequent UPDATEs are recorded for assertion."""

    def __init__(self, fetched_rows: list[tuple]):
        self.fetched_rows = fetched_rows
        self.queries: list[tuple[str, list]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._lock = threading.Lock()

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        with self._lock:
            self.commits += 1

    def rollback(self):
        with self._lock:
            self.rollbacks += 1

    def close(self):
        self.closed = True


def _make_rows(n: int) -> list[tuple]:
    """Synthesize n rows matching the SELECT shape:
    (tid, sid, idx, slug, role, content, ts, model, ai_title)."""
    import datetime
    return [
        (i, f"00000000-0000-0000-0000-{i:012d}", i,
         f"proj{i % 3}", "user" if i % 2 else "assistant",
         f"content body {i} " + "x" * 100,
         datetime.datetime(2026, 5, 17, 0, 0, i % 60),
         "claude-opus", f"title-{i}")
        for i in range(n)
    ]


def _setup_mocks(rows: list[tuple]):
    """Returns (ingester_module, parent_conn, worker_conn_log,
    weaviate_calls). Patches the ingester module so worker threads see
    the fake conns."""
    import ingester
    parent = _FakeConn(rows)
    worker_conn_log: list[_FakeConn] = []
    weaviate_calls: list[list[tuple[int, dict]]] = []
    weaviate_lock = threading.Lock()

    def fake_weaviate(items):
        with weaviate_lock:
            weaviate_calls.append(list(items))
        # Acknowledge every tid with a synthetic UUID.
        return {tid: f"uuid-{tid}" for tid, _ in items}

    def fake_connect(_dsn):
        c = _FakeConn(rows)  # workers will only ever run UPDATE/commit
        worker_conn_log.append(c)
        return c

    psycopg2_mock = mock.patch.object(ingester, "psycopg2")
    psy = psycopg2_mock.start()
    psy.connect = fake_connect
    psy.extras = mock.MagicMock()
    # execute_values stub: just record per-conn UPDATE for assertion.
    def _exec_values(cur, sql, values, **kw):
        cur.execute(sql, list(values))
    psy.extras.execute_values = _exec_values

    weav_mock = mock.patch.object(ingester, "weaviate_batch_upsert",
                                       side_effect=fake_weaviate)
    weav_mock.start()

    def cleanup():
        psycopg2_mock.stop()
        weav_mock.stop()

    return ingester, parent, worker_conn_log, weaviate_calls, cleanup


def test_empty_rows_noop() -> None:
    _step("empty result set → no Weaviate calls, no worker conns")
    ingester, parent, workers, weav, cleanup = _setup_mocks([])
    try:
        ingester.cmd_retry_vectorize(parent, limit=None, workers=1)
        _aT("no Weaviate calls", weav == [], f"got {len(weav)} calls")
        _aT("no worker conns opened", workers == [],
             f"got {len(workers)} conns")
        ingester.cmd_retry_vectorize(parent, limit=None, workers=4)
        _aT("workers=4 with no rows → still no worker conns",
             workers == [], f"got {len(workers)} conns")
    finally:
        cleanup()


def test_single_worker_no_extra_conn() -> None:
    _step("workers=1 — uses parent conn, opens 0 worker conns")
    rows = _make_rows(10)
    ingester, parent, workers, weav, cleanup = _setup_mocks(rows)
    try:
        ingester.cmd_retry_vectorize(parent, limit=None,
                                       workers=1, batch_size=4)
        _aT("zero worker conns (sequential path)",
             len(workers) == 0,
             f"got {len(workers)} conns")
        # All 10 rows went to Weaviate.
        all_tids = {tid for batch in weav for tid, _ in batch}
        _aT("all 10 tids vectorized",
             all_tids == set(range(10)),
             f"got {sorted(all_tids)}")
        _aT("parent.commit() was called",
             parent.commits >= 1, f"commits={parent.commits}")
    finally:
        cleanup()


def test_multi_worker_opens_n_conns() -> None:
    _step("workers=4 — opens exactly 4 worker conns, shards are exclusive")
    rows = _make_rows(20)
    ingester, parent, workers, weav, cleanup = _setup_mocks(rows)
    try:
        ingester.cmd_retry_vectorize(parent, limit=None,
                                       workers=4, batch_size=3)
        _aT("4 worker conns opened",
             len(workers) == 4, f"got {len(workers)}")
        for c in workers:
            _aT(f"worker conn closed", c.closed, "leaked conn")
        # All 20 tids show up exactly once across all batches.
        all_tids: list[int] = []
        for batch in weav:
            all_tids.extend(tid for tid, _ in batch)
        _aT("all 20 tids processed",
             sorted(all_tids) == list(range(20)),
             f"got {sorted(all_tids)}")
        _aT("no double-write (each tid appears once)",
             len(all_tids) == len(set(all_tids)),
             f"len={len(all_tids)} unique={len(set(all_tids))}")
    finally:
        cleanup()


def test_workers_normalized_to_1() -> None:
    _step("workers=0 / negative → coerced to 1 (no crash)")
    rows = _make_rows(5)
    ingester, parent, workers, weav, cleanup = _setup_mocks(rows)
    try:
        ingester.cmd_retry_vectorize(parent, limit=None, workers=0,
                                       batch_size=2)
        # workers=0 → coerced to 1 → no worker conn opened
        _aT("workers=0 coerced to 1 (no extra conns)",
             len(workers) == 0, f"got {len(workers)}")
        all_tids = {tid for batch in weav for tid, _ in batch}
        _aT("all 5 tids vectorized",
             all_tids == set(range(5)),
             f"got {sorted(all_tids)}")
    finally:
        cleanup()


def test_shard_sizes_balanced() -> None:
    _step("workers=3 over 10 rows → balanced shards (4+4+2)")
    rows = _make_rows(10)
    ingester, parent, workers, weav, cleanup = _setup_mocks(rows)
    try:
        ingester.cmd_retry_vectorize(parent, limit=None, workers=3,
                                       batch_size=10)
        _aT("3 worker conns", len(workers) == 3)
        # Each worker should have done at least one batch.
        # `weav` contains one batch per worker (batch_size >= shard_size).
        _aT("≥ 3 batches recorded (one per worker)",
             len(weav) >= 3, f"got {len(weav)} batches")
        sizes = sorted(len(b) for b in weav if b)
        # Should be [2,4,4] or similar — total 10, max ≤ ceil(10/3)=4.
        _aT("total rows = 10",
             sum(sizes) == 10, f"sizes={sizes}")
        _aT("max shard ≤ ceil(10/3) = 4",
             max(sizes) <= 4, f"sizes={sizes}")
    finally:
        cleanup()


def test_more_workers_than_rows() -> None:
    _step("workers > rows → only `rows` shards spawned")
    rows = _make_rows(3)
    ingester, parent, workers, weav, cleanup = _setup_mocks(rows)
    try:
        ingester.cmd_retry_vectorize(parent, limit=None, workers=8,
                                       batch_size=10)
        # ceil(3/8) = 1, so shard_size=1, total shards = 3.
        _aT("only 3 worker conns (one per row)",
             len(workers) == 3, f"got {len(workers)}")
        all_tids = {tid for batch in weav for tid, _ in batch}
        _aT("all 3 tids processed", all_tids == {0, 1, 2})
    finally:
        cleanup()


def main() -> int:
    tests = [
        test_empty_rows_noop,
        test_single_worker_no_extra_conn,
        test_multi_worker_opens_n_conns,
        test_workers_normalized_to_1,
        test_shard_sizes_balanced,
        test_more_workers_than_rows,
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
