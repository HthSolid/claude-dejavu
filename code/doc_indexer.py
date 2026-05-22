#!/usr/bin/env python3
"""
claude-dejavu — markdown documentation indexer.

Walks .md files into heading-anchored chunks for embedding in the
ClaudeDejavuDoc Weaviate class. See:
  docs/superpowers/specs/2026-05-08-dejavu-docs-embedding-design.md
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^```")


@dataclass
class Section:
    """One heading-anchored section of a markdown file."""
    heading_path: str   # "Top > Section A > Subsection A.1"
    level: int          # 1..6 (the heading's own level), or 0 for synthetic
    body: str           # everything between this heading and the next at
                        # the same level or shallower (including subheadings)


def walk_headings(text: str) -> Iterator[Section]:
    """Yield Section objects walking the heading tree of `text`.

    A "section" is the body from a heading up to the next heading at
    the same level or shallower. Subheadings remain in the parent's
    body (they'll be their own Section too — sections overlap by
    design so that a query against a parent heading still returns
    its full subtree).

    Empty file → no sections.
    File with no headings → one synthetic section labeled "(no heading)".
    Code fences (``` blocks) suppress heading detection inside them.
    """
    if not text.strip():
        return

    lines = text.splitlines(keepends=True)
    in_fence = False
    # Pre-pass: find all (line_idx, level, title) tuples.
    headings: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            headings.append((idx, level, title))

    if not headings:
        yield Section(heading_path="(no heading)", level=0, body=text)
        return

    # Build path-stack as we walk.
    stack: list[tuple[int, str]] = []  # (level, title)
    for hi, (line_idx, level, title) in enumerate(headings):
        # Pop deeper levels off the stack.
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        heading_path = " > ".join(t for _, t in stack)

        # Find the end: next heading at level <= this level, or EOF.
        end_idx = len(lines)
        for j in range(hi + 1, len(headings)):
            if headings[j][1] <= level:
                end_idx = headings[j][0]
                break

        body = "".join(lines[line_idx:end_idx])
        yield Section(heading_path=heading_path, level=level, body=body)


# Coarse token estimate: ~4 chars per token (good enough for the
# sub-split threshold; this is not for billing).
_CHARS_PER_TOKEN = 4
_MAX_TOKENS_PER_CHUNK = 1500
_MAX_SUMMARY_CHARS = 200


@dataclass
class Chunk:
    """One emitted chunk ready for embedding."""
    heading_path: str
    chunk_idx: int
    one_line_summary: str
    content: str
    content_hash: str  # sha256 hex of content bytes


def _first_non_blank_line(body: str) -> str:
    for line in body.splitlines():
        # Skip heading lines entirely — Section.body includes its own
        # heading at the top, but the summary should pull the first
        # BODY line, not echo the heading.
        if _HEADING_RE.match(line):
            continue
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _make_summary(heading_path: str, body: str) -> str:
    """heading + first non-blank line of body, truncated."""
    leaf = heading_path.split(" > ")[-1] if heading_path else ""
    first = _first_non_blank_line(body)
    if first:
        summary = f"{leaf} — {first}" if leaf else first
    else:
        summary = leaf
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[: _MAX_SUMMARY_CHARS - 1] + "…"
    return summary


def _split_paragraphs(body: str, max_chars: int) -> list[str]:
    """Greedy paragraph-boundary split, never exceeding max_chars per piece.
    Falls back to hard-cut at max_chars if a single paragraph is too big."""
    paragraphs = body.split("\n\n")
    pieces, current = [], ""
    for p in paragraphs:
        candidate = (current + "\n\n" + p).strip() if current else p
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                pieces.append(current)
            if len(p) <= max_chars:
                current = p
            else:
                # Hard-cut.
                for i in range(0, len(p), max_chars):
                    pieces.append(p[i:i + max_chars])
                current = ""
    if current:
        pieces.append(current)
    return pieces


def chunk_file(path: Path) -> Iterator[Chunk]:
    """Yield Chunk objects for the markdown file at `path`.

    Heading-anchored sections; sections larger than ~1500 tokens are
    sub-split at paragraph boundaries (each piece inherits the same
    heading_path with a suffix [1/N], [2/N], …).
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return

    max_chars = _MAX_TOKENS_PER_CHUNK * _CHARS_PER_TOKEN  # ~6000
    idx = 0
    for section in walk_headings(text):
        body = section.body
        if len(body) <= max_chars:
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            yield Chunk(
                heading_path=section.heading_path,
                chunk_idx=idx,
                one_line_summary=_make_summary(section.heading_path, body),
                content=body,
                content_hash=digest,
            )
            idx += 1
            continue
        pieces = _split_paragraphs(body, max_chars)
        n = len(pieces)
        for i, piece in enumerate(pieces, start=1):
            suffix_path = f"{section.heading_path} [{i}/{n}]"
            digest = hashlib.sha256(piece.encode("utf-8")).hexdigest()
            yield Chunk(
                heading_path=suffix_path,
                chunk_idx=idx,
                one_line_summary=_make_summary(suffix_path, piece),
                content=piece,
                content_hash=digest,
            )
            idx += 1


_DEFAULT_IGNORES = [
    ".dejavu/", "node_modules/", "target/", "dist/", "build/",
    ".git/", "__pycache__/", ".venv/", "vendor/",
    "*.min.md",
]


def _load_dejavuignore(repo_root: Path) -> list[str]:
    """Return the union of built-in defaults + the user's .dejavuignore."""
    patterns = list(_DEFAULT_IGNORES)
    user_file = repo_root / ".dejavuignore"
    if user_file.is_file():
        for line in user_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def _match_ignore(rel_path: str, patterns: list[str]) -> bool:
    """Apply gitignore-flavored matching. Naive but covers our cases:
    trailing-/ matches a directory prefix; otherwise glob via fnmatch.
    For full gitignore semantics, swap in `pathspec` later — not pulled
    in as a dep for v0.7."""
    import fnmatch
    for pat in patterns:
        if pat.endswith("/"):
            if rel_path == pat[:-1] or rel_path.startswith(pat):
                return True
            # Match nested: "node_modules/foo/bar.md"
            if f"/{pat}" in f"/{rel_path}/" or rel_path.split("/", 1)[0] + "/" == pat:
                return True
        elif fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(Path(rel_path).name, pat):
            return True
    return False


def walk_repo_for_md(repo_root: Path) -> Iterator[Path]:
    """Yield absolute paths to .md files under repo_root that aren't
    ignored by .dejavuignore + built-in defaults."""
    repo_root = Path(repo_root).resolve()
    patterns = _load_dejavuignore(repo_root)
    for dirpath, dirnames, filenames in os.walk(repo_root):
        rel_dir = os.path.relpath(dirpath, repo_root)
        if rel_dir == ".":
            rel_dir = ""
        # Prune ignored directories so we don't descend.
        dirnames[:] = [d for d in dirnames
                       if not _match_ignore(
                           (f"{rel_dir}/{d}" if rel_dir else d), patterns)]
        for f in filenames:
            if not f.endswith(".md"):
                continue
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if _match_ignore(rel, patterns):
                continue
            yield Path(dirpath) / f
