#!/usr/bin/env python3
"""
claude-dejavu — Stop-hook doc diff scanner.

Runs incrementally: reads doc_file_state, compares to live filesystem,
re-embeds only changed files, tombstones deleted files.

Standalone-runnable entry point (subprocess from hooks/stop.py):
    python3 code/doc_diff_scan.py <repo_root>

Never raises into the caller. Logs to claude-dejavu's error_log on failure.
"""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


def _file_hash_from_chunks(chunks) -> str:
    """sha256 of concatenated chunk hashes — matches B5/_reindex_docs."""
    return hashlib.sha256(
        "".join(c.content_hash for c in chunks).encode("utf-8")
    ).hexdigest()


def scan(repo_root: Path,
          *, dsn: Optional[str] = None,
          embed_fn: Optional[Callable] = None,
          delete_fn: Optional[Callable] = None) -> dict:
    """Run one diff scan against repo_root. Returns counts dict:
    {scanned, unchanged, embedded, tombstoned, errors}.

    embed_fn(repo_id, rel_path, chunks, *, mtime_iso) is called once per
    changed file. Defaults to semantic_search.index_docs.

    delete_fn(repo_id, rel_path) is called once per file that has
    disappeared. Defaults to semantic_search.delete_docs_for_file.

    Test seams: embed_fn / delete_fn injected for unit tests.
    """
    import psycopg2
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import repo_id as _rid
    import doc_indexer as _di
    if embed_fn is None:
        from semantic_search import index_docs as embed_fn  # type: ignore
    if delete_fn is None:
        from semantic_search import delete_docs_for_file as delete_fn  # type: ignore

    counts = {"scanned": 0, "unchanged": 0, "embedded": 0,
              "tombstoned": 0, "errors": 0}
    repo_root = Path(repo_root).resolve()
    try:
        rid, rroot = _rid.resolve(start=repo_root)
    except Exception as e:
        counts["errors"] += 1
        _log_error("doc_diff_scan", f"repo_id resolve failed: {e}")
        return counts

    try:
        conn = psycopg2.connect(dsn or _default_dsn())
    except Exception as e:
        counts["errors"] += 1
        _log_error("doc_diff_scan", f"psycopg2.connect failed: {e}")
        return counts
    try:
        _rid.upsert_repos_row(conn, rid, rroot)
        conn.commit()

        # Snapshot prior cursor state.
        prior: dict[str, tuple[int, str]] = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT file_path, mtime, content_hash FROM doc_file_state "
                "WHERE repo_id = %s", (rid,))
            for fp, mt, ch in cur.fetchall():
                prior[fp] = (int(mt), ch)

        live_paths: set[str] = set()
        for md_path in _di.walk_repo_for_md(rroot):
            rel = str(md_path.relative_to(rroot))
            live_paths.add(rel)
            counts["scanned"] += 1
            try:
                live_mtime = int(md_path.stat().st_mtime)
            except OSError as e:
                counts["errors"] += 1
                _log_error("doc_diff_scan", f"stat failed for {rel}: {e}")
                continue

            try:
                chunks = list(_di.chunk_file(md_path))
            except Exception as e:
                counts["errors"] += 1
                _log_error("doc_diff_scan", f"chunk_file failed for {rel}: {e}")
                continue
            if not chunks:
                # Empty file — treat as nothing to embed; remove from cursor
                # if it was there.
                if rel in prior:
                    try:
                        delete_fn(rid, rel)
                    except Exception as e:
                        counts["errors"] += 1
                        _log_error("doc_diff_scan",
                                    f"delete on emptied file failed for {rel}: {e}")
                        continue  # don't touch cursor; retry next scan
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM doc_file_state WHERE repo_id=%s AND file_path=%s",
                            (rid, rel))
                    conn.commit()
                    counts["tombstoned"] += 1
                continue

            file_hash = _file_hash_from_chunks(chunks)
            cached = prior.get(rel)
            if cached and cached[1] == file_hash:
                # Content unchanged. Touch mtime if drifted.
                if cached[0] != live_mtime:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE doc_file_state SET mtime=%s, embedded_at=now() "
                            "WHERE repo_id=%s AND file_path=%s",
                            (live_mtime, rid, rel))
                    conn.commit()
                counts["unchanged"] += 1
                continue

            # Real change — re-embed.
            try:
                delete_fn(rid, rel)
                mtime_iso = datetime.fromtimestamp(
                    live_mtime, tz=timezone.utc).isoformat()
                embed_fn(rid, rel, chunks, mtime_iso=mtime_iso)
            except Exception as e:
                counts["errors"] += 1
                _log_error("doc_diff_scan", f"embed failed for {rel}: {e}")
                continue
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO doc_file_state
                       (repo_id, file_path, mtime, content_hash, chunk_count)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (repo_id, file_path) DO UPDATE SET
                         mtime=EXCLUDED.mtime,
                         content_hash=EXCLUDED.content_hash,
                         chunk_count=EXCLUDED.chunk_count,
                         embedded_at=now()""",
                    (rid, rel, live_mtime, file_hash, len(chunks)))
            conn.commit()
            counts["embedded"] += 1

        # Tombstone files in the cursor that are no longer on disk.
        for rel in set(prior.keys()) - live_paths:
            try:
                delete_fn(rid, rel)
            except Exception as e:
                counts["errors"] += 1
                _log_error("doc_diff_scan", f"tombstone failed for {rel}: {e}")
                continue
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM doc_file_state WHERE repo_id=%s AND file_path=%s",
                    (rid, rel))
            conn.commit()
            counts["tombstoned"] += 1
    finally:
        conn.close()
    return counts


def _default_dsn() -> str:
    """Read config.env for the runtime DSN. Mirrors recall.py's PG_DSN
    construction; we re-derive here to keep this script self-contained
    (so it can be spawned from hooks/stop.py without importing recall)."""
    from pathlib import Path as _P
    # config.env lives in the per-OS data root.
    if sys.platform == "darwin":
        data_root = _P.home() / "Library" / "Application Support" / "claude-dejavu"
    elif sys.platform.startswith("win"):
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("APPDATA")
                or str(_P.home() / "AppData" / "Local"))
        data_root = _P(base) / "claude-dejavu"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        data_root = (_P(xdg) / "claude-dejavu" if xdg
                      else _P.home() / ".local" / "share" / "claude-dejavu")
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        data_root = _P(override)
    cfg: dict[str, str] = {}
    cfg_path = data_root / "config.env"
    if cfg_path.is_file():
        for line in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    os_user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    pg_db = cfg.get("PG_DB") or f"claude_dejavu_{os_user}"
    return (f"host={cfg.get('PG_HOST','localhost')} "
            f"port={cfg.get('PG_PORT','5450')} "
            f"user={cfg.get('PG_USER','postgres')} "
            f"password={cfg.get('PG_PASS','postgres')} "
            f"dbname={pg_db}")


def _log_error(component: str, message: str) -> None:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from error_log import log_error
        log_error(component, message, level="error", exc_info=True)
    except Exception:
        print(f"[{component}] {message}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: doc_diff_scan.py <repo_root>", file=sys.stderr)
        sys.exit(2)
    try:
        result = scan(Path(sys.argv[1]))
        print(f"scan: {result}")
    except Exception as e:  # belt-and-suspenders — scan() shouldn't raise but if it does, don't crash the hook
        print(f"scan failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
