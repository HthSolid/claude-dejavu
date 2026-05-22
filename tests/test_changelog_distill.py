"""Unit tests for code/changelog_distill.py — heuristic CHANGELOG
section parser + idempotent versions-file recorder.

Pure stdlib, infra-free. Fixtures are inline strings written to tmp."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))

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


SAMPLE = """# Changelog

## [0.8.3] — 2026-05-17

### Fixed (post-release polish, 2026-05-17 evening)

- `install_restic.MAINTAINER_FP` had a typo in last 10 hex chars
  (BD3F1A6E9 → BD3F7A907). Blocked restic install.
- `install_restic.gpg_verify` only tried `keys.openpgp.org`.
- `pg_rescue.scan` reported phantom corruption.

### Added (post-release polish, 2026-05-17 evening)

- `claude-dejavu session-copy reclaim-legacy` — deletes the tree.
- `scripts/install.py [7/9]` now surfaces restic repo path.

## [0.8.2] — 2026-05-16

### Added

- `rescue-shard` subcommand recovers torn Weaviate LSM segments.

### Fixed

- WAL replay segfault.

## [Unreleased]

### Added

- Future stuff.
"""


def test_distill_first_line_and_buckets() -> None:
    _step("distill_changelog_entry — first line + bucket tally")
    import changelog_distill as cd
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CHANGELOG.md"
        p.write_text(SAMPLE, encoding="utf-8")
        out = cd.distill_changelog_entry(p, "0.8.3")
        _aT("returns non-None for known version", out is not None)
        _aT("starts with v0.8.3:", out and out.startswith("v0.8.3:"),
             f"got: {out!r}")
        _aT("bucket tally present", "[" in (out or "") and "]" in (out or ""))
        _aT("Fixed:3 counted",
             "Fixed:3" in (out or ""), f"got: {out!r}")
        _aT("Added:2 counted",
             "Added:2" in (out or ""), f"got: {out!r}")
        _aT("≤200 chars", len(out or "") <= 200,
             f"got len={len(out or '')}")


def test_distill_missing_version() -> None:
    _step("distill_changelog_entry — missing version returns None")
    import changelog_distill as cd
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CHANGELOG.md"
        p.write_text(SAMPLE, encoding="utf-8")
        out = cd.distill_changelog_entry(p, "9.9.9")
        _aT("None for missing version", out is None,
             f"got: {out!r}")


def test_distill_unreadable_file() -> None:
    _step("distill_changelog_entry — unreadable file returns None")
    import changelog_distill as cd
    out = cd.distill_changelog_entry("/nonexistent/CHANGELOG.md", "1.0.0")
    _aT("None for missing path", out is None)


def test_distill_truncation_cap() -> None:
    _step("distill_changelog_entry — long first line truncated to ≤200")
    import changelog_distill as cd
    long_line = "x" * 500
    text = f"## [1.2.3] — 2026-01-01\n\n### Added\n\n- {long_line}\n"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "C.md"
        p.write_text(text, encoding="utf-8")
        out = cd.distill_changelog_entry(p, "1.2.3")
        _aT("returns something", out is not None)
        _aT("≤200 chars even on huge input", len(out or "") <= 200,
             f"len={len(out or '')}")
        _aT("ends with ellipsis marker",
             (out or "").rstrip().endswith("…") or "…" in (out or ""),
             f"got: {out!r}")


def test_latest_version_skips_unreleased() -> None:
    _step("latest_version — returns newest, skips [Unreleased]")
    import changelog_distill as cd
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CHANGELOG.md"
        p.write_text(SAMPLE, encoding="utf-8")
        v = cd.latest_version(p)
        _aT("returns 0.8.3 (skips Unreleased)", v == "0.8.3",
             f"got: {v!r}")


def test_record_version_creates_file() -> None:
    _step("record_version — creates a fresh versions file")
    import changelog_distill as cd
    with tempfile.TemporaryDirectory() as d:
        fp = cd.record_version(d, "my-repo", "1.0.0",
                                "v1.0.0: initial release [Added:5]")
        _aT("file created", fp.is_file())
        body = fp.read_text(encoding="utf-8")
        _aT("filename slugified", fp.name == "project_my-repo_versions.md",
             f"got: {fp.name!r}")
        _aT("contains the summary line",
             "v1.0.0: initial release" in body)
        _aT("has header", body.startswith("# my-repo"))


def test_record_version_idempotent() -> None:
    _step("record_version — idempotent on re-record")
    import changelog_distill as cd
    with tempfile.TemporaryDirectory() as d:
        cd.record_version(d, "x", "1.0.0", "v1.0.0: first [Added:1]")
        cd.record_version(d, "x", "1.0.0", "v1.0.0: updated [Added:2]")
        cd.record_version(d, "x", "1.0.0", "v1.0.0: final [Added:3]")
        fp = Path(d) / "project_x_versions.md"
        body = fp.read_text(encoding="utf-8")
        _aT("only one v1.0.0 line",
             body.count("v1.0.0:") == 1,
             f"count={body.count('v1.0.0:')}")
        _aT("kept the latest content",
             "final" in body and "first" not in body and "updated" not in body)


def test_record_version_multiple_versions() -> None:
    _step("record_version — multiple versions stack newest-first")
    import changelog_distill as cd
    with tempfile.TemporaryDirectory() as d:
        cd.record_version(d, "x", "1.0.0", "v1.0.0: first")
        cd.record_version(d, "x", "1.1.0", "v1.1.0: second")
        cd.record_version(d, "x", "2.0.0", "v2.0.0: third")
        fp = Path(d) / "project_x_versions.md"
        body = fp.read_text(encoding="utf-8")
        # Each line present.
        for tag in ("v1.0.0:", "v1.1.0:", "v2.0.0:"):
            _aT(f"{tag} present", tag in body)
        # Most recent is closest to the top.
        idx_2 = body.find("v2.0.0:")
        idx_11 = body.find("v1.1.0:")
        idx_1 = body.find("v1.0.0:")
        _aT("newest at top",
             0 < idx_2 < idx_11 < idx_1,
             f"idxs={idx_2},{idx_11},{idx_1}")


def test_record_version_slug_sanitization() -> None:
    _step("record_version — sanitizes weird repo slugs")
    import changelog_distill as cd
    with tempfile.TemporaryDirectory() as d:
        fp = cd.record_version(d, "Foo/Bar Baz!", "1.0.0",
                                "v1.0.0: ok")
        # Lowercased, slashes/spaces/specials → dashes.
        _aT("slug sanitized",
             "foo-bar-baz" in fp.name,
             f"got: {fp.name!r}")


def main() -> int:
    tests = [
        test_distill_first_line_and_buckets,
        test_distill_missing_version,
        test_distill_unreadable_file,
        test_distill_truncation_cap,
        test_latest_version_skips_unreleased,
        test_record_version_creates_file,
        test_record_version_idempotent,
        test_record_version_multiple_versions,
        test_record_version_slug_sanitization,
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
