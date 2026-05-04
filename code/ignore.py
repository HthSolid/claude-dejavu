#!/usr/bin/env python3
"""
claude-dejavu — `.dejavuignore` filter (v0.3.11).

Stops the indexers from walking files / folders that don't belong in
the index. Same syntax as `.gitignore`, with one important addition:
**`!` re-include works** for the case where a path is gitignored but
should still be indexed locally.

Usage from indexers:

    from ignore import IgnoreFilter

    flt = IgnoreFilter.for_project(project_root)
    if flt.should_skip(some_path):
        continue

The filter is built once per `index_project_*` call and reused for
the duration of the walk. Its compiled patterns live in memory only
— no DB persistence (the file IS the source of truth).

Discovery rules:

  1. Walk up from the project root, collecting `.dejavuignore` files
     at every level. Patterns from a parent apply to children unless
     overridden by a more-specific child rule.

  2. Subdirectories MAY contain their own `.dejavuignore`. Patterns
     in a child file are scoped to that subtree (gitignore semantics).

  3. If `respect_gitignore: true` is set in `.dejavu-config.toml`
     (or env var `CLAUDE_DEJAVU_RESPECT_GITIGNORE=1`), `.gitignore`
     patterns are ALSO loaded. Default is FALSE because gitignore
     covers things like `*.log` that you might still want indexed.

  4. Pattern syntax (gitignore):
        foo            — match `foo` anywhere
        /foo           — match `foo` at this directory level only
        foo/           — match `foo` only as a directory
        *.log          — glob match
        !important.log — re-include (overrides earlier ignore)
        # comment      — ignored

Implementation: pure-Python (no `pathspec` dep), based on
gitignore semantics. We use `fnmatch` plus a small ordered-rules
walker so the LAST matching rule wins (supports `!` overrides).
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


_IGNORE_FILE = ".dejavuignore"
_GITIGNORE_FILE = ".gitignore"

# Built-in defaults — always skipped, even when no .dejavuignore exists.
# Keep this list small + universally-bad. Anything project-specific
# belongs in the project's own .dejavuignore.
_BUILTIN_PATTERNS = (
    ".git/", "node_modules/", ".next/", ".turbo/",
    "dist/", "build/", "out/", "target/",
    "__pycache__/", ".venv/", "venv/", ".pytest_cache/",
    ".mypy_cache/", ".coverage/", "coverage/",
    "vendor/",
    # Lock files indexing causes massive symbol noise with no value.
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "go.sum",
)


@dataclass
class _Rule:
    """One compiled ignore rule, anchored to a base directory."""
    pattern: str               # the raw pattern string from the file (without `!`)
    negate: bool               # `!` prefix re-include
    anchored: bool             # leading `/` means "only at base level"
    only_dirs: bool            # trailing `/` means "directories only"
    base_dir: Path             # directory the rule is relative to


@dataclass
class IgnoreFilter:
    project_root: Path
    rules: list[_Rule] = field(default_factory=list)
    respect_gitignore: bool = False

    @classmethod
    def for_project(cls, project_root: str | Path,
                     respect_gitignore: bool | None = None) -> "IgnoreFilter":
        root = Path(project_root).resolve()
        if respect_gitignore is None:
            respect_gitignore = (
                os.environ.get("CLAUDE_DEJAVU_RESPECT_GITIGNORE", "")
                in ("1", "true", "yes")
            )
        flt = cls(project_root=root, respect_gitignore=respect_gitignore)
        flt._load_builtins()
        flt._discover_ignore_files()
        return flt

    # ─── Loading ──────────────────────────────────────────────────────

    def _load_builtins(self):
        """Always-on patterns at project root."""
        for p in _BUILTIN_PATTERNS:
            self.rules.append(self._compile(p, base_dir=self.project_root))

    def _discover_ignore_files(self):
        """Walk the project tree (top-down) collecting .dejavuignore +
        optionally .gitignore at every directory."""
        for dirpath, dirnames, filenames in os.walk(self.project_root):
            base = Path(dirpath)
            if _IGNORE_FILE in filenames:
                self._load_file(base / _IGNORE_FILE, base)
            if self.respect_gitignore and _GITIGNORE_FILE in filenames:
                self._load_file(base / _GITIGNORE_FILE, base)
            # Optimization: don't descend into dirs that are already
            # ignored by what we've loaded so far. This is BOTH a
            # perf improvement on huge repos AND avoids surprises
            # where a deeper .dejavuignore inside an ignored dir would
            # be loaded uselessly.
            dirnames[:] = [
                d for d in dirnames
                if not self._is_skipped_relative(base / d)
            ]

    def _load_file(self, path: Path, base: Path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            self.rules.append(self._compile(line, base_dir=base))

    @staticmethod
    def _compile(line: str, base_dir: Path) -> _Rule:
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        anchored = line.startswith("/")
        if anchored:
            line = line[1:]
        only_dirs = line.endswith("/")
        if only_dirs:
            line = line[:-1]
        # Gitignore `**/` prefix means "match in any directory including
        # the root". `fnmatch` doesn't understand `**`, so we strip the
        # leading `**/` and treat the remainder as an unanchored pattern.
        if line.startswith("**/"):
            line = line[3:]
            anchored = False
        return _Rule(
            pattern=line, negate=negate, anchored=anchored,
            only_dirs=only_dirs, base_dir=base_dir,
        )

    # ─── Matching ─────────────────────────────────────────────────────

    def _matches_rule(self, abs_path: Path, is_dir: bool, rule: _Rule) -> bool:
        # Path must be inside the rule's base.
        try:
            rel = abs_path.resolve().relative_to(rule.base_dir.resolve())
        except ValueError:
            return False
        rel_str = str(rel)
        # Normalize Windows backslashes.
        rel_str = rel_str.replace(os.sep, "/")

        # only_dirs: only match directories
        if rule.only_dirs and not is_dir:
            # Still match if any ancestor of `rel_str` matches the
            # pattern as a directory — e.g. pattern `dist/` should
            # match `dist/foo.js`.
            parts = rel_str.split("/")
            for i in range(1, len(parts) + 1):
                ancestor = "/".join(parts[:i])
                if rule.anchored:
                    if fnmatch.fnmatchcase(ancestor, rule.pattern):
                        return True
                else:
                    if fnmatch.fnmatchcase(ancestor.split("/")[-1], rule.pattern):
                        return True
                    if fnmatch.fnmatchcase(ancestor, rule.pattern):
                        return True
            return False

        if rule.anchored:
            # Match at base level only — full relative path against pattern.
            return fnmatch.fnmatchcase(rel_str, rule.pattern)

        # Unanchored: pattern matches a path component anywhere, OR the
        # full rel path.
        if fnmatch.fnmatchcase(rel_str, rule.pattern):
            return True
        if fnmatch.fnmatchcase(Path(rel_str).name, rule.pattern):
            return True
        for part in rel_str.split("/"):
            if fnmatch.fnmatchcase(part, rule.pattern):
                return True
        return False

    def should_skip(self, path: str | Path, *, is_dir: bool | None = None
                     ) -> bool:
        """The hot path. Returns True if `path` should NOT be indexed."""
        p = Path(path)
        if is_dir is None:
            is_dir = p.is_dir()
        # Last matching rule wins — supports !re-include.
        skip = False
        for rule in self.rules:
            if self._matches_rule(p, is_dir, rule):
                skip = not rule.negate
        return skip

    def _is_skipped_relative(self, abs_path: Path) -> bool:
        """Used during discovery to prune walk. Equivalent to
        `should_skip(abs_path, is_dir=True)` but cheaper because we
        haven't finished loading rules yet."""
        return self.should_skip(abs_path, is_dir=True)

    # ─── Diagnostics ──────────────────────────────────────────────────

    def explain(self, path: str | Path) -> dict:
        """Return WHICH rule(s) matched and what the verdict is, so
        users can debug 'why isn't my file being indexed?'"""
        p = Path(path)
        is_dir = p.is_dir()
        winning: _Rule | None = None
        all_matched = []
        for rule in self.rules:
            if self._matches_rule(p, is_dir, rule):
                winning = rule
                all_matched.append({
                    "pattern": ("!" if rule.negate else "") + rule.pattern,
                    "base": str(rule.base_dir),
                    "anchored": rule.anchored,
                    "only_dirs": rule.only_dirs,
                    "negate": rule.negate,
                })
        return {
            "path": str(p),
            "is_dir": is_dir,
            "skipped": (winning is not None and not winning.negate),
            "winning_rule": (
                {"pattern": ("!" if winning.negate else "") + winning.pattern,
                 "base": str(winning.base_dir),
                 "negate": winning.negate}
                if winning else None
            ),
            "all_matching_rules": all_matched,
        }

    def list_rules(self) -> list[dict]:
        return [{
            "pattern": ("!" if r.negate else "") + r.pattern,
            "base": str(r.base_dir),
            "anchored": r.anchored,
            "only_dirs": r.only_dirs,
        } for r in self.rules]

    def filter_walk(self, root: Path) -> Iterable[tuple[str, list[str], list[str]]]:
        """Convenience: like os.walk but yields only paths that pass
        the filter, AND prunes walked directories in-place."""
        for dirpath, dirnames, filenames in os.walk(root):
            base = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if not self.should_skip(base / d, is_dir=True)
            ]
            filenames = [
                f for f in filenames
                if not self.should_skip(base / f, is_dir=False)
            ]
            yield dirpath, dirnames, filenames
