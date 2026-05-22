#!/usr/bin/env python3
"""Unit tests for code/install_restic.py — cross-platform restic installer.

Style mirrors tests/test_repair.py: homemade _step/_ok/_f/_aT discipline,
no pytest. Subprocess calls are mocked. GPG verification uses a TEST key
created fresh in a tmp GNUPGHOME per test (no committed secrets).
"""
from __future__ import annotations

import hashlib
import os
import shutil
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


# ── TEST GPG key helpers ───────────────────────────────────────────────────

def _make_test_gpg_key(gnupg_home: Path) -> str:
    """Create a TEST GPG key inside gnupg_home and return its fingerprint."""
    gnupg_home.mkdir(parents=True, exist_ok=True)
    os.chmod(gnupg_home, 0o700)
    batch = (
        "%no-protection\n"
        "Key-Type: RSA\n"
        "Key-Length: 2048\n"
        "Subkey-Type: RSA\n"
        "Subkey-Length: 2048\n"
        "Name-Real: dejavu-test\n"
        "Name-Email: dejavu-test@example.invalid\n"
        "Expire-Date: 0\n"
        "%commit\n"
    )
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    subprocess.run(["gpg", "--batch", "--passphrase", "",
                     "--pinentry-mode", "loopback",
                     "--quick-gen-key",
                     "dejavu-test@example.invalid",
                     "rsa2048", "sign", "0"],
                    env=env, check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(
        ["gpg", "--list-keys", "--with-colons",
         "dejavu-test@example.invalid"],
        env=env, check=True, capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith("fpr:"):
            return line.split(":")[9]
    raise RuntimeError("failed to extract test fingerprint")


def _build_fixture(tmpdir: Path, fname: str, content: bytes,
                    gnupg_home: Path) -> tuple[Path, Path, Path]:
    """Create a fake archive + SHA256SUMS + SHA256SUMS.asc under tmpdir."""
    arch_path = tmpdir / fname
    arch_path.write_bytes(content)
    sha = hashlib.sha256(content).hexdigest()
    sums = tmpdir / "SHA256SUMS"
    sums.write_text(f"{sha}  {fname}\n", encoding="utf-8")
    asc = tmpdir / "SHA256SUMS.asc"
    env = {**os.environ, "GNUPGHOME": str(gnupg_home)}
    subprocess.run(
        ["gpg", "--batch", "--yes", "--armor", "--detach-sign",
         "--output", str(asc), str(sums)],
        env=env, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return arch_path, sums, asc


# ── detect_platform ─────────────────────────────────────────────────────────

def test_detect_os_arch_linux_amd64():
    _step("detect_platform: linux + amd64 → bz2 ext")
    import install_restic
    with mock.patch("platform.system", return_value="Linux"), \
         mock.patch("platform.machine", return_value="x86_64"):
        os_n, arch, ext = install_restic.detect_platform()
    _aT("os=linux", os_n == "linux", os_n)
    _aT("arch=amd64", arch == "amd64", arch)
    _aT("ext=bz2", ext == "bz2", ext)


def test_detect_os_arch_darwin_arm64():
    _step("detect_platform: darwin + arm64 → bz2 ext")
    import install_restic
    with mock.patch("platform.system", return_value="Darwin"), \
         mock.patch("platform.machine", return_value="arm64"):
        os_n, arch, ext = install_restic.detect_platform()
    _aT("os=darwin", os_n == "darwin", os_n)
    _aT("arch=arm64", arch == "arm64", arch)
    _aT("ext=bz2", ext == "bz2", ext)


def test_detect_os_arch_windows_amd64():
    _step("detect_platform: windows + amd64 → zip ext")
    import install_restic
    with mock.patch("platform.system", return_value="Windows"), \
         mock.patch("platform.machine", return_value="AMD64"):
        os_n, arch, ext = install_restic.detect_platform()
    _aT("os=windows", os_n == "windows", os_n)
    _aT("arch=amd64", arch == "amd64", arch)
    _aT("ext=zip", ext == "zip", ext)


# ── sha256_verify ───────────────────────────────────────────────────────────

def test_sha256_verify_match():
    _step("sha256_verify: matching hash passes")
    import install_restic
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fname = "restic_0.17.3_linux_amd64.bz2"
        data = b"fake archive bytes"
        arch = td / fname
        arch.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        sums = td / "SHA256SUMS"
        sums.write_text(f"{sha}  {fname}\n", encoding="utf-8")
        try:
            install_restic.sha256_verify(arch, sums, fname)
            _aT("verify returns without raising", True)
        except install_restic.InstallError as e:
            _aT("verify returns without raising", False, str(e))


def test_sha256_verify_mismatch_raises():
    _step("sha256_verify: mismatched hash raises InstallError")
    import install_restic
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fname = "restic_0.17.3_linux_amd64.bz2"
        data = b"fake archive bytes"
        arch = td / fname
        arch.write_bytes(data)
        sums = td / "SHA256SUMS"
        # Flip checksum to a different value
        sums.write_text(f"{'0' * 64}  {fname}\n", encoding="utf-8")
        try:
            install_restic.sha256_verify(arch, sums, fname)
            _aT("InstallError raised on mismatch", False, "did not raise")
        except install_restic.InstallError:
            _aT("InstallError raised on mismatch", True)


# ── MAINTAINER_FP pin regression ────────────────────────────────────────────

def test_maintainer_fp_canonical_form_and_pinned_value():
    _step("MAINTAINER_FP: 40 upper-hex chars matching the v0.17.x signing key")
    import install_restic
    fp = install_restic.MAINTAINER_FP
    # If you bump RESTIC_VERSION, re-verify by running
    # `gpg --verify SHA256SUMS.asc` against the new release and update
    # both this constant and MAINTAINER_FP.
    EXPECTED_FP = "CF8F18F2844575973F79D4E191A6868BD3F7A907"
    _aT("exactly 40 hex characters",
         isinstance(fp, str) and len(fp) == 40
         and all(c in "0123456789ABCDEFabcdef" for c in fp),
         f"len={len(fp) if isinstance(fp, str) else 'N/A'} fp={fp!r}")
    _aT("canonical uppercase form",
         fp.upper() == fp, f"got {fp!r}")
    _aT("pinned value matches the v0.17.3 signing FP",
         fp == EXPECTED_FP,
         f"FP drifted: got {fp!r}, expected {EXPECTED_FP!r} — "
         f"if you bumped RESTIC_VERSION re-verify the maintainer FP "
         f"and update both this test and code/install_restic.py")


def test_keyservers_list_includes_known_carriers():
    _step("KEYSERVERS: includes ubuntu + mit (which actually carry the key)")
    import install_restic
    ks = install_restic.KEYSERVERS
    _aT("KEYSERVERS is a tuple of >=2 entries",
         isinstance(ks, tuple) and len(ks) >= 2, str(ks))
    joined = " ".join(ks)
    _aT("keyserver.ubuntu.com present (primary carrier)",
         "keyserver.ubuntu.com" in joined, joined)
    _aT("pgp.mit.edu present (secondary carrier)",
         "pgp.mit.edu" in joined, joined)


# ── gpg_verify ──────────────────────────────────────────────────────────────

def test_gpg_verify_with_pinned_fingerprint():
    _step("gpg_verify: signature by pinned TEST fingerprint passes")
    if not shutil.which("gpg"):
        _aT("skipped (no gpg)", True); return
    import install_restic
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        gnupg_home = td / "gnupg"
        fp = _make_test_gpg_key(gnupg_home)
        _, sums, asc = _build_fixture(
            td, "restic_0.17.3_linux_amd64.bz2",
            b"fake archive bytes", gnupg_home)
        # Point install_restic at our TEST key fingerprint + GNUPGHOME
        with mock.patch.object(install_restic, "MAINTAINER_FP", fp), \
             mock.patch.dict(os.environ,
                              {"GNUPGHOME": str(gnupg_home)}):
            try:
                install_restic.gpg_verify(sums, asc,
                                            allow_fallback=False)
                _aT("gpg_verify passes with pinned fingerprint", True)
            except install_restic.InstallError as e:
                _aT("gpg_verify passes with pinned fingerprint",
                     False, str(e))


def test_gpg_fallback_when_gpg_missing():
    _step("gpg_verify: gpg missing — fallback path")
    import install_restic
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        sums = td / "SHA256SUMS"
        sums.write_text("zzz\n", encoding="utf-8")
        asc = td / "SHA256SUMS.asc"
        asc.write_text("not-a-real-sig\n", encoding="utf-8")
        with mock.patch.object(install_restic.shutil, "which",
                                return_value=None):
            # allow_fallback=True succeeds with warning
            try:
                install_restic.gpg_verify(sums, asc,
                                            allow_fallback=True)
                _aT("allow_fallback=True succeeds w/o gpg", True)
            except install_restic.InstallError as e:
                _aT("allow_fallback=True succeeds w/o gpg",
                     False, str(e))
            # allow_fallback=False raises with platform hint
            try:
                install_restic.gpg_verify(sums, asc,
                                            allow_fallback=False)
                _aT("allow_fallback=False raises w/o gpg",
                     False, "did not raise")
            except install_restic.InstallError as e:
                msg = str(e)
                ok = any(h in msg for h in
                          ("apt", "brew", "winget", "gnupg",
                           "GnuPG", "gpg"))
                _aT("error message includes platform install hint",
                     ok, msg)


def test_gpg_fallback_tolerates_verify_failure_but_not_fp_mismatch():
    _step("gpg_verify(allow_fallback=True) — verify-failure ok, "
           "FP-mismatch still fails")
    if not shutil.which("gpg"):
        _aT("skipped (no gpg)", True); return
    import install_restic
    # Case 1: TEST key creates a valid signature but pinned FP is bogus
    # → should ALWAYS fail, even under allow_fallback=True (security).
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        gnupg_home = td / "gnupg"
        _make_test_gpg_key(gnupg_home)
        _, sums, asc = _build_fixture(
            td, "restic_0.17.3_linux_amd64.bz2",
            b"fake archive bytes", gnupg_home)
        with mock.patch.object(install_restic, "MAINTAINER_FP",
                                  "0" * 40), \
             mock.patch.dict(os.environ,
                              {"GNUPGHOME": str(gnupg_home)}):
            try:
                install_restic.gpg_verify(sums, asc,
                                            allow_fallback=True)
                _aT("FP-mismatch fails even under allow_fallback=True",
                     False, "did not raise")
            except install_restic.InstallError as e:
                msg = str(e)
                ok = ("does not match" in msg
                       or "MAINTAINER_FP" in msg)
                _aT("FP-mismatch raises with fingerprint-mismatch message",
                     ok, msg)
    # Case 2: gpg exec exists but verify exits non-zero (bad sig data)
    # → under allow_fallback=True we should warn + return cleanly.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        sums = td / "SHA256SUMS"
        sums.write_text("dummy\n", encoding="utf-8")
        asc = td / "SHA256SUMS.asc"
        asc.write_text("not-a-real-sig\n", encoding="utf-8")
        try:
            install_restic.gpg_verify(sums, asc,
                                        allow_fallback=True)
            _aT("verify-failure tolerated under allow_fallback=True",
                 True)
        except install_restic.InstallError as e:
            _aT("verify-failure tolerated under allow_fallback=True",
                 False, str(e))


def test_macos_quarantine_clear_called():
    _step("clear_quarantine_macos: invokes xattr on Darwin")
    import install_restic
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "restic"
        target.write_bytes(b"x")
        with mock.patch("platform.system", return_value="Darwin"), \
             mock.patch("subprocess.run") as runp:
            install_restic.clear_quarantine_macos(target)
        called = any(
            ("xattr" in (call.args[0] if call.args else
                          call.kwargs.get("args", [""])[0])
             if isinstance(call.args[0] if call.args else
                           call.kwargs.get("args", [""]),
                           (list, tuple))
             else False)
            for call in runp.call_args_list)
        if not called:
            # alternate: check positional
            for call in runp.call_args_list:
                argv = call.args[0] if call.args else None
                if isinstance(argv, (list, tuple)) and "xattr" in argv:
                    called = True; break
        _aT("xattr subprocess invoked",
             called, str(runp.call_args_list))


def test_windows_unblock_file_called():
    _step("unblock_file_windows: invokes powershell Unblock-File on Windows")
    import install_restic
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "restic.exe"
        target.write_bytes(b"x")
        with mock.patch("platform.system", return_value="Windows"), \
             mock.patch("subprocess.run") as runp:
            install_restic.unblock_file_windows(target)
        called = False
        for call in runp.call_args_list:
            argv = call.args[0] if call.args else None
            if not isinstance(argv, (list, tuple)): continue
            joined = " ".join(str(x) for x in argv)
            if "Unblock-File" in joined or "powershell" in argv[0]:
                called = True; break
        _aT("Unblock-File powershell invoked",
             called, str(runp.call_args_list))


# ── install_restic orchestrator ─────────────────────────────────────────────

def test_idempotent_skip_if_already_installed():
    _step("install_restic: skips download if existing restic >= min version")
    import install_restic
    with tempfile.TemporaryDirectory() as td:
        bin_dir = Path(td) / "bin"
        bin_dir.mkdir()
        existing = bin_dir / "restic"
        existing.write_bytes(b"#!/bin/sh\necho 'restic 0.17.3'\n")
        existing.chmod(0o755)
        # Mock the version-probe subprocess
        def fake_run(argv, *a, **kw):
            if isinstance(argv, (list, tuple)) and \
               argv and str(argv[0]).endswith("restic") and \
               len(argv) > 1 and argv[1] == "version":
                return subprocess.CompletedProcess(
                    argv, 0, stdout="restic 0.17.3 compiled with...",
                    stderr="")
            raise AssertionError(
                f"unexpected subprocess call: {argv}")
        with mock.patch("subprocess.run", side_effect=fake_run):
            path = install_restic.install_restic(bin_dir,
                                                    allow_fallback=True)
        _aT("returns existing binary path",
             path == existing, str(path))
        _aT("no network / unzip work was done",
             True)  # implied by the assertion-raising fake_run


def main() -> int:
    print("claude-dejavu v0.8.0 install_restic tests")
    print(f"  ROOT={ROOT}")
    tests = [
        test_detect_os_arch_linux_amd64,
        test_detect_os_arch_darwin_arm64,
        test_detect_os_arch_windows_amd64,
        test_sha256_verify_match,
        test_sha256_verify_mismatch_raises,
        # v0.8.3 post-release polish — FP pin + multi-keyserver regression
        test_maintainer_fp_canonical_form_and_pinned_value,
        test_keyservers_list_includes_known_carriers,
        test_gpg_verify_with_pinned_fingerprint,
        test_gpg_fallback_when_gpg_missing,
        test_gpg_fallback_tolerates_verify_failure_but_not_fp_mismatch,
        test_macos_quarantine_clear_called,
        test_windows_unblock_file_called,
        test_idempotent_skip_if_already_installed,
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
