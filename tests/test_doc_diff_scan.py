"""Integration test for doc_diff_scan: synthetic repo + mock Weaviate +
real Postgres via DEJAVU_TEST_PG_DSN."""
from __future__ import annotations
import os, sys, tempfile, time
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
        print("SKIP: set DEJAVU_TEST_PG_DSN"); sys.exit(0)
    return dsn


def test_diff_scan_re_embeds_only_changed_files():
    _step("diff scan: change 1 of 3 files → exactly 1 re-embed event")
    dsn = _pg_or_skip()
    import doc_diff_scan
    embed_calls = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "diffrepo"
        (root / ".git").mkdir(parents=True)
        for name in ("a.md", "b.md", "c.md"):
            (root / name).write_text(f"# {name}\nbody\n")
        # First scan: should embed all 3.
        events = doc_diff_scan.scan(
            root, dsn=dsn,
            embed_fn=lambda repo_id, rel, chunks, **kw: (
                embed_calls.append(("embed", rel)) or 1),
            delete_fn=lambda *a, **k: 0,
        )
        _aT("first scan embeds 3", events["embedded"] == 3,
             str(events))
        _aT("embed_fn called 3 times on first scan",
             len(embed_calls) == 3, str(embed_calls))
        embed_calls.clear()
        # Mutate one.
        time.sleep(0.01)  # ensure mtime changes
        (root / "b.md").write_text("# b\nDIFFERENT body\n")
        events = doc_diff_scan.scan(
            root, dsn=dsn,
            embed_fn=lambda repo_id, rel, chunks, **kw: (
                embed_calls.append(("embed", rel)) or 1),
            delete_fn=lambda *a, **k: 0,
        )
        _aT("second scan re-embeds exactly 1", events["embedded"] == 1,
             str(events))
        _aT("second scan re-embeds b.md",
             embed_calls == [("embed", "b.md")], str(embed_calls))


def test_diff_scan_tombstones_deleted_files():
    _step("diff scan tombstones files that no longer exist")
    dsn = _pg_or_skip()
    import doc_diff_scan
    delete_calls = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "delrepo"
        (root / ".git").mkdir(parents=True)
        (root / "a.md").write_text("# a\n")
        doc_diff_scan.scan(
            root, dsn=dsn,
            embed_fn=lambda *a, **k: 1,
            delete_fn=lambda *a, **k: 0,
        )
        (root / "a.md").unlink()
        events = doc_diff_scan.scan(
            root, dsn=dsn,
            embed_fn=lambda *a, **k: 1,
            delete_fn=lambda rid, fp: (
                delete_calls.append((rid, fp)) or 1),
        )
        _aT("delete_fn called once", len(delete_calls) == 1,
             str(delete_calls))
        _aT("delete_fn called with the missing file path",
             delete_calls[0][1] == "a.md", str(delete_calls))


def main() -> int:
    tests = [test_diff_scan_re_embeds_only_changed_files,
              test_diff_scan_tombstones_deleted_files]
    for t in tests:
        try: t()
        except Exception as e:
            import traceback; traceback.print_exc()
            FAIL.append((t.__name__, str(e)))
    print(f"\n{'='*60}\nPASS: {len(PASS)}  FAIL: {len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
