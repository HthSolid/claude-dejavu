#!/usr/bin/env python3
"""claude-dejavu — dejavu-rescue Go binary installer (v0.8.2).

The Go source lives at ``code/rescue/cmd/dejavu-rescue/main.go``. This
installer:

  1. Detects host OS + arch.
  2. If Go is on PATH, builds the binary locally with
     ``GOOS=<os> GOARCH=<arch> go build`` and installs the result into
     ``$DATA_ROOT/bin/dejavu-rescue[.exe]``.
  3. SHA256-stamps the install into ``$DATA_ROOT/rescue-install.json``
     for doctor audits.
  4. If Go is missing → raise :class:`RescueInstallError` with a clear
     install hint. v0.8.2 ships local-build only; prebuilt download
     URLs are TBD (a known limitation documented in CHANGELOG).

Idempotent: skipped if the existing binary already passes
``dejavu-rescue version``.

Cross-compile from a single host (CI use):

    GOOS=darwin  GOARCH=arm64  go build -o dejavu-rescue ...
    GOOS=windows GOARCH=amd64  go build -o dejavu-rescue.exe ...
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


RESCUE_VERSION = "0.8.2"
RESCUE_SOURCE_DIR = (Path(__file__).resolve().parent / "rescue"
                       / "cmd" / "dejavu-rescue")


class RescueInstallError(RuntimeError):
    """rescue binary installer hard error."""


# ─── Platform detection ────────────────────────────────────────────────


def detect_platform() -> tuple[str, str]:
    """Return ``(goos, goarch)`` for the current host.

    Maps Python's ``platform.system()`` / ``platform.machine()`` to
    Go's GOOS/GOARCH constants.
    """
    sys_name = (platform.system() or "").lower()
    if sys_name.startswith("linux"):
        goos = "linux"
    elif sys_name.startswith("darwin"):
        goos = "darwin"
    elif sys_name.startswith("windows"):
        goos = "windows"
    else:
        raise RescueInstallError(f"unsupported OS: {sys_name!r}")
    raw = (platform.machine() or "").lower()
    if raw in ("x86_64", "amd64"):
        goarch = "amd64"
    elif raw in ("aarch64", "arm64"):
        goarch = "arm64"
    else:
        raise RescueInstallError(f"unsupported arch: {raw!r}")
    return goos, goarch


# ─── Idempotency probe ─────────────────────────────────────────────────


def _probe_existing(bin_path: Path) -> Optional[str]:
    if not bin_path.is_file():
        return None
    try:
        r = subprocess.run([str(bin_path), "version"],
                            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    line = (r.stdout or "").strip()
    if "dejavu-rescue" in line:
        return line
    return None


# ─── Build ─────────────────────────────────────────────────────────────


def _go_on_path() -> Optional[Path]:
    g = shutil.which("go")
    return Path(g) if g else None


def _build_local(out_path: Path, goos: str, goarch: str,
                  source_dir: Optional[Path] = None) -> Path:
    """``go build -o <out> ./cmd/dejavu-rescue/``. Caller must have
    verified Go is on PATH."""
    src = source_dir or RESCUE_SOURCE_DIR
    if not src.is_dir():
        raise RescueInstallError(
            f"Go source directory missing: {src}")
    # The `go build` workdir is the package root (..) so the module
    # resolves correctly.
    pkg_root = src.parent.parent  # …/code/rescue
    env = os.environ.copy()
    env["GOOS"] = goos
    env["GOARCH"] = goarch
    # Build with -trimpath for reproducibility, -ldflags='-s -w' to
    # strip debug info (~30% smaller binary).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "go", "build",
        "-trimpath",
        "-ldflags=-s -w",
        "-o", str(out_path),
        "./cmd/dejavu-rescue/",
    ]
    r = subprocess.run(cmd, cwd=pkg_root, env=env,
                        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RescueInstallError(
            f"go build failed (exit {r.returncode}):\n"
            f"{r.stderr.strip()}")
    if not out_path.is_file():
        raise RescueInstallError(
            f"go build reported success but no output at {out_path}")
    try:
        out_path.chmod(0o755)
    except OSError:
        pass
    return out_path


# ─── Orchestrator ──────────────────────────────────────────────────────


def install_rescue_binary(bin_dir: Path,
                              version: str = RESCUE_VERSION,
                              source_dir: Optional[Path] = None,
                              ) -> Path:
    """Install (or skip-if-present) the dejavu-rescue binary into
    ``bin_dir``. Returns the resulting executable path."""
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    goos, goarch = detect_platform()
    out_name = "dejavu-rescue.exe" if goos == "windows" else "dejavu-rescue"
    target = bin_dir / out_name

    # Idempotency.
    if _probe_existing(target) is not None:
        return target

    go = _go_on_path()
    if go is None:
        raise RescueInstallError(
            "Go is required to build dejavu-rescue locally and is not "
            "on PATH.\nInstall Go ≥1.21 from https://go.dev/dl/ then "
            "re-run `scripts/install.py`. Prebuilt binary downloads "
            "are TBD in a future release.")
    _build_local(target, goos, goarch, source_dir=source_dir)

    # Verify by invoking it.
    if _probe_existing(target) is None:
        raise RescueInstallError(
            f"built binary at {target} did not respond to `version`")

    # Audit metadata for doctor.
    data_root = bin_dir.parent  # $DATA_ROOT/bin → $DATA_ROOT
    try:
        sha = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        sha = ""
    meta = data_root / "rescue-install.json"
    try:
        meta.write_text(
            json.dumps({
                "version": version,
                "sha256": sha,
                "installed_at": int(time.time()),
                "os": goos,
                "arch": goarch,
            }, indent=2) + "\n",
            encoding="utf-8")
    except OSError:
        # Non-fatal: install succeeded even if the audit file can't be
        # written.
        pass
    return target


__all__ = [
    "RESCUE_VERSION", "RescueInstallError",
    "detect_platform", "install_rescue_binary",
]
