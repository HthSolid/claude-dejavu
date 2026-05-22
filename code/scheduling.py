#!/usr/bin/env python3
"""claude-dejavu — platform-native cadence scheduler dispatcher (v0.8.0).

Dispatches to:

  * systemd user units on Linux
    (``~/.config/systemd/user/claude-dejavu-session-copy.{timer,service}``)
  * launchd user agent on macOS
    (``~/Library/LaunchAgents/digital.hte.claude-dejavu.session-copy.plist``)
  * Task Scheduler XML on Windows (staged at
    ``%LOCALAPPDATA%\\claude-dejavu\\scheduled-task.xml``)

All three fire ``<bin> session-copy take --tag timer`` every
``cadence_min`` minutes (default 5, matching the legacy chain).

The ``install_scheduler`` function writes the unit files / plist / XML
but DOES NOT enable them. Call ``enable_scheduler()`` separately. This
two-step approach matches spec §3.6 step 8/9b — files are installed
before the rsync rename, but only enabled after the rename succeeds.

See: docs/superpowers/specs/2026-05-15-snapshot-subsystem-design.md §3.7.2
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from string import Template
from typing import Optional


# ── Locations ──────────────────────────────────────────────────────────────

# Resolve via lazy functions so tests can monkey-patch them.

def _scripts_root() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts"


def _linux_unit_dir() -> Path:
    """``~/.config/systemd/user/``."""
    base = os.environ.get("XDG_CONFIG_HOME") or \
            str(Path.home() / ".config")
    p = Path(base) / "systemd" / "user"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _macos_agents_dir() -> Path:
    p = Path.home() / "Library" / "LaunchAgents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _windows_stage_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or \
             str(Path.home() / "AppData" / "Local")
    p = Path(local) / "claude-dejavu"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Template helpers ───────────────────────────────────────────────────────

def _render(tpl_path: Path, **vars: str) -> str:
    raw = tpl_path.read_text(encoding="utf-8")
    return Template(raw).safe_substitute(**vars)


# ── Linux (systemd user) ───────────────────────────────────────────────────

_LINUX_TIMER = "claude-dejavu-session-copy.timer"
_LINUX_SERVICE = "claude-dejavu-session-copy.service"


def _linux_systemd_install(bin_path: Path, cadence_min: int) -> None:
    unit_dir = _linux_unit_dir()
    tpl_dir = _scripts_root() / "systemd"
    timer_tpl = tpl_dir / _LINUX_TIMER
    service_tpl = tpl_dir / _LINUX_SERVICE
    timer_out = unit_dir / _LINUX_TIMER
    service_out = unit_dir / _LINUX_SERVICE
    timer_out.write_text(
        _render(timer_tpl,
                 BIN_PATH=str(bin_path),
                 CADENCE_MIN=str(cadence_min)),
        encoding="utf-8")
    service_out.write_text(
        _render(service_tpl,
                 BIN_PATH=str(bin_path),
                 CADENCE_MIN=str(cadence_min)),
        encoding="utf-8")
    # daemon-reload only — no enable.
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        check=False, capture_output=True)


def _linux_systemd_enable() -> Optional[str]:
    """Enable + start the timer. Returns a warning string if the user
    has no linger configured (timer won't fire when logged out)."""
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", _LINUX_TIMER],
        check=False, capture_output=True)
    # Linger probe.
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    r = subprocess.run(
        ["loginctl", "show-user", user, "--property=Linger"],
        check=False, capture_output=True, text=True)
    if r.returncode == 0 and "Linger=no" in (r.stdout or ""):
        return ("warning: linger is disabled for user "
                f"{user!r}; the session-copy timer will NOT fire when "
                f"you are logged out. Run:\n"
                f"    sudo loginctl enable-linger {user}\n"
                f"to keep snapshots running on a headless or laptop "
                "lid-closed setup.")
    return None


def _linux_systemd_disable() -> None:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", _LINUX_TIMER],
        check=False, capture_output=True)
    for fname in (_LINUX_TIMER, _LINUX_SERVICE):
        p = _linux_unit_dir() / fname
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        check=False, capture_output=True)


def _linux_systemd_status() -> dict:
    unit_dir = _linux_unit_dir()
    installed = (unit_dir / _LINUX_TIMER).is_file() \
                 and (unit_dir / _LINUX_SERVICE).is_file()
    active = False
    if installed:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", _LINUX_TIMER],
            check=False, capture_output=True, text=True)
        active = (r.stdout or "").strip() == "active"
    return {"installed": installed, "active": active,
            "platform": "linux"}


# ── macOS (launchd) ────────────────────────────────────────────────────────

_MAC_PLIST = "digital.hte.claude-dejavu.session-copy.plist"
_MAC_LABEL = "digital.hte.claude-dejavu.session-copy"


def _macos_launchd_install(bin_path: Path, cadence_min: int) -> None:
    agents = _macos_agents_dir()
    tpl = _scripts_root() / "launchd" / _MAC_PLIST
    out = agents / _MAC_PLIST
    log_dir = Path.home() / "Library" / "Logs" / "claude-dejavu"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "session-copy.log"
    out.write_text(
        _render(tpl,
                 BIN_PATH=str(bin_path),
                 CADENCE_SEC=str(cadence_min * 60),
                 LOG_PATH=str(log_path)),
        encoding="utf-8")
    os.chmod(out, 0o644)
    # Clear quarantine xattr on the plist (launchd silently rejects
    # quarantined plists on some macOS versions).
    subprocess.run(
        ["xattr", "-d", "com.apple.quarantine", str(out)],
        check=False, capture_output=True)


def _macos_launchd_enable() -> Optional[str]:
    out = _macos_agents_dir() / _MAC_PLIST
    uid = os.getuid() if hasattr(os, "getuid") else 0
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(out)],
        check=False, capture_output=True)
    return None


def _macos_launchd_disable() -> None:
    out = _macos_agents_dir() / _MAC_PLIST
    uid = os.getuid() if hasattr(os, "getuid") else 0
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{_MAC_LABEL}"],
        check=False, capture_output=True)
    try:
        out.unlink()
    except FileNotFoundError:
        pass


def _macos_launchd_status() -> dict:
    out = _macos_agents_dir() / _MAC_PLIST
    installed = out.is_file()
    active = False
    if installed:
        r = subprocess.run(
            ["launchctl", "list", _MAC_LABEL],
            check=False, capture_output=True, text=True)
        active = (r.returncode == 0)
    return {"installed": installed, "active": active,
            "platform": "darwin"}


# ── Windows (Task Scheduler) ───────────────────────────────────────────────

_WIN_XML = "scheduled-task.xml"
_WIN_TASK_NAME = "ClaudeDejavu_SessionCopy"


def _windows_schtasks_install(bin_path: Path, cadence_min: int) -> None:
    stage = _windows_stage_dir()
    tpl = _scripts_root() / "windows" / "scheduled-task-template.xml"
    rendered = _render(tpl,
                         BIN_PATH=str(bin_path),
                         CADENCE_MIN=str(cadence_min))
    # Task Scheduler XML must be UTF-16 LE with BOM per Microsoft's docs.
    out = stage / _WIN_XML
    with open(out, "wb") as f:
        f.write(b"\xff\xfe")  # UTF-16 LE BOM
        f.write(rendered.encode("utf-16-le"))


def _windows_schtasks_enable() -> Optional[str]:
    stage = _windows_stage_dir()
    xml_path = stage / _WIN_XML
    subprocess.run(
        ["schtasks", "/Create",
         "/XML", str(xml_path),
         "/TN", _WIN_TASK_NAME,
         "/IT", "/F"],
        check=False, capture_output=True)
    return None


def _windows_schtasks_disable() -> None:
    subprocess.run(
        ["schtasks", "/Delete", "/TN", _WIN_TASK_NAME, "/F"],
        check=False, capture_output=True)
    try:
        (_windows_stage_dir() / _WIN_XML).unlink()
    except FileNotFoundError:
        pass


def _windows_schtasks_status() -> dict:
    stage = _windows_stage_dir()
    installed = (stage / _WIN_XML).is_file()
    active = False
    if installed:
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", _WIN_TASK_NAME],
            check=False, capture_output=True, text=True)
        active = (r.returncode == 0)
    return {"installed": installed, "active": active,
            "platform": "windows"}


# ── Public dispatcher ──────────────────────────────────────────────────────

def _current_platform() -> str:
    sys_name = (platform.system() or "").lower()
    if sys_name.startswith("linux"):
        return "linux"
    if sys_name.startswith("darwin"):
        return "darwin"
    if sys_name.startswith("windows"):
        return "windows"
    return sys_name


def install_scheduler(bin_path: Path, cadence_min: int = 5) -> None:
    """Write the platform's scheduler unit/plist/XML. Does NOT enable."""
    bin_path = Path(bin_path)
    p = _current_platform()
    if p == "linux":
        _linux_systemd_install(bin_path, cadence_min)
    elif p == "darwin":
        _macos_launchd_install(bin_path, cadence_min)
    elif p == "windows":
        _windows_schtasks_install(bin_path, cadence_min)
    else:
        raise RuntimeError(f"unsupported platform: {p!r}")


def enable_scheduler() -> Optional[str]:
    """Enable + start the previously-installed scheduler. Returns an
    informational warning string if the platform has a soft caveat
    (e.g. linger=no on Linux)."""
    p = _current_platform()
    if p == "linux":
        return _linux_systemd_enable()
    if p == "darwin":
        return _macos_launchd_enable()
    if p == "windows":
        return _windows_schtasks_enable()
    raise RuntimeError(f"unsupported platform: {p!r}")


def disable_scheduler() -> None:
    p = _current_platform()
    if p == "linux":
        _linux_systemd_disable()
    elif p == "darwin":
        _macos_launchd_disable()
    elif p == "windows":
        _windows_schtasks_disable()
    else:
        raise RuntimeError(f"unsupported platform: {p!r}")


def scheduler_status() -> dict:
    p = _current_platform()
    if p == "linux":
        return _linux_systemd_status()
    if p == "darwin":
        return _macos_launchd_status()
    if p == "windows":
        return _windows_schtasks_status()
    return {"installed": False, "active": False, "platform": p}


__all__ = [
    "install_scheduler", "enable_scheduler",
    "disable_scheduler", "scheduler_status",
    "_linux_systemd_install", "_linux_systemd_enable",
    "_linux_systemd_disable", "_linux_systemd_status",
    "_macos_launchd_install", "_macos_launchd_enable",
    "_macos_launchd_disable", "_macos_launchd_status",
    "_windows_schtasks_install", "_windows_schtasks_enable",
    "_windows_schtasks_disable", "_windows_schtasks_status",
    "_linux_unit_dir", "_macos_agents_dir", "_windows_stage_dir",
]
