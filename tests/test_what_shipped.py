"""Unit tests for `claude-dejavu what-shipped` (v0.8.4 G1.a).

Drives the CLI via subprocess against a tmp git repo with a fixture
CHANGELOG + a handful of `chore(release): vX.Y.Z` commits. Verifies
parsing, --json output shape, --last cap, missing-CHANGELOG handling.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECALL = ROOT / "code" / "recall.py"

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


SAMPLE_CHANGELOG = """# Changelog

## [0.3.0] — 2026-03-01

### Added

- biggest thing
- another thing

### Fixed

- one bug

## [0.2.0] — 2026-02-01

### Added

- middle version feature

## [0.1.0] — 2026-01-01

### Added

- initial release
"""


def _make_repo(d: Path, with_commits: bool = True) -> None:
    (d / "CHANGELOG.md").write_text(SAMPLE_CHANGELOG, encoding="utf-8")
    if not with_commits:
        return
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", str(d), "init", "-q", "-b", "main"],
                    check=True, env=env)
    subprocess.run(["git", "-C", str(d), "add", "."],
                    check=True, env=env)
    subprocess.run(["git", "-C", str(d), "commit", "-q",
                    "-m", "chore(release): v0.1.0 — initial"],
                    check=True, env=env)
    (d / "x.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "."],
                    check=True, env=env)
    subprocess.run(["git", "-C", str(d), "commit", "-q",
                    "-m", "chore(release): v0.2.0 — mid"],
                    check=True, env=env)
    (d / "x.txt").write_text("b", encoding="utf-8")
    subprocess.run(["git", "-C", str(d), "add", "."],
                    check=True, env=env)
    subprocess.run(["git", "-C", str(d), "commit", "-q",
                    "-m", "chore(release): v0.3.0 — newest"],
                    check=True, env=env)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RECALL), "what-shipped", *args],
        capture_output=True, text=True,
    )


def test_human_output() -> None:
    _step("what-shipped — human-readable default output")
    with tempfile.TemporaryDirectory() as d:
        _make_repo(Path(d))
        r = _run(["--repo", d, "--last", "3"])
        _aT("exit 0", r.returncode == 0,
             f"rc={r.returncode}, stderr={r.stderr!r}")
        out = r.stdout
        _aT("contains v0.3.0", "v0.3.0" in out)
        _aT("contains v0.2.0", "v0.2.0" in out)
        _aT("contains v0.1.0", "v0.1.0" in out)
        # Newest-first ordering.
        i3 = out.find("v0.3.0"); i2 = out.find("v0.2.0"); i1 = out.find("v0.1.0")
        _aT("newest first", 0 < i3 < i2 < i1,
             f"{i3},{i2},{i1}")
        _aT("commit short SHAs surfaced",
             "[" in out and "]" in out)


def test_json_output() -> None:
    _step("what-shipped --json — structured output")
    with tempfile.TemporaryDirectory() as d:
        _make_repo(Path(d))
        r = _run(["--repo", d, "--last", "10", "--json"])
        _aT("exit 0", r.returncode == 0,
             f"rc={r.returncode}, stderr={r.stderr!r}")
        try:
            arr = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            _fail("valid JSON", f"{e}: {r.stdout[:200]}")
            return
        _aT("is a list", isinstance(arr, list))
        _aT("3 entries", len(arr) == 3, f"got {len(arr)}")
        first = arr[0]
        _aT("entry has 'version'", "version" in first)
        _aT("entry has 'date'", "date" in first)
        _aT("entry has 'commit'", "commit" in first)
        _aT("entry has 'summary'", "summary" in first)
        _aT("first is 0.3.0", first["version"] == "0.3.0",
             f"got {first['version']!r}")
        _aT("date populated", first["date"] == "2026-03-01",
             f"got {first['date']!r}")
        _aT("commit short SHA",
             isinstance(first["commit"], str) and len(first["commit"]) >= 4)


def test_last_caps_output() -> None:
    _step("what-shipped --last 1 — caps to most recent")
    with tempfile.TemporaryDirectory() as d:
        _make_repo(Path(d))
        r = _run(["--repo", d, "--last", "1", "--json"])
        _aT("exit 0", r.returncode == 0)
        arr = json.loads(r.stdout)
        _aT("only 1 entry", len(arr) == 1, f"got {len(arr)}")
        _aT("that one is newest", arr[0]["version"] == "0.3.0")


def test_no_changelog_returns_1() -> None:
    _step("what-shipped — missing CHANGELOG.md exits 1")
    with tempfile.TemporaryDirectory() as d:
        # NO CHANGELOG written.
        r = _run(["--repo", d, "--last", "5"])
        _aT("exit 1", r.returncode == 1,
             f"rc={r.returncode}, stderr={r.stderr!r}")
        _aT("emits error message",
             "no CHANGELOG.md" in (r.stderr + r.stdout))


def test_invalid_args_exit_2() -> None:
    _step("what-shipped --last not-an-int → argparse exits 2")
    r = _run(["--last", "abc"])
    _aT("argparse exits 2", r.returncode == 2,
         f"rc={r.returncode}")


def test_no_git_still_works() -> None:
    _step("what-shipped — no git repo, just CHANGELOG.md → still works")
    with tempfile.TemporaryDirectory() as d:
        _make_repo(Path(d), with_commits=False)
        r = _run(["--repo", d, "--last", "3", "--json"])
        _aT("exit 0", r.returncode == 0,
             f"rc={r.returncode}, stderr={r.stderr!r}")
        arr = json.loads(r.stdout)
        _aT("3 entries", len(arr) == 3)
        # No git → commit field is None.
        for entry in arr:
            _aT(f"v{entry['version']} commit is None (no git)",
                 entry["commit"] is None,
                 f"got {entry['commit']!r}")


def main() -> int:
    tests = [
        test_human_output,
        test_json_output,
        test_last_caps_output,
        test_no_changelog_returns_1,
        test_invalid_args_exit_2,
        test_no_git_still_works,
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
