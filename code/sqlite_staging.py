#!/usr/bin/env python3
"""claude-dejavu — SQLite consistent-snapshot staging (v0.8.0).

Backing up a live SQLite-WAL database with ``restic backup`` would
capture the ``.db`` plus ``-wal``/``-shm`` triple at inconsistent
points, yielding a "database disk image is malformed" error on first
open. The fix is :sql:`VACUUM INTO` (SQLite 3.27+):

  * Atomic from SQLite's point of view (a single transaction).
  * Produces a fully-consistent single-file copy — no WAL sidecars.
  * Acquires only a normal write lock briefly; concurrent reads
    continue.

Run :func:`stage_sqlite_db` at the start of every ``session-copy
take`` (and the migration's initial backup), then point restic at the
staged file instead of the live one.

See: docs/superpowers/specs/2026-05-15-snapshot-subsystem-design.md §3.7.5
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


def stage_sqlite_db(src: Path, staging_dir: Path) -> Optional[Path]:
    """Run :sql:`VACUUM INTO` so the resulting file is a consistent
    single-file copy of ``src``, regardless of WAL state.

    Returns the staged path on success, ``None`` on graceful skip
    (source missing, locked, or any :class:`sqlite3.Error` from
    ``VACUUM INTO``). Never raises — callers can treat this as a soft
    pre-step and continue with the rest of the backup.
    """
    src = Path(src)
    if not src.is_file():
        return None
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    dst = staging_dir / src.name
    # VACUUM INTO refuses to overwrite an existing file.
    try:
        dst.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return None
    try:
        conn = sqlite3.connect(str(src))
        try:
            # Bind via string interp since SQLite refuses placeholders
            # in VACUUM INTO. dst is a controlled internal path, never
            # user input — escape single quotes defensively.
            quoted = str(dst).replace("'", "''")
            conn.execute(f"VACUUM INTO '{quoted}'")
        finally:
            conn.close()
    except sqlite3.Error:
        # Best-effort: a locked source, malformed source, or out-of-disk
        # all degrade gracefully — better to skip the DB backup than
        # abort the whole session-copy.
        try:
            if dst.is_file():
                dst.unlink()
        except OSError:
            pass
        return None
    return dst


__all__ = ["stage_sqlite_db"]
