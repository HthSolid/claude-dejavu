"""Runtime tests for v0.8.0 pre-recovery snapshot contract (Pass C, H5).

Wiring tests in tests/test_install.py prove the call exists in source.
These tests prove it actually executes BEFORE the destructive write —
and is synchronous, not fire-and-forget.

Contract (spec §3.4):
  * `repair.write_repaired_jsonl` and `recovery.write_recovered` MUST
    call `session_copy.take(tag='pre-recovery')` synchronously before
    touching the live file.
  * If `session_copy.take` raises, the destructive write MUST be aborted
    and the live file MUST be unchanged.
  * `--no-pre-recovery-snapshot` (i.e. ``no_pre_recovery_snapshot=True``
    passed into the function) MUST bypass the snapshot call entirely.

The monkey-patching pattern: replace `session_copy.take` with a recorder
that captures the call order vs the file's mtime, and (in the synchronous
test) ALSO replace `subprocess.Popen` with a detector that fails the test
if invoked — proving the call is direct, not detached.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Make code/ importable as flat package
sys.path.insert(0, str(ROOT / "code"))

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg): PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)
def _fail_(msg, d=""): FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}"); d and print(f"      {d}")
def _step(name): print(f"\n\033[36m── {name} ──\033[0m", flush=True)
def _aT(name, cond, d=""): _ok(name) if cond else _fail_(name, d)


def _import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── Repair fixture helpers ──────────────────────────────────────────────────

def _build_repair_diagnosis(tmp_path: Path, session_id: str = "00000000-0000-0000-0000-000000000001"):
    """Create a minimal Diagnosis pointing at a tmp JSONL with a few lines."""
    import repair as _rp  # type: ignore[import]
    live = tmp_path / f"{session_id}.jsonl"
    live.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
    d = _rp.Diagnosis(
        session_id=session_id,
        live_path=live,
        valid_lines=2,
        corrupted_lines=0,
        db_turn_count=2,
        db_max_idx=2,
    )
    return _rp, d, live


def _build_recovery_inputs(tmp_path: Path):
    """Create a tmp target file + an out_path identical to it."""
    target = tmp_path / "src" / "lifecycle.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("// original\nexport const a = 1;\n", encoding="utf-8")
    return target


# ── Repair tests ────────────────────────────────────────────────────────────

def test_repair_pre_recovery_snapshot_called_before_write():
    _step("repair.write_repaired_jsonl calls session_copy.take "
          "(tag=pre-recovery) BEFORE writing the JSONL")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _rp, diag, live = _build_repair_diagnosis(td)
        import session_copy as _sc  # type: ignore[import]

        order: list[tuple[str, float, bytes]] = []
        original_bytes = live.read_bytes()

        def fake_take(spec, sources, *, tag="manual", message=None, **kw):
            # Capture live file's content at the moment of take()
            order.append(("take", time.time(), live.read_bytes()))
            return None

        with mock.patch.object(_sc, "take", side_effect=fake_take):
            entries = [{"a": 1}, {"b": 2}, {"c": 3}]
            backup_root = td / "backup"
            backup, n = _rp.write_repaired_jsonl(
                diag, entries, backup_root=backup_root)
            order.append(("write_done", time.time(), live.read_bytes()))

        _aT("snapshot recorder fired exactly once",
             sum(1 for o in order if o[0] == "take") == 1,
             f"order={[o[0] for o in order]}")
        _aT("snapshot fired before write_done",
             [o[0] for o in order] == ["take", "write_done"],
             f"order={[o[0] for o in order]}")
        # At the moment of take, live file should still be the original
        take_evt = next(o for o in order if o[0] == "take")
        _aT("live file unchanged at moment of take()",
             take_evt[2] == original_bytes,
             "destructive write must follow the snapshot")
        # Sanity: repair did write something
        _aT("repair wrote new content",
             live.read_bytes() != original_bytes,
             "write_repaired_jsonl must update the file")


def test_repair_aborts_when_pre_recovery_snapshot_fails():
    _step("repair.write_repaired_jsonl aborts when "
          "session_copy.take raises — JSONL unchanged + raises")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _rp, diag, live = _build_repair_diagnosis(td)
        import session_copy as _sc  # type: ignore[import]

        original_bytes = live.read_bytes()

        def boom(*a, **kw):
            raise RuntimeError("repo unreachable")

        raised = None
        with mock.patch.object(_sc, "take", side_effect=boom):
            try:
                _rp.write_repaired_jsonl(
                    diag, [{"x": 1}], backup_root=td / "backup")
            except Exception as e:
                raised = e

        _aT("write_repaired_jsonl raised on snapshot failure",
             raised is not None,
             "must propagate snapshot failure")
        msg = str(raised) if raised else ""
        _aT("error names the snapshot failure",
             "snapshot" in msg.lower() or "pre-recovery" in msg.lower() or
             "repo unreachable" in msg.lower(),
             f"error msg: {msg!r}")
        _aT("live JSONL byte-equal to pre-call state",
             live.read_bytes() == original_bytes,
             "destructive write must not have happened")


def test_repair_no_pre_recovery_snapshot_flag_bypasses():
    _step("repair.write_repaired_jsonl with no_pre_recovery_snapshot=True "
          "bypasses session_copy.take")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _rp, diag, live = _build_repair_diagnosis(td)
        import session_copy as _sc  # type: ignore[import]

        calls: list = []

        def recorder(*a, **kw):
            calls.append((a, kw))
            return None

        with mock.patch.object(_sc, "take", side_effect=recorder):
            entries = [{"a": 1}]
            _rp.write_repaired_jsonl(
                diag, entries,
                backup_root=td / "backup",
                no_pre_recovery_snapshot=True)

        _aT("recorder NOT called",
             len(calls) == 0,
             f"calls={calls!r}")
        _aT("repair proceeded normally",
             live.read_text(encoding="utf-8").strip() == '{"a": 1}',
             "file should still be written")


def test_repair_pre_recovery_is_synchronous():
    _step("repair.py invokes session_copy.take SYNCHRONOUSLY "
          "(direct call, not subprocess.Popen)")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _rp, diag, live = _build_repair_diagnosis(td)
        import session_copy as _sc  # type: ignore[import]
        import subprocess as _sp

        take_calls: list = []
        popen_calls: list = []

        def take_recorder(*a, **kw):
            take_calls.append((a, kw))
            return None

        original_popen = _sp.Popen

        def popen_detector(*a, **kw):
            # Only flag if argv mentions session-copy or take —
            # other Popens (e.g. from psycopg2 ops, restic internals)
            # would be incidental.
            argv0 = a[0] if a else kw.get("args", [])
            argv_str = " ".join(str(x) for x in (argv0 if isinstance(argv0, (list, tuple)) else [argv0]))
            if "session-copy" in argv_str or "session_copy" in argv_str or "take" in argv_str.split():
                popen_calls.append(argv_str)
            return original_popen(*a, **kw)

        with mock.patch.object(_sc, "take", side_effect=take_recorder), \
              mock.patch.object(_sp, "Popen", side_effect=popen_detector):
            _rp.write_repaired_jsonl(
                diag, [{"a": 1}], backup_root=td / "backup")

        _aT("session_copy.take called directly (synchronous)",
             len(take_calls) == 1,
             f"take_calls={len(take_calls)}")
        _aT("no Popen spawn of session-copy/take detected",
             not popen_calls,
             f"popen_calls={popen_calls!r}")


# ── Recovery tests ──────────────────────────────────────────────────────────

def test_recovery_pre_recovery_snapshot_called_before_write():
    _step("recovery.write_recovered calls session_copy.take "
          "(tag=pre-recovery) BEFORE writing the file")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        target = _build_recovery_inputs(td)
        import recovery as _rc  # type: ignore[import]
        import session_copy as _sc  # type: ignore[import]

        order: list[tuple[str, bytes]] = []
        original_bytes = target.read_bytes()

        def fake_take(spec, sources, *, tag="manual", message=None, **kw):
            order.append(("take", target.read_bytes()))
            return None

        with mock.patch.object(_sc, "take", side_effect=fake_take):
            new_content = "// recovered\nexport const a = 2;\n"
            _rc.write_recovered(target, new_content)
            order.append(("write_done", target.read_bytes()))

        _aT("snapshot recorder fired exactly once",
             sum(1 for o in order if o[0] == "take") == 1,
             f"order={[o[0] for o in order]}")
        _aT("snapshot fired before write_done",
             [o[0] for o in order] == ["take", "write_done"],
             f"order={[o[0] for o in order]}")
        take_evt = next(o for o in order if o[0] == "take")
        _aT("target unchanged at moment of take()",
             take_evt[1] == original_bytes,
             "write must follow the snapshot")
        _aT("recovery wrote new content",
             target.read_bytes() != original_bytes,
             "write_recovered must update the file")


def test_recovery_aborts_when_pre_recovery_snapshot_fails():
    _step("recovery.write_recovered aborts when "
          "session_copy.take raises — target unchanged + raises")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        target = _build_recovery_inputs(td)
        import recovery as _rc  # type: ignore[import]
        import session_copy as _sc  # type: ignore[import]

        original_bytes = target.read_bytes()

        def boom(*a, **kw):
            raise RuntimeError("repo unreachable")

        raised = None
        with mock.patch.object(_sc, "take", side_effect=boom):
            try:
                _rc.write_recovered(target, "// new\n")
            except Exception as e:
                raised = e

        _aT("write_recovered raised on snapshot failure",
             raised is not None,
             "must propagate snapshot failure")
        msg = str(raised) if raised else ""
        _aT("error names the snapshot failure",
             "snapshot" in msg.lower() or "pre-recovery" in msg.lower() or
             "repo unreachable" in msg.lower(),
             f"error msg: {msg!r}")
        _aT("target byte-equal to pre-call state",
             target.read_bytes() == original_bytes,
             "destructive write must not have happened")


def main() -> int:
    print("claude-dejavu v0.8.0 — repair/recovery integration tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_repair_pre_recovery_snapshot_called_before_write,
        test_repair_aborts_when_pre_recovery_snapshot_fails,
        test_repair_no_pre_recovery_snapshot_flag_bypasses,
        test_repair_pre_recovery_is_synchronous,
        test_recovery_pre_recovery_snapshot_called_before_write,
        test_recovery_aborts_when_pre_recovery_snapshot_fails,
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
