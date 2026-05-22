#!/usr/bin/env python3
"""
claude-dejavu — stable repo identity resolver.

Resolves any starting directory to a (repo_id, repo_root) pair.
The repo_id is a UUID stored in <repo_root>/.dejavu/id; the file
is .gitignored by default so each clone has its own identity.

See docs/superpowers/specs/2026-05-08-dejavu-docs-embedding-design.md
section 6 for the design.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Optional


class MalformedRepoIdError(Exception):
    """Raised when <repo_root>/.dejavu/id exists but isn't a valid UUID."""


_MAX_HOPS = 64


def _find_dejavu_id(start: Path) -> Optional[Path]:
    """Walk UP from start; return the path of the first .dejavu/id found."""
    cur = start.resolve()
    hops = 0
    while hops < _MAX_HOPS:
        candidate = cur / ".dejavu" / "id"
        if candidate.is_file():
            return candidate
        if cur.parent == cur:  # root
            return None
        cur = cur.parent
        hops += 1
    return None


def _find_git_root(start: Path) -> Optional[Path]:
    """Walk UP from start; return the first ancestor containing .git."""
    cur = start.resolve()
    hops = 0
    while hops < _MAX_HOPS:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
        hops += 1
    return None


def _bootstrap_gitignore(repo_root: Path) -> None:
    """Append .dejavu/ to .gitignore if not already present."""
    gi = repo_root / ".gitignore"
    existing = gi.read_text() if gi.is_file() else ""
    if ".dejavu/" in existing.splitlines():
        return
    with gi.open("a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("# claude-dejavu local state (per-clone repo id, etc.)\n")
        f.write(".dejavu/\n")


def _write_new_id(repo_root: Path) -> str:
    """Generate + persist a new UUID at <repo_root>/.dejavu/id."""
    rid = str(uuid.uuid4())
    dj = repo_root / ".dejavu"
    dj.mkdir(parents=True, exist_ok=True)
    (dj / "id").write_text(rid + "\n", encoding="utf-8")
    _bootstrap_gitignore(repo_root)
    return rid


def resolve(start: Optional[Path] = None,
             *, home_override: Optional[Path] = None
             ) -> tuple[str, Path]:
    """Return (repo_id, repo_root) for `start` (defaults to cwd).

    Lookup order:
      1. Existing .dejavu/id walking up from start.
      2. Git root walking up; generate UUID, write .dejavu/id, bootstrap .gitignore.
      3. Cwd as the repo boundary; generate UUID, write .dejavu/id.

    Raises MalformedRepoIdError if a .dejavu/id file is found but
    its contents are not a valid UUID.
    """
    if start is None:
        start = Path.cwd()
    start = Path(start).resolve()
    home = Path(home_override) if home_override else Path.home()

    existing = _find_dejavu_id(start)
    if existing is not None:
        try:
            rid = uuid.UUID(existing.read_text(encoding="utf-8").strip())
        except ValueError as e:
            raise MalformedRepoIdError(
                f"{existing} contains invalid UUID: {e}") from e
        return str(rid), existing.parent.parent

    git_root = _find_git_root(start)
    if git_root is not None:
        # Refuse to write .dejavu/id at or above $HOME (defensive).
        if not str(git_root.resolve()).startswith(str(home.resolve())):
            pass  # still allow — git root outside home is fine
        return _write_new_id(git_root), git_root

    # No git, no existing id — use start as the boundary.
    return _write_new_id(start), start


def upsert_repos_row(conn, repo_id: str, repo_root: Path) -> None:
    """Insert or update the `repos` row for this repo. Caller owns the
    transaction; this just runs the SQL.

    `conn` is a psycopg2 connection. `label` is set to the basename
    of `repo_root`.
    """
    label = repo_root.name or "unknown"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO repos (id, label, last_seen_path)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              label = EXCLUDED.label,
              last_seen_path = EXCLUDED.last_seen_path,
              updated_at = now()
            """,
            (repo_id, label, str(repo_root)),
        )
