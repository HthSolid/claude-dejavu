"""Unit tests for code/recovery.py — file corruption detection + replay.

Golden test: applies the 3 captured Edit ops from the example fixture session
against the v5 file-history snapshot and asserts byte-equality with the
expected output. If this fails, recovery is wrong and shipping is blocked.
"""
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(m): PASS.append(m); print(f"  \033[32m✓\033[0m {m}", flush=True)
def _f(m, d=""): FAIL.append((m, d)); print(f"  \033[31m✗\033[0m {m}"); d and print(f"      {d}")
def _step(n): print(f"\n\033[36m── {n} ──\033[0m", flush=True)
def _aT(n, c, d=""): _ok(n) if c else _f(n, d)


FIX = ROOT / "tests" / "fixtures" / "recovery"


# ── Corruption detection ─────────────────────────────────────────────────

def test_detect_zeroed_all_null():
    _step("detect_corruption: 100% NUL file → ZEROED")
    from recovery import detect_corruption, Status
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tf:
        tf.write(b"\0" * 1024); p = Path(tf.name)
    try:
        _aT("status == ZEROED", detect_corruption(p) == Status.ZEROED,
            f"got {detect_corruption(p)}")
    finally:
        p.unlink(missing_ok=True)


def test_detect_partial_zero_ts():
    _step("detect_corruption: 7% NUL in .ts → PARTIAL_ZERO")
    from recovery import detect_corruption, Status
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tf:
        tf.write(b"valid content\n" * 100 + b"\0" * 105); p = Path(tf.name)
    try:
        _aT("status == PARTIAL_ZERO", detect_corruption(p) == Status.PARTIAL_ZERO,
            f"got {detect_corruption(p)}")
    finally:
        p.unlink(missing_ok=True)


def test_detect_utf16_bom_not_partial_zero():
    _step("detect_corruption: UTF-16-LE BOM file → OK (not PARTIAL_ZERO)")
    from recovery import detect_corruption, Status
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tf:
        # UTF-16-LE BOM + alternating ASCII (lots of NUL bytes but valid UTF-16)
        tf.write(b"\xff\xfe" + b"h\x00e\x00l\x00l\x00o\x00" * 50); p = Path(tf.name)
    try:
        _aT("status == OK (BOM carve-out)", detect_corruption(p) == Status.OK,
            f"got {detect_corruption(p)}")
    finally:
        p.unlink(missing_ok=True)


def test_detect_binary_lock_skipped():
    _step("detect_corruption: .lock file with high NUL ratio → OK (binary)")
    from recovery import detect_corruption, Status
    with tempfile.NamedTemporaryFile(suffix=".lock", delete=False) as tf:
        tf.write(b"\0\0\0\0\0\0\0\0" * 100); p = Path(tf.name)
    try:
        # 100% NUL still flags ZEROED even for binary (size>0 + all-NUL is
        # unambiguous corruption). Use mixed bytes to exercise the
        # extension carve-out for PARTIAL_ZERO.
        # → re-write with 50% NUL ratio
        p.write_bytes(b"x" * 400 + b"\0" * 400)
        _aT("status == OK (binary extension)", detect_corruption(p) == Status.OK,
            f"got {detect_corruption(p)}")
    finally:
        p.unlink(missing_ok=True)


def test_detect_low_nul_ratio_ok():
    _step("detect_corruption: 4% NUL in .ts → OK")
    from recovery import detect_corruption, Status
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tf:
        tf.write(b"valid content abc\n" * 100 + b"\0" * 60); p = Path(tf.name)
    try:
        _aT("status == OK (under 5% threshold)",
            detect_corruption(p) == Status.OK,
            f"got {detect_corruption(p)}")
    finally:
        p.unlink(missing_ok=True)


def test_detect_shrunk_via_snapshot_size():
    _step("detect_corruption: 0 bytes vs 1000-byte snapshot → SHRUNK")
    from recovery import detect_corruption, Status
    with tempfile.NamedTemporaryFile(suffix=".ts", delete=False) as tf:
        p = Path(tf.name)
    try:
        # explicit zero-byte file
        p.write_bytes(b"")
        _aT("status == SHRUNK (0 bytes, snapshot=1000)",
            detect_corruption(p, snapshot_size=1000) == Status.SHRUNK,
            f"got {detect_corruption(p, snapshot_size=1000)}")
    finally:
        p.unlink(missing_ok=True)


# ── Edit collection ──────────────────────────────────────────────────────

def test_collect_edit_ops_chronological():
    _step("collect_edit_ops: returns ops in chronological order, 3 ops")
    from recovery import collect_edit_ops
    ops = collect_edit_ops(FIX / "example" / "edits.jsonl",
                            target_path="lifecycle.ts")
    _aT("3 ops collected", len(ops) == 3, f"got {len(ops)}")
    if len(ops) >= 3:
        timestamps = [o[0] for o in ops]
        _aT("strictly increasing ts",
             timestamps == sorted(timestamps), str(timestamps))
        _aT("all ops are Edit",
             all(o[1] == "Edit" for o in ops),
             f"names={[o[1] for o in ops]}")


# ── Apply edits ──────────────────────────────────────────────────────────

def test_apply_edits_str_replace_semantics():
    _step("apply_edits: matches Python str.replace semantics")
    from recovery import apply_edits
    # str.replace("a", "aa", 1) on "a" → "aa", NOT infinite loop
    base = "a"
    ops = [("2026-01-01T00:00:00Z", "Edit",
            {"old_string": "a", "new_string": "aa", "replace_all": False})]
    _aT("single replace, no loop", apply_edits(base, ops) == "aa")


def test_apply_edits_example_golden():
    _step("apply_edits: example fixture fixture round-trip is byte-exact")
    from recovery import collect_edit_ops, apply_edits
    base = (FIX / "example" / "lifecycle_v5.ts").read_text(encoding="utf-8")
    ops = collect_edit_ops(FIX / "example" / "edits.jsonl",
                            target_path="lifecycle.ts")
    result = apply_edits(base, ops)
    expected = (FIX / "example" / "expected.ts").read_text(encoding="utf-8")
    _aT("byte-exact match", result == expected,
         f"result={len(result)} expected={len(expected)}")


def test_apply_edits_missing_old_string_raises():
    _step("apply_edits: missing old_string raises ApplyError")
    from recovery import apply_edits, ApplyError
    try:
        apply_edits("hello world",
                     [("2026-01-01T00:00:00Z", "Edit",
                       {"old_string": "MISSING_TOKEN", "new_string": "x",
                        "replace_all": False})])
        _f("should have raised ApplyError")
    except ApplyError as e:
        _aT("ApplyError raised", True)
        _aT("error mentions missing string",
             "MISSING_TOKEN" in str(e), str(e))


def test_apply_edits_write_overrides_buffer():
    _step("apply_edits: Write op discards prior buffer")
    from recovery import apply_edits
    base = "garbage\n"
    ops = [("2026-01-01T00:00:00Z", "Write",
            {"content": "fresh content\n"})]
    _aT("Write replaces buffer wholesale",
        apply_edits(base, ops) == "fresh content\n")


def test_apply_edits_replace_all():
    _step("apply_edits: replace_all=True replaces every occurrence")
    from recovery import apply_edits
    base = "foo foo foo"
    ops = [("2026-01-01T00:00:00Z", "Edit",
            {"old_string": "foo", "new_string": "bar", "replace_all": True})]
    _aT("all 3 replaced", apply_edits(base, ops) == "bar bar bar")


# ── Path normalization ───────────────────────────────────────────────────

def test_resolve_path_absolute():
    _step("resolve_path: absolute path → realpath")
    from recovery import resolve_path
    out = resolve_path("/etc/passwd", project_path="/home/x")
    _aT("absolute echoed as realpath", out == "/etc/passwd",
        f"got {out}")


def test_resolve_path_relative_via_project():
    _step("resolve_path: relative path → resolved against project_path")
    from recovery import resolve_path
    import os
    out = resolve_path("foo/bar.ts", project_path="/tmp/proj")
    # realpath may resolve through /tmp symlinks differently per OS; assert
    # the path ends with the expected suffix
    _aT("relative resolved against project",
        out.endswith("/proj/foo/bar.ts") or out.endswith("foo/bar.ts"),
        f"got {out}")


# ── Write-no-prior-snapshot branch (strategy 4) ──────────────────────────

def test_pick_base_write_only_history():
    _step("pick_base: file first appears via Write → base = '', WRITE_ONLY")
    from recovery import pick_base, BaseSource
    ops = [("2026-01-01T00:00:00Z", "Write", {"content": "hello\nworld\n"})]
    base, src, since_ts = pick_base(target="/tmp/new.ts", session_id="s1",
                          ops=ops,
                          file_history_dir=Path("/nonexistent"),
                          git_repo=None)
    _aT("base is empty string", base == "",
        f"got {base!r}")
    _aT("source is BaseSource.WRITE_ONLY",
         src == BaseSource.WRITE_ONLY, str(src))
    _aT("since_ts is None for WRITE_ONLY",
         since_ts is None, f"got {since_ts!r}")


def test_pick_base_file_history_returns_since_ts():
    _step("pick_base: FILE_HISTORY strategy returns the snapshot's backupTime")
    # Regression: without since_ts, callers replay pre-snapshot ops and
    # fail with ApplyError on stale old_strings. Discovered during a
    # live G4 test 2026-05-15.
    from recovery import pick_base, BaseSource, _find_latest_snapshot
    # Use the staged fixtures: the live snapshot picker uses the JSONL
    # at ~/.claude/projects/.../<session>.jsonl plus the
    # ~/.claude/file-history dir — both must exist on this machine for
    # this test to run. Skip cleanly when running in CI without them.
    import os
    sid = os.environ.get("DEJAVU_RECOVERY_FIXTURE_SESSION",
                            "00000000-0000-0000-0000-000000000000")
    fixture_file = os.environ.get(
        "DEJAVU_RECOVERY_FIXTURE_FILE",
        str(Path.home() / "fixture-project" / "src"
              / "lifecycle.ts"))
    fhd = Path.home() / ".claude" / "file-history"
    if not (fhd / sid).is_dir():
        _ok("(no live file-history dir for the fixture session; "
             "skip — set DEJAVU_RECOVERY_FIXTURE_SESSION + "
             "DEJAVU_RECOVERY_FIXTURE_FILE to enable locally)")
        return
    snap_result = _find_latest_snapshot(
        fhd, fixture_file, session_id=sid)
    _aT("snapshot picker returns (path, ts) tuple",
         snap_result is not None and isinstance(snap_result, tuple)
         and len(snap_result) == 2,
         f"got {snap_result!r}")
    if snap_result is None:
        return
    snap_path, snap_ts = snap_result
    _aT("snapshot is the latest version (@v5)",
         "@v5" in str(snap_path), f"got {snap_path}")
    _aT("backupTime is 2026-05-14T16:39:01.962Z",
         snap_ts == "2026-05-14T16:39:01.962Z",
         f"got {snap_ts!r}")


def main() -> int:
    print("claude-dejavu v0.7.2 recovery primitive tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_detect_zeroed_all_null,
        test_detect_partial_zero_ts,
        test_detect_utf16_bom_not_partial_zero,
        test_detect_binary_lock_skipped,
        test_detect_low_nul_ratio_ok,
        test_detect_shrunk_via_snapshot_size,
        test_collect_edit_ops_chronological,
        test_apply_edits_str_replace_semantics,
        test_apply_edits_example_golden,
        test_apply_edits_missing_old_string_raises,
        test_apply_edits_write_overrides_buffer,
        test_apply_edits_replace_all,
        test_resolve_path_absolute,
        test_resolve_path_relative_via_project,
        test_pick_base_write_only_history,
        test_pick_base_file_history_returns_since_ts,
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
