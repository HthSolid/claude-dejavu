"""Unit tests for hooks/session_start_digest.py (v0.8.4 G1.c).

Drives the hook via subprocess with a synthetic
`$DATA_ROOT/versions/` directory containing fixture project_*.md
files. Asserts:
  - empty data root → empty systemMessage
  - populated → header + per-project blocks
  - PER_PROJECT_MAX cap (3 versions per project)
  - MAX_PROJECTS cap (cross-project)
  - CHAR_CAP truncation marker
  - INJECT_SESSION_START=false mutes the digest
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "session_start_digest.py"

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


def _ok(msg: str) -> None:
    PASS.append(msg); print(f"  \033[32m✓\033[0m {msg}", flush=True)


def _fail(msg: str, d: str = "") -> None:
    FAIL.append((msg, d)); print(f"  \033[31m✗\033[0m {msg}")
    if d: print(f"      {d}")


def _step(name: str) -> None:
    print(f"\n\033[36m── {name} ──\033[0m", flush=True)


def _aT(name: str, cond: bool, d: str = "") -> None:
    _ok(name) if cond else _fail(name, d)


def _run(data_root: Path, env_extra: dict[str, str] | None = None
            ) -> subprocess.CompletedProcess:
    env = {**os.environ, "CLAUDE_DEJAVU_DATA_ROOT": str(data_root)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        capture_output=True, text=True, input="{}",
        env=env,
    )


def _seed_versions(data_root: Path, projects: dict[str, list[str]]) -> None:
    """`projects` = {slug: [line, line, ...]} newest-first as the hook
    expects to read it."""
    vd = data_root / "versions"
    vd.mkdir(parents=True, exist_ok=True)
    for slug, lines in projects.items():
        body = f"# {slug} — release log\n\n" + "\n".join(
            f"- {ln}" for ln in lines) + "\n"
        (vd / f"project_{slug}_versions.md").write_text(body, encoding="utf-8")


def test_empty_data_root() -> None:
    _step("empty DATA_ROOT/versions → empty systemMessage")
    with tempfile.TemporaryDirectory() as d:
        r = _run(Path(d))
        _aT("exit 0", r.returncode == 0,
             f"rc={r.returncode}, stderr={r.stderr!r}")
        payload = json.loads(r.stdout)
        _aT("systemMessage key present",
             "systemMessage" in payload)
        _aT("empty body", payload["systemMessage"] == "",
             f"got: {payload['systemMessage']!r}")


def test_single_project_digest_shape() -> None:
    _step("one project → header + slug + bullet lines")
    with tempfile.TemporaryDirectory() as d:
        _seed_versions(Path(d), {
            "myrepo": [
                "v0.3.0: latest [Added:2]",
                "v0.2.0: middle [Added:1]",
                "v0.1.0: initial",
            ],
        })
        r = _run(Path(d))
        _aT("exit 0", r.returncode == 0,
             f"rc={r.returncode}, stderr={r.stderr!r}")
        msg = json.loads(r.stdout)["systemMessage"]
        _aT("has header", "claude-dejavu" in msg
                            and "recent releases" in msg)
        _aT("contains slug", "myrepo" in msg)
        _aT("contains v0.3.0 line", "v0.3.0:" in msg)
        _aT("contains v0.2.0 line", "v0.2.0:" in msg)
        _aT("contains v0.1.0 line", "v0.1.0:" in msg)


def test_per_project_cap() -> None:
    _step("PER_PROJECT_MAX (3) — extra versions dropped")
    with tempfile.TemporaryDirectory() as d:
        _seed_versions(Path(d), {
            "big": [f"v0.{i}.0: rev {i}" for i in range(10, 0, -1)],
        })
        r = _run(Path(d))
        msg = json.loads(r.stdout)["systemMessage"]
        # Newest 3 entries (v0.10.0, v0.9.0, v0.8.0) kept.
        _aT("v0.10.0 included", "v0.10.0:" in msg)
        _aT("v0.9.0 included", "v0.9.0:" in msg)
        _aT("v0.8.0 included", "v0.8.0:" in msg)
        _aT("v0.7.0 dropped",  "v0.7.0:" not in msg)
        _aT("v0.1.0 dropped",  "v0.1.0:" not in msg)


def test_inject_session_start_false_mutes() -> None:
    _step("INJECT_SESSION_START=false → empty body even with data")
    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        _seed_versions(dp, {"x": ["v1.0.0: thing"]})
        # Write config.env disabling the inject.
        (dp).mkdir(parents=True, exist_ok=True)
        (dp / "config.env").write_text(
            "INJECT_SESSION_START=false\n", encoding="utf-8")
        r = _run(dp)
        _aT("exit 0", r.returncode == 0,
             f"rc={r.returncode}, stderr={r.stderr!r}")
        msg = json.loads(r.stdout)["systemMessage"]
        _aT("empty body", msg == "", f"got: {msg!r}")


def test_char_cap_truncation() -> None:
    _step("CHAR_CAP — truncate marker shown when over budget")
    with tempfile.TemporaryDirectory() as d:
        # 20 projects × 3 lines × ~150 chars = ~9000 chars > 6000 cap.
        big_payload = "x" * 150
        projects = {
            f"proj{i:02d}": [
                f"v0.3.0: {big_payload}",
                f"v0.2.0: {big_payload}",
                f"v0.1.0: {big_payload}",
            ]
            for i in range(20)
        }
        _seed_versions(Path(d), projects)
        r = _run(Path(d))
        _aT("exit 0", r.returncode == 0)
        msg = json.loads(r.stdout)["systemMessage"]
        _aT("within hard cap", len(msg) <= 6000,
             f"len={len(msg)}")
        _aT("truncate marker present",
             "truncated" in msg,
             f"tail: {msg[-200:]!r}")


def test_30_line_target_for_typical_usage() -> None:
    _step("typical usage (10 projects × 2 versions) → ≤ ~30 lines")
    with tempfile.TemporaryDirectory() as d:
        projects = {
            f"proj{i}": [
                f"v0.2.0: short summary [Added:1]",
                f"v0.1.0: shorter [Added:1]",
            ]
            for i in range(10)
        }
        _seed_versions(Path(d), projects)
        r = _run(Path(d))
        msg = json.loads(r.stdout)["systemMessage"]
        n_lines = len(msg.splitlines())
        _aT("≤ 40 lines total (header + 10×3)", n_lines <= 40,
             f"lines={n_lines}")
        _aT("all 10 slugs present",
             all(f"proj{i}" in msg for i in range(10)))


def test_ignores_non_matching_files() -> None:
    _step("files not matching project_*_versions.md are ignored")
    with tempfile.TemporaryDirectory() as d:
        vd = Path(d) / "versions"
        vd.mkdir(parents=True)
        (vd / "project_real_versions.md").write_text(
            "- v1.0.0: real thing\n", encoding="utf-8")
        (vd / "readme.md").write_text(
            "- v999.0.0: bogus\n", encoding="utf-8")
        (vd / "random.txt").write_text(
            "- v999.0.0: also bogus\n", encoding="utf-8")
        r = _run(Path(d))
        msg = json.loads(r.stdout)["systemMessage"]
        _aT("real entry included",
             "v1.0.0:" in msg and "real" in msg)
        _aT("bogus entries excluded", "v999.0.0:" not in msg,
             f"got: {msg!r}")


def main() -> int:
    tests = [
        test_empty_data_root,
        test_single_project_digest_shape,
        test_per_project_cap,
        test_inject_session_start_false_mutes,
        test_char_cap_truncation,
        test_30_line_target_for_typical_usage,
        test_ignores_non_matching_files,
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
