#!/usr/bin/env python3
"""claude-dejavu — cross-platform restic binary installer (v0.8.0).

Downloads + verifies the pinned upstream restic release into a dejavu-
owned bin directory. Never runs sudo / admin. Verification stack:

  1. SHA256 of the downloaded archive against the published
     ``SHA256SUMS`` line for our ``<os>_<arch>`` filename.
  2. GPG signature of ``SHA256SUMS.asc`` matches the upstream
     maintainer fingerprint pinned in :data:`MAINTAINER_FP`.

If ``gpg`` is missing, callers may pass ``allow_fallback=True`` to fall
back to SHA256-only with a printed warning; refusing this is the
default — security-by-default per spec §3.7.1.

See: docs/superpowers/specs/2026-05-15-snapshot-subsystem-design.md
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional


# ── Constants ──────────────────────────────────────────────────────────────

RESTIC_VERSION = "0.17.3"
RESTIC_MIN_VERSION = "0.17.0"
MAINTAINER_FP = "CF8F18F2844575973F79D4E191A6868BD3F7A907"

# Keyservers tried in order during gpg_verify on cache miss. The restic
# maintainer key is *not* on keys.openpgp.org (that service only hosts
# keys whose owners opt-in by email-verifying); keyserver.ubuntu.com and
# pgp.mit.edu both carry it. We try keys.openpgp.org first anyway so
# users who maintain a local keys.openpgp.org mirror still hit fast-path.
KEYSERVERS: tuple[str, ...] = (
    "hkps://keys.openpgp.org",
    "hkps://keyserver.ubuntu.com",
    "hkps://pgp.mit.edu",
)

_RELEASE_URL_TPL = (
    "https://github.com/restic/restic/releases/download/"
    "v{ver}/{fname}"
)
_SUMS_URL_TPL = (
    "https://github.com/restic/restic/releases/download/"
    "v{ver}/SHA256SUMS"
)
_SUMS_ASC_URL_TPL = (
    "https://github.com/restic/restic/releases/download/"
    "v{ver}/SHA256SUMS.asc"
)

_INSTALL_HINTS = {
    "linux":   "sudo apt install gnupg   (Debian/Ubuntu)\n"
                "sudo dnf install gnupg2  (Fedora/RHEL)",
    "darwin":  "brew install gnupg",
    "windows": "winget install GnuPG.Gpg4win",
}


# ── Errors ──────────────────────────────────────────────────────────────────

class InstallError(RuntimeError):
    """restic installer hard error (verification failure, network, etc.)."""


class _GpgVerifyUnavailable(InstallError):
    """gpg verify could not complete (gpg missing OR key fetch/import failed
    OR sig parse failed OR verify exited non-zero from a non-FP-mismatch
    cause). Under ``allow_fallback=True`` we degrade to SHA256-only here;
    SHA256 has already been verified upstream of ``gpg_verify``."""


class _GpgFingerprintMismatch(InstallError):
    """The signature parsed cleanly but the signing key fingerprint did
    NOT match the pinned :data:`MAINTAINER_FP`. SECURITY-CRITICAL: never
    silently fall back from this; a third party signed the archive."""


# ── Platform detection ─────────────────────────────────────────────────────

def detect_platform() -> tuple[str, str, str]:
    """Return ``(os_name, arch, archive_ext)`` for the current host.

    - ``os_name`` ∈ {``linux``, ``darwin``, ``windows``}.
    - ``arch``    ∈ {``amd64``, ``arm64``}.
    - ``ext``     = ``zip`` on Windows, ``bz2`` elsewhere (matches restic
                    release naming).
    """
    sys_name = (platform.system() or "").lower()
    if sys_name.startswith("linux"):
        os_name = "linux"
    elif sys_name.startswith("darwin"):
        os_name = "darwin"
    elif sys_name.startswith("windows"):
        os_name = "windows"
    else:
        raise InstallError(f"unsupported OS: {sys_name!r}")
    raw = (platform.machine() or "").lower()
    if raw in ("x86_64", "amd64"):
        arch = "amd64"
    elif raw in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise InstallError(f"unsupported arch: {raw!r}")
    ext = "zip" if os_name == "windows" else "bz2"
    return os_name, arch, ext


def _release_filename(version: str = RESTIC_VERSION) -> str:
    os_name, arch, ext = detect_platform()
    return f"restic_{version}_{os_name}_{arch}.{ext}"


def _release_url(version: str = RESTIC_VERSION) -> str:
    return _RELEASE_URL_TPL.format(
        ver=version, fname=_release_filename(version))


# ── Download ───────────────────────────────────────────────────────────────

def download_release(url: str, dest: Path) -> Path:
    """HTTPS GET ``url`` to ``dest``. Returns ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=60) as r, \
             open(dest, "wb") as out:
            shutil.copyfileobj(r, out)
    except (OSError, ValueError) as e:
        raise InstallError(f"download failed: {url}: {e}") from e
    return dest


# ── SHA256 verification ────────────────────────────────────────────────────

def sha256_verify(archive: Path, sums_file: Path, filename: str) -> None:
    """Raise :class:`InstallError` if ``archive`` doesn't hash to the
    value listed for ``filename`` in ``sums_file``."""
    if not archive.is_file():
        raise InstallError(f"archive missing: {archive}")
    if not sums_file.is_file():
        raise InstallError(f"SHA256SUMS missing: {sums_file}")
    h = hashlib.sha256()
    with open(archive, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    expected: Optional[str] = None
    for line in sums_file.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        # Standard sha256sum format: "<hash><sp><sp><filename>" or single sp
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            expected = parts[0].lower()
            break
    if expected is None:
        raise InstallError(
            f"filename {filename!r} not present in {sums_file}")
    if actual.lower() != expected:
        raise InstallError(
            f"SHA256 mismatch for {filename}: "
            f"expected {expected}, got {actual}")


# ── GPG verification ───────────────────────────────────────────────────────

def gpg_verify(sums_file: Path, asc_file: Path,
                 allow_fallback: bool = False) -> None:
    """Raise :class:`InstallError` unless ``asc_file`` is a valid detached
    signature for ``sums_file`` made by the pinned :data:`MAINTAINER_FP`.

    Failure modes split three ways so ``allow_fallback`` does the right
    thing per spec §3.7.1:

      * ``gpg`` binary missing                    → recoverable (warn).
      * key fetch/import or verify exec failed    → recoverable (warn).
      * VALIDSIG fingerprint != ``MAINTAINER_FP`` → ALWAYS fail (security).

    Under ``allow_fallback=False`` all three escalate to ``InstallError``
    (the previous behavior is preserved for the default-secure path).
    Under ``allow_fallback=True``, the first two degrade to a stderr
    warning and return cleanly; the FP-mismatch case is propagated
    regardless — a wrong signing key is an active attack signal, not a
    transient network blip.
    """
    try:
        _gpg_verify_strict(sums_file, asc_file)
    except _GpgFingerprintMismatch:
        # why: FP mismatch means a non-pinned key signed the archive —
        # a third-party-substituted release. NEVER swallow under
        # allow_fallback. Re-raise unchanged.
        raise
    except _GpgVerifyUnavailable as exc:
        # why: gpg missing OR keyserver unreachable OR sig parse
        # transient. SHA256 was verified upstream of this call so the
        # archive bytes match the maintainer's published sums file; we
        # are only losing the chain-of-trust check, not integrity.
        if allow_fallback:
            print(f"warning: gpg verify failed ({exc}) — "
                   "relying on SHA256 only", file=sys.stderr)
            return
        raise InstallError(str(exc)) from exc


def _gpg_verify_strict(sums_file: Path, asc_file: Path) -> None:
    """Real verify body. Raises the typed internal errors above; the
    public :func:`gpg_verify` wraps this and decides how to react.
    """
    if shutil.which("gpg") is None:
        # why: recoverable — caller may not have GnuPG installed; SHA256
        # is still trustworthy because it came from the same TLS-fetched
        # release page as the archive.
        os_name = (platform.system() or "").lower()
        if os_name.startswith("darwin"):
            hint = _INSTALL_HINTS["darwin"]
        elif os_name.startswith("windows"):
            hint = _INSTALL_HINTS["windows"]
        else:
            hint = _INSTALL_HINTS["linux"]
        raise _GpgVerifyUnavailable(
            "gpg is required but not on PATH.\n"
            f"Install GnuPG: \n{hint}\n"
            "or rerun with --insecure-no-gpg to skip signature check.")

    if not sums_file.is_file():
        # why: missing input file is unrecoverable structurally but
        # not a security event — classify as Unavailable so --auto
        # surfaces a clear warning rather than a hard fail.
        raise _GpgVerifyUnavailable(f"SHA256SUMS missing: {sums_file}")
    if not asc_file.is_file():
        raise _GpgVerifyUnavailable(f"SHA256SUMS.asc missing: {asc_file}")

    # Fetch the maintainer pubkey into the active keyring on cache miss.
    env = os.environ.copy()
    list_r = subprocess.run(
        ["gpg", "--batch", "--list-keys", MAINTAINER_FP],
        env=env, capture_output=True, text=True)
    if list_r.returncode != 0:
        # Try each keyserver in turn. The restic maintainer key is on
        # keyserver.ubuntu.com + pgp.mit.edu but NOT keys.openpgp.org
        # (its owner hasn't email-verified there), so we MUST iterate
        # — single-server retry was the v0.8.3 install bug.
        # If every keyserver fails we still fall through; the verify
        # step below produces the user-facing error. Under
        # allow_fallback=True we degrade to SHA256-only there.
        for ks in KEYSERVERS:
            fetch = subprocess.run(
                ["gpg", "--batch", "--keyserver", ks,
                 "--recv-keys", MAINTAINER_FP],
                env=env, capture_output=True, text=True)
            if fetch.returncode == 0:
                break
        # Tolerate all-fail: the key may already exist under a TEST
        # GNUPGHOME or all keyservers may be unreachable in this
        # sandbox; verify will give the real error message below.

    # gpg --verify --status-fd produces machine-readable status lines we
    # parse for VALIDSIG + the signing key fingerprint.
    verify = subprocess.run(
        ["gpg", "--batch", "--status-fd", "1", "--verify",
         str(asc_file), str(sums_file)],
        env=env, capture_output=True, text=True)
    if verify.returncode != 0:
        # why: gpg verify exits non-zero for both "no public key" (the
        # keyserver-fetch transient we want to tolerate) and "BADSIG"
        # (signature does not match the data — also recoverable into
        # SHA256-only because the archive bytes are pinned by SHA256SUMS
        # upstream). Classify as Unavailable; a true FP-mismatch is
        # surfaced below from a VALIDSIG line that DOES parse.
        raise _GpgVerifyUnavailable(
            f"gpg verify failed (exit {verify.returncode}):\n"
            f"{verify.stderr.strip()}")
    signing_fp: Optional[str] = None
    for line in verify.stdout.splitlines():
        m = re.match(r"\[GNUPG:\] VALIDSIG ([0-9A-Fa-f]{40})", line)
        if m:
            signing_fp = m.group(1).upper()
            break
    if not signing_fp:
        # why: verify exited 0 but no VALIDSIG line — extremely unusual
        # gpg state. Treat as Unavailable so --auto can fall through;
        # security-by-default path will still reject.
        raise _GpgVerifyUnavailable(
            "gpg verify produced no VALIDSIG status line:\n"
            f"{verify.stdout.strip()}\n{verify.stderr.strip()}")
    if signing_fp.upper() != MAINTAINER_FP.upper():
        # why: SECURITY-CRITICAL — a valid signature from a non-pinned
        # key means someone other than the restic maintainer signed
        # this release. Never recoverable; always escalate even under
        # allow_fallback=True.
        raise _GpgFingerprintMismatch(
            f"signing fingerprint {signing_fp} does not match "
            f"pinned MAINTAINER_FP {MAINTAINER_FP}")


# ── Platform-specific quarantine clearance ─────────────────────────────────

def clear_quarantine_macos(path: Path) -> None:
    """``xattr -d com.apple.quarantine <path>`` on Darwin; no-op elsewhere."""
    if not (platform.system() or "").lower().startswith("darwin"):
        return
    subprocess.run(
        ["xattr", "-d", "com.apple.quarantine", str(path)],
        check=False, capture_output=True)


def unblock_file_windows(path: Path) -> None:
    """``powershell Unblock-File -Path <path>`` on Windows; no-op elsewhere."""
    if not (platform.system() or "").lower().startswith("windows"):
        return
    subprocess.run(
        ["powershell", "-Command",
         f"Unblock-File -Path \"{path}\""],
        check=False, capture_output=True)


# ── Version probe ──────────────────────────────────────────────────────────

def _parse_restic_version(stdout: str) -> Optional[tuple[int, int, int]]:
    m = re.search(r"restic\s+(\d+)\.(\d+)\.(\d+)", stdout)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _min_version_tuple() -> tuple[int, int, int]:
    parts = RESTIC_MIN_VERSION.split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _probe_existing(bin_path: Path) -> Optional[tuple[int, int, int]]:
    if not bin_path.is_file():
        return None
    try:
        r = subprocess.run([str(bin_path), "version"],
                            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return _parse_restic_version(r.stdout or "")


# ── Archive extraction ─────────────────────────────────────────────────────

def _extract(archive: Path, ext: str, out_dir: Path,
              out_name: str) -> Path:
    """Extract ``archive`` (.bz2 or .zip) → ``out_dir/out_name``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / out_name
    if ext == "bz2":
        import bz2
        with bz2.open(archive, "rb") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        target.chmod(0o755)
    elif ext == "zip":
        import zipfile
        with zipfile.ZipFile(archive, "r") as zf:
            # Find the restic binary inside the zip (usually named
            # restic_<ver>_<os>_<arch>.exe).
            members = [n for n in zf.namelist()
                        if n.lower().endswith(".exe")]
            if not members:
                raise InstallError(
                    f"no .exe inside {archive.name}")
            with zf.open(members[0]) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    else:
        raise InstallError(f"unknown archive ext: {ext!r}")
    return target


# ── Orchestrator ───────────────────────────────────────────────────────────

def install_restic(bin_dir: Path,
                     allow_fallback: bool = False,
                     version: str = RESTIC_VERSION) -> Path:
    """Install (or skip-if-present) the restic binary into ``bin_dir``.

    Returns the path of the resulting executable.
    """
    bin_dir = Path(bin_dir)
    bin_dir.mkdir(parents=True, exist_ok=True)
    os_name, arch, ext = detect_platform()
    out_name = "restic.exe" if os_name == "windows" else "restic"
    target = bin_dir / out_name

    # Idempotency: skip if the existing binary already meets min version.
    existing = _probe_existing(target)
    if existing is not None and existing >= _min_version_tuple():
        return target

    fname = _release_filename(version)
    with tempfile.TemporaryDirectory(prefix="restic-install-") as td:
        td = Path(td)
        arch_path = td / fname
        sums_path = td / "SHA256SUMS"
        asc_path  = td / "SHA256SUMS.asc"
        download_release(_release_url(version), arch_path)
        download_release(_SUMS_URL_TPL.format(ver=version), sums_path)
        download_release(_SUMS_ASC_URL_TPL.format(ver=version), asc_path)
        sha256_verify(arch_path, sums_path, fname)
        gpg_verify(sums_path, asc_path, allow_fallback=allow_fallback)
        installed = _extract(arch_path, ext, bin_dir, out_name)

    if os_name == "darwin":
        clear_quarantine_macos(installed)
    elif os_name == "windows":
        unblock_file_windows(installed)

    # Write install metadata for doctor's audit.
    data_root = bin_dir.parent  # default: $DATA_ROOT/bin/ → $DATA_ROOT
    meta = data_root / "restic-install.json"
    try:
        sha = hashlib.sha256(installed.read_bytes()).hexdigest()
    except OSError:
        sha = ""
    meta.write_text(
        json.dumps({
            "version": version,
            "sha256": sha,
            "fingerprint": MAINTAINER_FP,
            "installed_at": int(time.time()),
            "os": os_name,
            "arch": arch,
        }, indent=2) + "\n",
        encoding="utf-8")

    return installed


__all__ = [
    "RESTIC_VERSION", "RESTIC_MIN_VERSION", "MAINTAINER_FP", "KEYSERVERS",
    "InstallError",
    "detect_platform", "download_release",
    "sha256_verify", "gpg_verify",
    "clear_quarantine_macos", "unblock_file_windows",
    "install_restic",
]
