#!/usr/bin/env python3
"""Unit tests for code/session_copy.py — restic-backed snapshot subsystem.

Subprocess interaction with restic is fully mocked. Style: homemade
_step / _ok / _f / _aT.
"""
from __future__ import annotations

import json
import os
import subprocess
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


FIXTURES = ROOT / "tests" / "fixtures" / "session_copy"


def _mk_spec(td: Path):
    """Build a RepoSpec rooted at td."""
    import session_copy
    return session_copy.RepoSpec(
        repo_path=td / "restic-repo",
        passphrase_file=td / ".restic-pass",
        bin_path=Path("/usr/bin/restic"),
    )


def _fake_snapshots_json() -> str:
    """Return a fixture restic snapshots --json output (2 entries)."""
    return json.dumps([
        {
            "id": "abc123" + "0" * 58,
            "short_id": "abc12300",
            "time": "2026-05-15T10:00:00Z",
            "hostname": "canary",
            "paths": ["/home/user/.claude/projects"],
            "tags": ["timer"],
            "parent": None,
        },
        {
            "id": "def456" + "0" * 58,
            "short_id": "def45600",
            "time": "2026-05-15T10:05:00Z",
            "hostname": "canary",
            "paths": ["/home/user/.claude/projects"],
            "tags": ["stop-hook"],
            "parent": "abc123" + "0" * 58,
        },
    ])


# ── init_repo ───────────────────────────────────────────────────────────────

def test_init_repo_writes_passphrase_mode_600():
    _step("init_repo: writes .restic-pass mode 0o600 + invokes restic init")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        calls = []
        def fake_run(argv, *a, **kw):
            calls.append(argv)
            # snapshots probe (idempotency check) → fail so we go to init
            if argv[1:3] == ["snapshots", "--json"]:
                return subprocess.CompletedProcess(argv, 1,
                                                     stdout="", stderr="no repo")
            if argv[1] == "init":
                return subprocess.CompletedProcess(argv, 0,
                                                     stdout="created repository",
                                                     stderr="")
            raise AssertionError(f"unexpected call: {argv}")
        with mock.patch("subprocess.run", side_effect=fake_run):
            session_copy.init_repo(spec, prompt_passphrase=False)
        _aT("passphrase file exists",
             spec.passphrase_file.is_file(), str(spec.passphrase_file))
        # mode bits — 0o600
        mode = spec.passphrase_file.stat().st_mode & 0o777
        _aT(f"passphrase file mode 0o600 (got 0o{mode:o})",
             mode == 0o600)
        # passphrase content is a long hex string (token_hex(32) → 64 chars)
        content = spec.passphrase_file.read_text(encoding="utf-8").strip()
        _aT(f"passphrase is 64-char hex (got len {len(content)})",
             len(content) == 64
             and all(c in "0123456789abcdef" for c in content))
        # restic init was called
        init_called = any(a[1] == "init" for a in calls
                           if isinstance(a, list) and len(a) > 1)
        _aT("restic init was invoked", init_called)


def test_init_repo_idempotent():
    _step("init_repo: existing repo + passphrase + clean snapshots → "
           "no regenerate")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        # Pre-stage repo + passphrase
        spec.repo_path.mkdir(parents=True, exist_ok=True)
        (spec.repo_path / "config").write_bytes(b"x")
        original_pass = "a" * 64
        spec.passphrase_file.write_text(original_pass, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        def fake_run(argv, *a, **kw):
            if argv[1:3] == ["snapshots", "--json"]:
                return subprocess.CompletedProcess(argv, 0,
                                                     stdout="[]", stderr="")
            raise AssertionError(
                f"unexpected call (must be idempotent): {argv}")
        with mock.patch("subprocess.run", side_effect=fake_run):
            session_copy.init_repo(spec, prompt_passphrase=False)
        # Passphrase unchanged
        _aT("existing passphrase preserved",
             spec.passphrase_file.read_text(encoding="utf-8") == original_pass)


# ── take ────────────────────────────────────────────────────────────────────

def test_take_with_tag():
    _step("take: argv contains --tag, --exclude '*.tmp', source paths")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        spec.passphrase_file.write_text("p" * 64, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        src = td / "projects"
        src.mkdir()
        (src / "x.txt").write_text("x", encoding="utf-8")
        observed = {}
        def fake_run(argv, *a, **kw):
            if argv[1] == "backup":
                observed["argv"] = argv
                # Emit one minimal JSON status line + summary
                summary = {
                    "message_type": "summary",
                    "snapshot_id": "snap123" + "0" * 57,
                    "total_bytes_processed": 1,
                    "total_files_processed": 1,
                    "data_added": 1,
                }
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=json.dumps(summary) + "\n",
                    stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            snap = session_copy.take(spec, [src], tag="stop-hook")
        argv = observed["argv"]
        _aT("--tag stop-hook present",
             "--tag" in argv and "stop-hook" in argv, str(argv))
        _aT("--exclude '*.tmp' present",
             "--exclude" in argv and "*.tmp" in argv, str(argv))
        _aT("source path present",
             str(src) in argv, str(argv))
        _aT("snapshot id returned", snap is not None
             and snap.id.startswith("snap123"), str(snap))


def test_take_calls_sqlite_staging_first():
    _step("take: invokes stage_sqlite_db BEFORE restic backup")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        spec.passphrase_file.write_text("p" * 64, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        # Make a .db source
        db = td / "claude-mem.db"
        import sqlite3 as _sql
        with _sql.connect(str(db)) as c:
            c.execute("CREATE TABLE t (x INT)")
            c.execute("INSERT INTO t VALUES (1)")
            c.commit()
        order = []
        def fake_stage(src, staging_dir):
            order.append("stage")
            staging_dir.mkdir(parents=True, exist_ok=True)
            out = staging_dir / src.name
            out.write_bytes(src.read_bytes())
            return out
        def fake_run(argv, *a, **kw):
            if argv[1] == "backup":
                order.append("backup")
                summary = {"message_type": "summary",
                            "snapshot_id": "s" + "0" * 63}
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=json.dumps(summary) + "\n",
                    stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch.object(session_copy, "stage_sqlite_db",
                                side_effect=fake_stage), \
             mock.patch("subprocess.run", side_effect=fake_run):
            session_copy.take(spec, [db], tag="timer")
        _aT(f"order: stage before backup (got {order})",
             order[:2] == ["stage", "backup"])


# ── list_snapshots ──────────────────────────────────────────────────────────

def test_list_parses_json_output():
    _step("list_snapshots: parses restic --json into Snapshot dataclasses")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        spec.passphrase_file.write_text("p" * 64, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        def fake_run(argv, *a, **kw):
            if argv[1:3] == ["snapshots", "--json"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=_fake_snapshots_json(), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            snaps = session_copy.list_snapshots(spec)
        _aT("2 snapshots parsed", len(snaps) == 2, str(snaps))
        _aT("first id correct",
             snaps[0].id.startswith("abc123"), snaps[0].id)
        _aT("first tags include timer",
             "timer" in snaps[0].tags, str(snaps[0].tags))
        _aT("second parent set",
             snaps[1].parent == snaps[0].id, str(snaps[1]))


# ── prune ───────────────────────────────────────────────────────────────────

def test_prune_with_retention_args():
    _step("prune: argv contains --keep-hourly 24 --keep-daily 7 "
           "--keep-weekly 13")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        spec.passphrase_file.write_text("p" * 64, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        observed = {}
        def fake_run(argv, *a, **kw):
            if argv[1] == "forget":
                observed["argv"] = argv
                return subprocess.CompletedProcess(argv, 0,
                                                     stdout="{}", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            session_copy.prune(spec, dry_run=False, force=True)
        argv = observed["argv"]
        _aT("--keep-hourly 24",
             "--keep-hourly" in argv and "24" in argv, str(argv))
        _aT("--keep-daily 7",
             "--keep-daily" in argv and "7" in argv, str(argv))
        _aT("--keep-weekly 13",
             "--keep-weekly" in argv and "13" in argv, str(argv))


# ── status ──────────────────────────────────────────────────────────────────

def test_status_reports_size_and_age():
    _step("status: returns dict with repo_size + snapshot_count + age")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        spec.repo_path.mkdir(parents=True, exist_ok=True)
        # Put a small file in the repo to give it a measurable size.
        (spec.repo_path / "config").write_bytes(b"x" * 1024)
        spec.passphrase_file.write_text("p" * 64, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        def fake_run(argv, *a, **kw):
            if argv[1:3] == ["snapshots", "--json"]:
                return subprocess.CompletedProcess(
                    argv, 0, stdout=_fake_snapshots_json(), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            s = session_copy.status(spec)
        _aT("repo_size present + > 0",
             s.get("repo_size", 0) > 0, str(s))
        _aT("snapshot_count == 2",
             s.get("snapshot_count") == 2, str(s))
        _aT("last_snapshot_age_s is a number",
             isinstance(s.get("last_snapshot_age_s"), (int, float)),
             str(s))


# ── check (24h cache) ───────────────────────────────────────────────────────

def test_check_cached_to_24h():
    _step("check: caches result for 24h; --force bypasses cache")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        spec.passphrase_file.write_text("p" * 64, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        calls = []
        def fake_run(argv, *a, **kw):
            if argv[1] == "check":
                calls.append("check")
                return subprocess.CompletedProcess(argv, 0,
                                                     stdout="no errors found",
                                                     stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            r1 = session_copy.check(spec)
            after_r1 = list(calls)
            r2 = session_copy.check(spec)  # within cache window
            after_r2 = list(calls)
            r3 = session_copy.check(spec, force=True)
            after_r3 = list(calls)
        _aT("first call runs check",
             len(after_r1) >= 1, f"after_r1={after_r1}")
        _aT("second call hits cache (no extra check call)",
             len(after_r2) == 1, f"after_r2={after_r2}")
        _aT("force=True bypasses cache",
             len(after_r3) == 2, f"after_r3={after_r3}")
        _aT("returned dicts include status",
             "ok" in r1 and "ok" in r3, f"r1={r1} r3={r3}")


# ── restore ─────────────────────────────────────────────────────────────────

def test_restore_calls_restic_restore():
    _step("restore: argv contains restore <id> --target <path>")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        spec.passphrase_file.write_text("p" * 64, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        target = td / "restore-target"
        observed = {}
        def fake_run(argv, *a, **kw):
            if argv[1] == "restore":
                observed["argv"] = argv
                return subprocess.CompletedProcess(argv, 0,
                                                     stdout="restored",
                                                     stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            session_copy.restore(spec, "abc123" + "0" * 58, target)
        argv = observed["argv"]
        _aT("restore subcommand", "restore" in argv, str(argv))
        _aT("snapshot id present",
             any(a.startswith("abc123") for a in argv), str(argv))
        _aT("--target present + matches",
             "--target" in argv and str(target) in argv, str(argv))


# ── diff ────────────────────────────────────────────────────────────────────

def test_diff_parses_added_removed_changed():
    _step("diff: parses restic diff --json output → "
           "{added,removed,changed}")
    import session_copy
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        spec = _mk_spec(td)
        spec.passphrase_file.write_text("p" * 64, encoding="utf-8")
        os.chmod(spec.passphrase_file, 0o600)
        diff_lines = [
            json.dumps({"message_type": "change",
                         "path": "/a",
                         "modifier": "+"}),
            json.dumps({"message_type": "change",
                         "path": "/b",
                         "modifier": "-"}),
            json.dumps({"message_type": "change",
                         "path": "/c",
                         "modifier": "M"}),
            json.dumps({"message_type": "statistics",
                         "files": {"new": 1, "removed": 1, "changed": 1}}),
        ]
        def fake_run(argv, *a, **kw):
            if argv[1] == "diff":
                return subprocess.CompletedProcess(
                    argv, 0, stdout="\n".join(diff_lines) + "\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            d = session_copy.diff(spec,
                                    "abc123" + "0" * 58,
                                    "def456" + "0" * 58)
        _aT("added list",
             "/a" in d.get("added", []), str(d))
        _aT("removed list",
             "/b" in d.get("removed", []), str(d))
        _aT("changed list",
             "/c" in d.get("changed", []), str(d))


def main() -> int:
    print("claude-dejavu v0.8.0 session_copy tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_init_repo_writes_passphrase_mode_600,
        test_init_repo_idempotent,
        test_take_with_tag,
        test_take_calls_sqlite_staging_first,
        test_list_parses_json_output,
        test_prune_with_retention_args,
        test_status_reports_size_and_age,
        test_check_cached_to_24h,
        test_restore_calls_restic_restore,
        test_diff_parses_added_removed_changed,
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
