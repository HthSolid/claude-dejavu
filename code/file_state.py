#!/usr/bin/env python3
"""
claude-dejavu — indexer fingerprint cache (v0.4.5).

Each non-symbol indexer used to walk the entire project tree on every
reindex, even when nothing relevant had changed. This module provides
a project-level skip cache: each indexer records the fingerprint of
its relevant files (count + max-mtime + sum-size); on next run, if
the fingerprint matches, the indexer skips itself and returns the
stats it cached last time.

Usage from an indexer:

    from file_state import IndexerCache, files_under

    def index_project_X(project_dir, project_slug, conn, *, force=False):
        relevant = list(files_under(project_dir, suffixes={".py", ".ts"},
                                      flt=flt))
        cache = IndexerCache(conn, project_slug, "X")
        if not force and cache.is_fresh(relevant):
            return cache.cached_stats() or {"skipped": True}
        # ... do the actual indexing work ...
        cache.store(relevant, stats=result_stats)
        return result_stats

The cache is conservative: ANY change to any relevant file (mtime
bump, size change, new file, deleted file) invalidates the cache and
forces a full re-run.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class _Fingerprint:
    file_count: int
    max_mtime_ns: int
    sum_size_bytes: int


def _fingerprint_of(files: list[Path]) -> _Fingerprint:
    """Compute (count, max-mtime, sum-size) of the file list. Files
    that can't be stat()'d are silently skipped — they'll be re-tried
    on next run, which is fine."""
    count = 0
    max_mtime = 0
    sum_size = 0
    for f in files:
        try:
            st = os.stat(f)
        except OSError:
            continue
        count += 1
        if st.st_mtime_ns > max_mtime:
            max_mtime = st.st_mtime_ns
        sum_size += st.st_size
    return _Fingerprint(count, max_mtime, sum_size)


class IndexerCache:
    """Per-(project, indexer) fingerprint cache. Reads + writes the
    `indexer_fingerprints` table."""

    def __init__(self, conn, project_slug: str, indexer_kind: str):
        self.conn = conn
        self.project_slug = project_slug
        self.indexer_kind = indexer_kind

    def _read(self) -> Optional[tuple[_Fingerprint, dict]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT file_count, max_mtime_ns, sum_size_bytes,
                          cached_stats
                   FROM indexer_fingerprints
                   WHERE project_slug = %s AND indexer_kind = %s""",
                (self.project_slug, self.indexer_kind),
            )
            row = cur.fetchone()
        if not row:
            return None
        fp = _Fingerprint(row[0], row[1], row[2])
        stats = row[3] or {}
        return (fp, stats)

    def is_fresh(self, files: list[Path]) -> bool:
        """Returns True if the relevant files haven't changed since
        last run — caller should skip the heavy work."""
        prev = self._read()
        if prev is None:
            return False
        prev_fp, _ = prev
        cur_fp = _fingerprint_of(files)
        return (prev_fp.file_count == cur_fp.file_count
                and prev_fp.max_mtime_ns == cur_fp.max_mtime_ns
                and prev_fp.sum_size_bytes == cur_fp.sum_size_bytes)

    def cached_stats(self) -> Optional[dict]:
        """Return the stats dict that was stored on last run."""
        prev = self._read()
        if prev is None:
            return None
        return prev[1]

    def store(self, files: list[Path], stats: dict) -> None:
        """Persist the new fingerprint + stats so future runs can skip."""
        fp = _fingerprint_of(files)
        # Mark stats as cache-hit-friendly: future returns will say so.
        cache_stats = dict(stats)
        cache_stats.setdefault("cached", False)  # this run did real work
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO indexer_fingerprints
                     (project_slug, indexer_kind, file_count,
                      max_mtime_ns, sum_size_bytes, cached_stats,
                      last_run_at)
                   VALUES (%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (project_slug, indexer_kind)
                     DO UPDATE SET file_count = EXCLUDED.file_count,
                                   max_mtime_ns = EXCLUDED.max_mtime_ns,
                                   sum_size_bytes = EXCLUDED.sum_size_bytes,
                                   cached_stats = EXCLUDED.cached_stats,
                                   last_run_at = now()""",
                (self.project_slug, self.indexer_kind,
                 fp.file_count, fp.max_mtime_ns, fp.sum_size_bytes,
                 json.dumps(cache_stats)),
            )

    def cached_stats_marked(self) -> dict:
        """Same as cached_stats() but flips `cached=True` so the
        caller's output makes the cache hit visible."""
        s = self.cached_stats() or {}
        s = dict(s)
        s["cached"] = True
        return s

    def invalidate(self) -> None:
        """Drop the cache entry for this indexer + project. Forces a
        full re-run on next call."""
        with self.conn.cursor() as cur:
            cur.execute(
                """DELETE FROM indexer_fingerprints
                   WHERE project_slug = %s AND indexer_kind = %s""",
                (self.project_slug, self.indexer_kind),
            )


def files_under(root: Path, *, suffixes: set[str] | None = None,
                  filenames: set[str] | None = None,
                  flt=None) -> Iterable[Path]:
    """Walk `root` and yield Path objects whose suffix is in
    `suffixes` OR whose lowercase basename is in `filenames`. Honors
    the IgnoreFilter. Used by indexers to compute the set of
    "relevant files" for fingerprinting.

    Either `suffixes` or `filenames` (or both) may be given.
    """
    if flt is None:
        from ignore import IgnoreFilter
        flt = IgnoreFilter.for_project(root)
    suffix_set = {s.lower() for s in suffixes} if suffixes else None
    name_set = {n.lower() for n in filenames} if filenames else None
    for dirpath, dirnames, fnames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                        if not flt.should_skip(Path(dirpath) / d, is_dir=True)]
        for f in fnames:
            full = Path(dirpath) / f
            if flt.should_skip(full, is_dir=False):
                continue
            if name_set and f.lower() in name_set:
                yield full
                continue
            if suffix_set:
                if full.suffix.lower() in suffix_set:
                    yield full
