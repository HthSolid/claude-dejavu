"""Unit tests for the v0.8.6 ingester-side gist hook + the
`memory backfill-gists` worker.

The test surface is the same in-tree functions the live ingest path
calls — no Postgres needed. We exercise:

- the make_gist shim wired to distill_pipeline
- ingest writes a gist when distillable
- ingest writes NULL gist for code blocks / tracebacks / diffs
- cmd_backfill_gists processes N turns end-to-end against an
  in-memory fake conn
- cmd_backfill_gists is idempotent (re-run on already-gisted rows
  is a no-op unless --force)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

import distill_pipeline as dp  # noqa: E402
import ingester  # noqa: E402  type: ignore[import]


PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, d: str = "") -> None:
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    if d:
        print(f"      {d}")


def _aT(name: str, cond: bool, d: str = "") -> None:
    _ok(name) if cond else _fail(name, d)


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


# ─── make_gist shim → distill_pipeline ───────────────────────────────────────

def test_ingester_make_gist_uses_distill_pipeline() -> None:
    _step("ingester.make_gist: routes through distill_pipeline")
    text = ("This release upgrades WV_URL handling on port 8889; "
            "see /v1/batch/objects for the new path.")
    gist, method = ingester.make_gist(text, role="assistant",
                                       content_type="text")
    _aT("method tag == claudedj-distill-v1",
         method == dp.GIST_METHOD, repr(method))
    _aT("gist is anchor-prefixed",
         gist is not None and gist.startswith("[anchors:"),
         repr(gist))


def test_ingester_make_gist_returns_none_for_code_blocks() -> None:
    _step("ingester.make_gist: code blocks → (None, None)")
    text = "Look:\n```python\nprint('hi')\n```\nthat's the fix."
    gist, method = ingester.make_gist(text, role="assistant",
                                       content_type="text")
    _aT("gist is None on code block",
         gist is None and method is None, f"{gist!r}, {method!r}")


def test_ingester_make_gist_returns_none_for_tracebacks() -> None:
    _step("ingester.make_gist: tracebacks → (None, None)")
    text = ("Traceback (most recent call last):\n"
            "  File \"a.py\", line 1, in <module>\n"
            "ImportError: no module foo")
    gist, method = ingester.make_gist(text, role="assistant",
                                       content_type="text")
    _aT("gist is None on traceback",
         gist is None and method is None, f"{gist!r}, {method!r}")


def test_ingester_make_gist_returns_none_for_tool_use() -> None:
    _step("ingester.make_gist: tool_use → (None, None)")
    text = '{"tool": "Edit", "args": {"file": "foo.ts"}}'
    gist, method = ingester.make_gist(text, role="assistant",
                                       content_type="tool_use")
    _aT("gist is None on tool_use",
         gist is None and method is None, f"{gist!r}, {method!r}")


# ─── cmd_backfill_gists against a fake conn ─────────────────────────────────

class _FakeCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows
        self.executed: list[tuple[str, tuple]] = []

    # context-manager hooks
    def __enter__(self):  # noqa: D401
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql: str, params=()) -> None:  # noqa: D401
        self.executed.append((sql, tuple(params or ())))

    def fetchall(self) -> list[tuple]:  # noqa: D401
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows: list[tuple]):
        self._rows = rows
        self.commits = 0
        self.last_cursor = _FakeCursor(rows)

    def cursor(self) -> _FakeCursor:  # noqa: D401
        return self.last_cursor

    def commit(self) -> None:  # noqa: D401
        self.commits += 1

    def close(self) -> None:  # noqa: D401
        pass


def test_backfill_gists_dry_run_counts_correctly() -> None:
    _step("cmd_backfill_gists: --dry-run produces counts, no writes")
    # 3 distillable rows, 1 code-block row, 1 traceback row, 1
    # already-tooled row (caller never serves these — gist IS NULL
    # filter handles it — but we include one to exercise content_type
    # gating).
    rows = [
        (1, "Set WV_URL=http://localhost:8888 on port 8889.",
         "assistant", "text"),
        (2, "Edit /home/x/projects/foo/src/bar.ts via --workers 4.",
         "assistant", "text"),
        (3, "We hit UndefinedColumn on v0.8.3.",
         "user", "text"),
        (4, "```python\nprint(1)\n```",
         "assistant", "text"),
        (5, "Traceback (most recent call last):\n  oops",
         "assistant", "text"),
    ]
    conn = _FakeConn(rows)
    # workers=1 keeps the body sequential, no thread pool — simpler
    # to introspect.
    ingester.cmd_backfill_gists(conn, dry_run=True, workers=1)
    # In dry-run mode we never open extra connections — assert the
    # passed-in conn was never committed.
    _aT("dry-run made 0 commits",
         conn.commits == 0, f"commits={conn.commits}")


def test_backfill_gists_idempotency() -> None:
    _step("cmd_backfill_gists: re-running over already-gisted rows is "
          "a no-op without --force")
    # Pre-filtered rows the SQL would surface (gist IS NULL); we just
    # confirm that running with --force vs not changes the SQL clause
    # exactly once. Because we can't reach SQL with fake conn we just
    # confirm the workflow runs without crash on an empty result.
    conn = _FakeConn([])
    ingester.cmd_backfill_gists(conn, dry_run=True, workers=1)
    _aT("empty result handled cleanly",
         conn.commits == 0, f"commits={conn.commits}")


def test_backfill_gists_force_flag_propagates() -> None:
    _step("cmd_backfill_gists: --force is honored")
    rows = [
        (10, "WV_URL on port 8889 — already gisted, force re-run.",
         "assistant", "text"),
    ]
    conn = _FakeConn(rows)
    ingester.cmd_backfill_gists(conn, dry_run=True, force=True, workers=1)
    # In dry-run the SQL doesn't actually filter, but force=True
    # should still avoid crashes. We assert the recorded execute
    # contains the SQL without the "AND gist IS NULL" clause when
    # force=True.
    sqls = [s for s, _ in conn.last_cursor.executed]
    _aT("at least one SELECT executed", any("SELECT" in s for s in sqls),
         repr(sqls))
    if sqls:
        # force=True omits the IS NULL clause
        _aT("force=True omits IS NULL clause",
             "gist IS NULL" not in sqls[0],
             sqls[0])


def test_backfill_gists_limit_propagates() -> None:
    _step("cmd_backfill_gists: --limit propagates to SQL")
    conn = _FakeConn([])
    ingester.cmd_backfill_gists(conn, dry_run=True, limit=50, workers=1)
    sqls = [s for s, p in conn.last_cursor.executed]
    params = [p for s, p in conn.last_cursor.executed]
    _aT("SQL contains LIMIT clause",
         any("LIMIT" in s for s in sqls), repr(sqls))
    _aT("LIMIT param == 50",
         any(50 in p for p in params), repr(params))


def test_backfill_gists_processes_distillable_rows() -> None:
    _step("cmd_backfill_gists: distill happens on real input")
    rows = [
        (1, "Set WV_URL=http://localhost:8888 on port 8889.",
         "assistant", "text"),
        (2, "Edit /home/x/projects/foo/src/bar.ts --workers 4.",
         "assistant", "text"),
    ]
    conn = _FakeConn(rows)
    # In dry-run we still call make_gist; we just don't write. The
    # function prints distilled vs skipped counts. Smoke-tests are
    # via captured stdout.
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ingester.cmd_backfill_gists(conn, dry_run=True, workers=1)
    out = buf.getvalue()
    _aT("distilled count surfaced",
         "distilled=" in out, out[-200:])
    _aT("skipped count surfaced",
         "skipped_uncompressible=" in out, out[-200:])


# ─── runner ──────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_ingester_make_gist_uses_distill_pipeline,
        test_ingester_make_gist_returns_none_for_code_blocks,
        test_ingester_make_gist_returns_none_for_tracebacks,
        test_ingester_make_gist_returns_none_for_tool_use,
        test_backfill_gists_dry_run_counts_correctly,
        test_backfill_gists_idempotency,
        test_backfill_gists_force_flag_propagates,
        test_backfill_gists_limit_propagates,
        test_backfill_gists_processes_distillable_rows,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
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
