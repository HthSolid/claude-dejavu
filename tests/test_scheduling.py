#!/usr/bin/env python3
"""Unit tests for code/scheduling.py — cross-platform cadence dispatcher.

Heavy use of mock.patch on platform.system + subprocess.run so the tests
run anywhere. Style: homemade _step / _ok / _f / _aT (no pytest).
"""
from __future__ import annotations

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


BIN = Path("/usr/local/bin/claude-dejavu")


# ── Dispatch ────────────────────────────────────────────────────────────────

def test_detect_platform_dispatch_linux():
    _step("install_scheduler: Linux → calls _linux_systemd_install")
    import scheduling
    with mock.patch("platform.system", return_value="Linux"), \
         mock.patch.object(scheduling, "_linux_systemd_install") as f:
        scheduling.install_scheduler(BIN, cadence_min=5)
    _aT("called once", f.call_count == 1, str(f.call_args_list))


def test_detect_platform_dispatch_darwin():
    _step("install_scheduler: Darwin → calls _macos_launchd_install")
    import scheduling
    with mock.patch("platform.system", return_value="Darwin"), \
         mock.patch.object(scheduling, "_macos_launchd_install") as f:
        scheduling.install_scheduler(BIN, cadence_min=5)
    _aT("called once", f.call_count == 1)


def test_detect_platform_dispatch_windows():
    _step("install_scheduler: Windows → calls _windows_schtasks_install")
    import scheduling
    with mock.patch("platform.system", return_value="Windows"), \
         mock.patch.object(scheduling, "_windows_schtasks_install") as f:
        scheduling.install_scheduler(BIN, cadence_min=5)
    _aT("called once", f.call_count == 1)


# ── Linux systemd ───────────────────────────────────────────────────────────

def test_linux_systemd_install_writes_files():
    _step("_linux_systemd_install: writes timer + service with vars "
           "substituted; daemon-reload called")
    import scheduling
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        with mock.patch.object(scheduling, "_linux_unit_dir",
                                return_value=td), \
             mock.patch("subprocess.run") as runp:
            scheduling._linux_systemd_install(BIN, cadence_min=5)
        timer = td / "claude-dejavu-session-copy.timer"
        service = td / "claude-dejavu-session-copy.service"
        _aT("timer written", timer.is_file(), str(timer))
        _aT("service written", service.is_file(), str(service))
        timer_txt = timer.read_text(encoding="utf-8")
        service_txt = service.read_text(encoding="utf-8")
        _aT("CADENCE_MIN substituted in timer",
             "5min" in timer_txt and "${CADENCE_MIN}" not in timer_txt,
             timer_txt[:200])
        _aT("BIN_PATH substituted in service",
             str(BIN) in service_txt
             and "${BIN_PATH}" not in service_txt,
             service_txt[:200])
        # daemon-reload invoked
        daemon_reload = False
        for call in runp.call_args_list:
            argv = call.args[0] if call.args else None
            if isinstance(argv, (list, tuple)) and "daemon-reload" in argv:
                daemon_reload = True; break
        _aT("systemctl daemon-reload invoked",
             daemon_reload, str(runp.call_args_list))


def test_linux_systemd_install_does_not_enable():
    _step("_linux_systemd_install: does NOT call 'enable --now'")
    import scheduling
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        with mock.patch.object(scheduling, "_linux_unit_dir",
                                return_value=td), \
             mock.patch("subprocess.run") as runp:
            scheduling._linux_systemd_install(BIN, cadence_min=5)
        enabled = False
        for call in runp.call_args_list:
            argv = call.args[0] if call.args else None
            if not isinstance(argv, (list, tuple)): continue
            if "enable" in argv and "--now" in argv:
                enabled = True; break
        _aT("no 'enable --now' subprocess call",
             not enabled, str(runp.call_args_list))


def test_linger_warning_when_loginctl_returns_no():
    _step("_linux_systemd_enable: warns when loginctl Linger=no")
    import scheduling
    def fake_run(argv, *a, **kw):
        if isinstance(argv, (list, tuple)) \
                and "loginctl" in argv[0]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="Linger=no\n", stderr="")
        return subprocess.CompletedProcess(argv, 0,
                                              stdout="", stderr="")
    with mock.patch("subprocess.run", side_effect=fake_run):
        msg = scheduling._linux_systemd_enable()
    _aT("Linger warning surfaced",
         msg is not None and "linger" in msg.lower(),
         str(msg))


# ── macOS launchd ───────────────────────────────────────────────────────────

def test_macos_plist_correct_perms():
    _step("_macos_launchd_install: plist has mode 0o644")
    import scheduling
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        with mock.patch.object(scheduling, "_macos_agents_dir",
                                return_value=td), \
             mock.patch("subprocess.run"):
            scheduling._macos_launchd_install(BIN, cadence_min=5)
        plist = td / "digital.hte.claude-dejavu.session-copy.plist"
        _aT("plist written", plist.is_file(), str(plist))
        mode = plist.stat().st_mode & 0o777
        _aT(f"mode 0o644 (got 0o{mode:o})",
             mode == 0o644)


def test_macos_clears_quarantine_on_plist():
    _step("_macos_launchd_install: xattr -d com.apple.quarantine "
           "invoked on plist")
    import scheduling
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        with mock.patch.object(scheduling, "_macos_agents_dir",
                                return_value=td), \
             mock.patch("subprocess.run") as runp:
            scheduling._macos_launchd_install(BIN, cadence_min=5)
        xattr_called = False
        for call in runp.call_args_list:
            argv = call.args[0] if call.args else None
            if isinstance(argv, (list, tuple)) and "xattr" in argv \
               and "com.apple.quarantine" in argv:
                xattr_called = True; break
        _aT("xattr -d com.apple.quarantine invoked",
             xattr_called, str(runp.call_args_list))


# ── Windows schtasks ───────────────────────────────────────────────────────

def test_windows_schtasks_xml_has_IT_flag():
    _step("_windows_schtasks_install: generated XML/registration uses "
           "/IT (interactive-only) semantic")
    import scheduling
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        with mock.patch.object(scheduling, "_windows_stage_dir",
                                return_value=td), \
             mock.patch("subprocess.run") as runp:
            scheduling._windows_schtasks_install(BIN, cadence_min=5)
        xml_path = td / "scheduled-task.xml"
        _aT("xml staged", xml_path.is_file(), str(xml_path))
        body = xml_path.read_text(encoding="utf-16-le",
                                     errors="replace")
        if "ClaudeDejavu_SessionCopy" not in body:
            body = xml_path.read_text(encoding="utf-8",
                                          errors="replace")
        # The semantic is encoded either in the XML (InteractiveToken)
        # or in the eventual schtasks /Create /IT argv. Both are
        # acceptable proofs. The install step writes the XML but
        # leaves registration to _windows_schtasks_enable.
        has_it_in_xml = ("InteractiveToken" in body)
        _aT("XML carries InteractiveToken semantic",
             has_it_in_xml, body[:200])


# ── disable inverse ────────────────────────────────────────────────────────

def test_disable_scheduler_inverse_of_enable():
    _step("disable_scheduler: removes the unit files written by install")
    import scheduling
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        with mock.patch("platform.system", return_value="Linux"), \
             mock.patch.object(scheduling, "_linux_unit_dir",
                                return_value=td), \
             mock.patch("subprocess.run"):
            scheduling.install_scheduler(BIN, cadence_min=5)
            files_after_install = sorted(p.name for p in td.iterdir())
            scheduling.disable_scheduler()
            files_after_disable = sorted(p.name for p in td.iterdir())
        _aT("install wrote files",
             len(files_after_install) >= 2,
             str(files_after_install))
        _aT("disable removed installed files",
             len(files_after_disable) == 0,
             f"remain: {files_after_disable}")


# ── Cross-platform round-trip parametrized (per reviewer C5) ──────────────

def test_install_status_disable_roundtrip_each_platform():
    _step("install → status → disable round-trip on Linux, Darwin, Windows")
    import scheduling
    for sysname, agent_attr, stage_attr in (
        ("Linux", "_linux_unit_dir", None),
        ("Darwin", "_macos_agents_dir", None),
        ("Windows", "_windows_stage_dir", "_windows_stage_dir"),
    ):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            patches = [
                mock.patch("platform.system", return_value=sysname),
                mock.patch("subprocess.run"),
            ]
            if sysname == "Linux":
                patches.append(mock.patch.object(scheduling,
                                                    "_linux_unit_dir",
                                                    return_value=td))
            elif sysname == "Darwin":
                patches.append(mock.patch.object(scheduling,
                                                    "_macos_agents_dir",
                                                    return_value=td))
            else:
                patches.append(mock.patch.object(scheduling,
                                                    "_windows_stage_dir",
                                                    return_value=td))
            for p in patches:
                p.start()
            try:
                scheduling.install_scheduler(BIN, cadence_min=7)
                st = scheduling.scheduler_status()
                scheduling.disable_scheduler()
                st2 = scheduling.scheduler_status()
            finally:
                for p in patches:
                    p.stop()
            _aT(f"{sysname}: status post-install reports installed=true",
                 bool(st.get("installed")), f"{sysname}: {st}")
            _aT(f"{sysname}: status post-disable reports installed=false",
                 not st2.get("installed"), f"{sysname}: {st2}")


def main() -> int:
    print("claude-dejavu v0.8.0 scheduling tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_detect_platform_dispatch_linux,
        test_detect_platform_dispatch_darwin,
        test_detect_platform_dispatch_windows,
        test_linux_systemd_install_writes_files,
        test_linux_systemd_install_does_not_enable,
        test_linger_warning_when_loginctl_returns_no,
        test_macos_plist_correct_perms,
        test_macos_clears_quarantine_on_plist,
        test_windows_schtasks_xml_has_IT_flag,
        test_disable_scheduler_inverse_of_enable,
        test_install_status_disable_roundtrip_each_platform,
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
