#!/usr/bin/env python3
"""changelog_distill — one-line CHANGELOG section summarizer.

Used at release time to record a compact "what shipped in vX.Y.Z" line
into the dejavu-owned versions directory (DATA_ROOT/versions/) so the
SessionStart digest hook (G1.c) can surface the last few releases per
project as passive context priming.

Deliberately heuristic + dependency-free — no Markdown parser, no LLM.
We just look for the `## [version]` heading, grab the first non-empty
content line that isn't a sub-heading, and tally Added/Fixed/Changed
buckets to make the summary informative.

Result is hard-capped at 200 chars so a 30-line digest stays well
under the SessionStart token budget.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


SUMMARY_MAX = 200

# `## [0.8.3]` or `## [Unreleased]` (case-insensitive on the bracket body).
_HEADING_RE = re.compile(r"^##\s+\[(?P<version>[^\]]+)\]\s*(?:[—–-]\s*(?P<date>.+))?\s*$")
# `### Added`, `### Fixed`, `### Changed`, `### Motivation`, etc.
_SUB_HEADING_RE = re.compile(r"^###\s+(?P<bucket>[A-Za-z][A-Za-z0-9 _/()-]+?)(?:\s*\(.*\))?\s*$")
# `## ...` of any kind ends the current section.
_ANY_H2_RE = re.compile(r"^##\s+")


def _read_section(path: Path, version: str) -> Optional[list[str]]:
    """Return the raw lines belonging to `## [version]` (heading inclusive,
    next H2 exclusive), or None if not found / unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    lines = text.splitlines()
    want = version.strip().lower()
    out: list[str] = []
    in_section = False
    for ln in lines:
        m = _HEADING_RE.match(ln)
        if m and m.group("version").strip().lower() == want:
            in_section = True
            out.append(ln)
            continue
        if in_section:
            if _ANY_H2_RE.match(ln):
                break
            out.append(ln)
    return out if in_section else None


def _first_meaningful_line(section: list[str]) -> str:
    """Find the first non-empty content line in the section that isn't
    the H2 heading or a sub-heading. Bullet markers are stripped.
    Returns '' if nothing usable."""
    for ln in section[1:]:  # skip the H2
        s = ln.rstrip()
        if not s.strip():
            continue
        if _SUB_HEADING_RE.match(s):
            continue
        # Strip leading bullet / list markers and surrounding whitespace.
        stripped = re.sub(r"^\s*[-*+]\s+", "", s).strip()
        if stripped:
            return stripped
    return ""


def _count_buckets(section: list[str]) -> dict[str, int]:
    """Tally bullet items per `### Bucket` header. Standard buckets are
    Added / Fixed / Changed / Removed / Deprecated / Security but we
    count any header that appears."""
    counts: dict[str, int] = {}
    current: Optional[str] = None
    for ln in section[1:]:
        m = _SUB_HEADING_RE.match(ln)
        if m:
            current = m.group("bucket").strip()
            counts.setdefault(current, 0)
            continue
        if current is None:
            continue
        # Count only top-level bullets: leading whitespace must be 0-1 spaces
        # before the bullet marker. Sub-bullets (4+ spaces) are detail, not items.
        if re.match(r"^[-*+]\s+\S", ln) or re.match(r"^ [-*+]\s+\S", ln):
            counts[current] += 1
    return {k: v for k, v in counts.items() if v > 0}


def _format_summary(version: str, first_line: str, buckets: dict[str, int]) -> str:
    """Assemble the one-line summary, hard-capped at SUMMARY_MAX chars."""
    bucket_part = ""
    if buckets:
        # Standard order: Added, Fixed, Changed, Removed, Deprecated, Security,
        # then anything else alphabetically. Caller wants a stable readout.
        order = ["Added", "Fixed", "Changed", "Removed", "Deprecated", "Security"]
        ordered: list[tuple[str, int]] = []
        for name in order:
            if name in buckets:
                ordered.append((name, buckets[name]))
        for name in sorted(buckets):
            if name not in order:
                ordered.append((name, buckets[name]))
        bucket_part = " [" + ", ".join(f"{n}:{c}" for n, c in ordered) + "]"

    base = f"v{version}: {first_line}" if first_line else f"v{version}:"
    full = base + bucket_part
    if len(full) <= SUMMARY_MAX:
        return full
    # Trim the first_line side first; keep the bucket tally — it's
    # dense, useful, and predictable size.
    keep = SUMMARY_MAX - len(bucket_part) - 1
    if keep < 20:  # bucket part too long; drop it instead
        return full[:SUMMARY_MAX - 1] + "…"
    return base[:keep].rstrip() + "…" + bucket_part


def distill_changelog_entry(path: str | Path, version: str) -> Optional[str]:
    """Return a single-line summary (≤200 chars) of the CHANGELOG section
    for `version`, or None if no such section exists / file is unreadable.

    Example output:
      `v0.8.3: install_restic.MAINTAINER_FP had a typo… [Added:2, Fixed:6]`

    The first content line is the most descriptive bullet/paragraph (in
    practice this is the headline issue or first Added/Fixed item). The
    bracketed tally tells the user the shape of the release at a glance
    without reading the whole section.
    """
    p = Path(path)
    section = _read_section(p, version)
    if section is None:
        return None
    first_line = _first_meaningful_line(section)
    buckets = _count_buckets(section)
    return _format_summary(version.strip(), first_line, buckets)


def latest_version(path: str | Path) -> Optional[str]:
    """Return the most recent ## [version] heading in `path` (i.e. the
    topmost — Keep a Changelog convention is newest-first). Skips
    `[Unreleased]`. Returns None if no version heading is found."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for ln in text.splitlines():
        m = _HEADING_RE.match(ln)
        if not m:
            continue
        v = m.group("version").strip()
        if v.lower() == "unreleased":
            continue
        return v
    return None


def record_version(memory_dir: str | Path, repo_slug: str,
                    version: str, summary: str) -> Path:
    """Idempotently write/update the `vX.Y.Z` line in
    `<memory_dir>/project_<repo_slug>_versions.md`.

    The file format is one `- <summary>` line per release, newest first.
    Re-recording the same version replaces its line in place. Returns
    the path written."""
    mdir = Path(memory_dir)
    mdir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repo_slug).strip("-").lower() or "repo"
    fpath = mdir / f"project_{slug}_versions.md"

    header = f"# {slug} — release log\n\n"
    line = f"- {summary}\n"
    tag = f"v{version.strip()}:"

    existing = ""
    if fpath.is_file():
        try:
            existing = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = ""

    if not existing.strip():
        fpath.write_text(header + line, encoding="utf-8")
        return fpath

    # Strip an existing entry for this version (idempotency).
    kept: list[str] = []
    saw_header = False
    for ln in existing.splitlines():
        if not saw_header and ln.startswith("# "):
            saw_header = True
            kept.append(ln)
            continue
        # Match `- v0.8.3:` (allow optional leading whitespace on the bullet).
        if re.match(rf"^\s*-\s+{re.escape(tag)}", ln):
            continue
        kept.append(ln)

    body = "\n".join(kept).rstrip() + "\n"
    if not saw_header:
        body = header + body
    # Insert the fresh line right after the header (newest first).
    out_lines = body.splitlines(keepends=False)
    insert_at = 0
    for i, ln in enumerate(out_lines):
        if ln.startswith("# "):
            insert_at = i + 1
            # Skip the blank line right after the header if present.
            if insert_at < len(out_lines) and not out_lines[insert_at].strip():
                insert_at += 1
            break
    out_lines.insert(insert_at, line.rstrip("\n"))
    fpath.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    return fpath
