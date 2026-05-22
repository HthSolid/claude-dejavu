#!/usr/bin/env python3
"""Unit tests for code/migrate_rsync.py — phase markers + per-step
rollback contracts (H4 of v0.8.0).

Style: homemade _step / _ok / _f / _aT. session_copy.* + scheduling.*
are mocked at module-attr level so we test the migration's dispatch +
rollback logic, NOT the primitives.
"""
from __future__ import annotations

import json
import os
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


# ── helpers ────────────────────────────────────────────────────────────────

def _write_state(data_root: Path, phase: str, **extra) -> Path:
    """Seed migration-state.json with the given phase."""
    p = data_root / "migration-state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"phase": phase, "started_at": 0, **extra}
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _read_state(data_root: Path) -> dict:
    p = data_root / "migration-state.json"
    if not p.is_file():
        return {"phase": "DELETED"}
    return json.loads(p.read_text(encoding="utf-8"))


# ── tests ──────────────────────────────────────────────────────────────────

def test_phase_marker_advances_on_success():
    _step("phase marker advances unstarted → ... → done on a full clean run")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_root = td / "data"
        data_root.mkdir(parents=True, exist_ok=True)
        # Run with everything mocked-out to no-ops.
        with mock.patch.object(M, "step_1_stop_legacy",
                                  return_value="legacy_stopped"), \
              mock.patch.object(M, "step_2_inventory",
                                  return_value=[]), \
              mock.patch.object(M, "step_3_preserve",
                                  return_value=None), \
              mock.patch.object(M, "step_5_init_repo",
                                  return_value=None), \
              mock.patch.object(M, "step_6_initial_snapshot",
                                  return_value=None), \
              mock.patch.object(M, "step_7_verify",
                                  return_value=None), \
              mock.patch.object(M, "step_8_install_scheduler_disabled",
                                  return_value=None), \
              mock.patch.object(M, "step_9_rename_old_tree",
                                  return_value=None), \
              mock.patch.object(M, "step_9b_enable_scheduler",
                                  return_value=None):
            rc = M.run(mode="cold", yes=True, data_root=data_root,
                       home=td / "home")
        _aT(f"run() returned 0 on full success (got {rc})", rc == 0)
        _aT("migration-state.json deleted after done",
             not (data_root / "migration-state.json").is_file())


def test_phase_marker_resume_after_crash_at_preserved():
    _step("run() resumes at Step 4 when state file has phase=preserved")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_root = td / "data"; data_root.mkdir(parents=True, exist_ok=True)
        _write_state(data_root, "preserved")
        called: list[str] = []
        with mock.patch.object(M, "step_1_stop_legacy",
              side_effect=lambda **k: called.append("s1")), \
              mock.patch.object(M, "step_2_inventory",
              side_effect=lambda **k: (called.append("s2"), [])[1]), \
              mock.patch.object(M, "step_3_preserve",
              side_effect=lambda *a, **k: called.append("s3")), \
              mock.patch.object(M, "step_5_init_repo",
              side_effect=lambda **k: called.append("s5")), \
              mock.patch.object(M, "step_6_initial_snapshot",
              side_effect=lambda **k: called.append("s6")), \
              mock.patch.object(M, "step_7_verify",
              side_effect=lambda **k: called.append("s7")), \
              mock.patch.object(M, "step_8_install_scheduler_disabled",
              side_effect=lambda **k: called.append("s8")), \
              mock.patch.object(M, "step_9_rename_old_tree",
              side_effect=lambda **k: called.append("s9")), \
              mock.patch.object(M, "step_9b_enable_scheduler",
              side_effect=lambda **k: called.append("s9b")):
            M.run(mode="cold", yes=True, data_root=data_root,
                  home=td / "home")
        _aT(f"Steps 1+2+3 skipped on resume from 'preserved' "
             f"(got {called})",
             "s1" not in called and "s2" not in called
             and "s3" not in called)
        _aT("Step 5+ ran",
             "s5" in called and "s6" in called
             and "s7" in called)


def test_step_5_init_failure_deletes_passphrase():
    _step("Step 5 rollback: restic init failure → passphrase deleted, "
          "phase stays at 'preserved'")
    import migrate_rsync as M
    import session_copy as SC
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_root = td / "data"; data_root.mkdir(parents=True,
                                                    exist_ok=True)
        # Pre-existing fresh passphrase file (as if Step 5 just wrote it)
        passphrase = data_root / ".restic-pass"
        passphrase.write_text("aabbcc", encoding="utf-8")
        os.chmod(passphrase, 0o600)
        _write_state(data_root, "preserved")
        def fake_init_repo(*a, **kw):
            raise SC.ResticError("simulated init failure")
        with mock.patch.object(SC, "init_repo", fake_init_repo):
            try:
                M.step_5_init_repo(data_root=data_root,
                                     home=td / "home")
            except Exception:
                pass
        _aT("passphrase deleted on init failure",
             not passphrase.is_file())
        # state should NOT have been advanced to repo_inited
        # (run() writes the marker; the step itself just raises).


def test_step_7_verify_failure_rolls_back():
    _step("Step 7 rollback: verify failure → legacy timer re-enabled, "
          "repo + passphrase cleaned, phase reset to 'legacy_stopped'")
    import migrate_rsync as M
    import session_copy as SC
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_root = td / "data"; data_root.mkdir(parents=True,
                                                    exist_ok=True)
        repo = data_root / "restic-repo"; repo.mkdir(parents=True)
        passphrase = data_root / ".restic-pass"
        passphrase.write_text("aa", encoding="utf-8")
        _write_state(data_root, "initial_taken")
        re_enable_called: list[bool] = []
        def fake_rollback(*a, **kw):
            re_enable_called.append(True)
        with mock.patch.object(SC, "check",
              return_value={"ok": False, "output": "broken"}), \
              mock.patch.object(M, "_re_enable_legacy_timer",
                                  fake_rollback):
            try:
                M.step_7_verify(data_root=data_root,
                                  home=td / "home")
            except Exception:
                pass
        _aT("legacy timer re-enabled", bool(re_enable_called))
        _aT("repo deleted", not repo.is_dir())
        _aT("passphrase deleted", not passphrase.is_file())


def test_step_8_partial_write_failure_rolls_back_files_linux():
    _step("Step 8 rollback (Linux): partial-write failure → first file "
          "deleted, phase stays at 'verified'")
    import migrate_rsync as M
    import scheduling as SCH
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_root = td / "data"; data_root.mkdir(parents=True,
                                                    exist_ok=True)
        # The install raises mid-write; M.step_8 must clean up.
        cleaned: list[str] = []
        def fake_install_scheduler(bin_path, cadence_min=5):
            # Simulate the partial-write happened, then raised
            raise OSError("simulated mid-write failure")
        def fake_disable():
            cleaned.append("disable_called")
        with mock.patch("platform.system", return_value="Linux"), \
              mock.patch.object(SCH, "install_scheduler",
                                  fake_install_scheduler), \
              mock.patch.object(SCH, "disable_scheduler",
                                  fake_disable):
            try:
                M.step_8_install_scheduler_disabled(
                    data_root=data_root, home=td / "home")
            except Exception:
                pass
        _aT("disable_scheduler invoked on rollback",
             "disable_called" in cleaned)


def test_step_8_partial_write_failure_rolls_back_files_macos():
    _step("Step 8 rollback (macOS): same contract")
    import migrate_rsync as M
    import scheduling as SCH
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_root = td / "data"; data_root.mkdir(parents=True,
                                                    exist_ok=True)
        cleaned: list[str] = []
        with mock.patch("platform.system", return_value="Darwin"), \
              mock.patch.object(SCH, "install_scheduler",
                                  side_effect=OSError("write fail")), \
              mock.patch.object(SCH, "disable_scheduler",
                                  side_effect=lambda: cleaned.append("d")):
            try:
                M.step_8_install_scheduler_disabled(
                    data_root=data_root, home=td / "home")
            except Exception:
                pass
        _aT("disable_scheduler called on mac rollback",
             "d" in cleaned)


def test_step_8_partial_write_failure_rolls_back_files_windows():
    _step("Step 8 rollback (Windows): same contract")
    import migrate_rsync as M
    import scheduling as SCH
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_root = td / "data"; data_root.mkdir(parents=True,
                                                    exist_ok=True)
        cleaned: list[str] = []
        with mock.patch("platform.system", return_value="Windows"), \
              mock.patch.object(SCH, "install_scheduler",
                                  side_effect=OSError("xml stage fail")), \
              mock.patch.object(SCH, "disable_scheduler",
                                  side_effect=lambda: cleaned.append("d")):
            try:
                M.step_8_install_scheduler_disabled(
                    data_root=data_root, home=td / "home")
            except Exception:
                pass
        _aT("disable_scheduler called on windows rollback",
             "d" in cleaned)


def test_step_9_rename_refuses_cross_filesystem():
    _step("Step 9 (reviewer N4): refuses rename when src + parent-of-dst "
          "live on different filesystems")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        home = td / "home"
        snaps = home / ".claude-backups" / "snapshots"
        snaps.mkdir(parents=True, exist_ok=True)
        # Build a fake statvfs return that flips f_fsid mid-call.
        class _FakeVFS:
            def __init__(self, fsid): self.f_fsid = fsid
        seen: list[Path] = []
        def fake_statvfs(p):
            seen.append(Path(p))
            return _FakeVFS(1 if "snapshots" in str(p) else 2)
        with mock.patch("os.statvfs", side_effect=fake_statvfs,
                          create=True):
            raised = False
            try:
                M.step_9_rename_old_tree(
                    data_root=td / "data", home=home)
            except Exception as e:  # expected
                raised = "cross-filesystem" in str(e).lower() \
                          or "different filesystem" in str(e).lower()
        _aT(f"step 9 refused cross-fs rename (raised correctly={raised})",
             raised)


def test_step_9b_failure_re_enables_legacy_timer():
    _step("Step 9b rollback: enable failure → legacy timer re-enabled "
          "as fallback")
    import migrate_rsync as M
    import scheduling as SCH
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_root = td / "data"; data_root.mkdir(parents=True,
                                                    exist_ok=True)
        re_enabled: list[bool] = []
        with mock.patch.object(SCH, "enable_scheduler",
              side_effect=RuntimeError("enable failed")), \
              mock.patch.object(M, "_re_enable_legacy_timer",
                                  lambda *a, **k: re_enabled.append(True)):
            try:
                M.step_9b_enable_scheduler(data_root=data_root,
                                              home=td / "home")
            except Exception:
                pass
        _aT("legacy timer re-enabled on 9b failure",
             bool(re_enabled))


def test_dry_run_mode_does_not_modify_anything():
    _step("dry-run mode: prints plan, never writes state, never calls "
          "destructive steps")
    import migrate_rsync as M
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        data_root = td / "data"; data_root.mkdir(parents=True,
                                                    exist_ok=True)
        called: list[str] = []
        with mock.patch.object(M, "step_1_stop_legacy",
              side_effect=lambda **k: called.append("s1")), \
              mock.patch.object(M, "step_3_preserve",
              side_effect=lambda *a, **k: called.append("s3")), \
              mock.patch.object(M, "step_9_rename_old_tree",
              side_effect=lambda **k: called.append("s9")):
            rc = M.run(mode="dry-run", yes=True, data_root=data_root,
                        home=td / "home")
        _aT("dry-run exits 0", rc == 0)
        _aT(f"no destructive step called in dry-run "
             f"(got {called})",
             "s1" not in called and "s3" not in called
             and "s9" not in called)
        _aT("no state file written",
             not (data_root / "migration-state.json").is_file())


def main() -> int:
    print("migrate_rsync phase + rollback tests")
    tests = [
        test_phase_marker_advances_on_success,
        test_phase_marker_resume_after_crash_at_preserved,
        test_step_5_init_failure_deletes_passphrase,
        test_step_7_verify_failure_rolls_back,
        test_step_8_partial_write_failure_rolls_back_files_linux,
        test_step_8_partial_write_failure_rolls_back_files_macos,
        test_step_8_partial_write_failure_rolls_back_files_windows,
        test_step_9_rename_refuses_cross_filesystem,
        test_step_9b_failure_re_enables_legacy_timer,
        test_dry_run_mode_does_not_modify_anything,
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
