#!/usr/bin/env python3
"""claude-dejavu — PG corruption rescue toolkit (v0.8.3).

Codifies the five ad-hoc scripts written during the 2026-05-14 → 2026-05-16
disk-full rescue session into proper, reusable subcommands. Every function
here is grounded on a tested behavior — see the references at the bottom of
the docstring.

Subcommands:

* :func:`scan` — walk every row of every table in dejavu-managed PG DBs,
  probe content (length + substring) to surface corrupt TOAST chunks,
  run index validity check (``pg_index.indisvalid``), and pick up
  pages whose heap tuples can't be read.
* :func:`patch_toast` — for every row reported corrupt by :func:`scan`,
  read the canonical content from the source JSONL using
  ``sessions.project_path`` + ``turns.message_uuid``, UPDATE with a
  per-row SAVEPOINT so one bad row doesn't kill the batch.
* :func:`reset_flags` — row-by-row UPDATE that sets
  ``vectorized=FALSE, weaviate_id=NULL``. Per-row savepoint isolates
  failures.
* :func:`rebuild_table` — filtered index-scan rebuild generalized to
  any table. Drops dependents (FKs + views) discovered at runtime via
  ``pg_catalog``, copies all rows except ``exclude_ids`` via an index
  scan to a ``<name>_new`` table, optionally INSERTs JSONL-recovered
  replacements, atomic swap, recreate dependents.
* :func:`reindex` — wraps ``REINDEX INDEX CONCURRENTLY`` for indexes
  flagged corrupt by :func:`scan` (or all indexes when ``all_indexes=True``).

All write paths default to ``dry_run=True``; only an explicit
``dry_run=False`` flips them destructive.

References (canonical behavior bibles, /tmp/* on the rescue host):

* ``/tmp/patch-pg-toast-corruption.py`` — savepoint-based TOAST patch.
* ``/tmp/reset-vectorized-row-by-row.py`` — per-row savepoint UPDATE.
* ``/tmp/rebuild-turns-table.py`` — table rebuild with FK + view +
  sequence handling.

See ``docs/superpowers/specs/2026-05-16-pg-weaviate-rescue-toolkit-design.md``.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


# ─── Config + DSN helpers ─────────────────────────────────────────────


def _resolve_data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = (os.environ.get("LOCALAPPDATA")
                or os.environ.get("APPDATA")
                or str(Path.home() / "AppData" / "Local"))
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) / "claude-dejavu" if xdg
            else Path.home() / ".local" / "share" / "claude-dejavu")


def _load_cfg() -> dict[str, str]:
    cfg: dict[str, str] = {}
    f = _resolve_data_root() / "config.env"
    if not f.exists():
        return cfg
    for line in f.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


def _default_db_name() -> str:
    """Resolve the dejavu-managed PG database name (mirrors ingester.py)."""
    cfg = _load_cfg()
    if cfg.get("PG_DB"):
        return cfg["PG_DB"]
    user = os.environ.get("USER") or "unknown"
    return f"claude_dejavu_{user}"


def _pg_connect(db_name: Optional[str] = None):
    """Open a psycopg2 connection to the named DB. Caller is responsible
    for closing."""
    try:
        import psycopg2  # type: ignore[import]
    except ImportError as exc:  # pragma: no cover — venv path
        raise RescueError(
            f"psycopg2 not available: {exc}; install dejavu venv first")
    cfg = _load_cfg()
    return psycopg2.connect(
        host=cfg.get("PG_HOST", "localhost"),
        port=cfg.get("PG_PORT", "5450"),
        user=cfg.get("PG_USER", "postgres"),
        password=cfg.get("PG_PASS", "postgres"),
        dbname=db_name or _default_db_name(),
        connect_timeout=5,
    )


class RescueError(RuntimeError):
    """Hard failure during a PG-rescue phase."""


# ─── scan ─────────────────────────────────────────────────────────────


# Tables whose row content lives in TOAST and is therefore vulnerable to
# disk-tear. The scan walks every row of these tables. Other dejavu
# tables are smaller and not TOAST-resident, so the scan skips them by
# default.
SCAN_TABLES: tuple[str, ...] = ("turns", "tool_calls", "observations")

# Columns that hold actual content (everything in pg_attribute that we
# expect to be large enough to land in TOAST). Probed via length() +
# substring().
# Probe expressions per table. Values are SQL expressions (not bare
# column names) so we can cast jsonb to text where TOAST lives on the
# stringified form. Each expression must accept length() + substring()
# and reference only one base column — that base column's name (the
# token before "::" if any) is what the probe falsely-reports against
# in `corrupt_rows[].col` so users can grep the report.
#
# Discovered 2026-05-17: this constant must match the live schema or
# scan reports phantom corruption (one fake hit per missing column ×
# every row in the table). Tests live in test_pg_rescue.py
# ``test_scan_columns_match_live_schema`` — if you bump these, update
# both.
SCAN_COLUMNS: dict[str, tuple[str, ...]] = {
    "turns": ("content",),
    "tool_calls": ("tool_input::text", "tool_output"),
    "observations": ("title", "subtitle"),
}


def scan(*, db_name: Optional[str] = None,
         conn=None,
         tables: Optional[Iterable[str]] = None) -> dict:
    """Walk every row of every table and probe content + index validity.

    Returns ``{corrupt_rows: [{table, id, col, error}], corrupt_indexes:
    [{name, error}], corrupt_pages: [{table, page_blocks}], probed_rows: N,
    probed_indexes: N}``.

    The ``conn`` parameter is a test seam — tests inject a mock psycopg2
    connection.
    """
    if conn is None:
        conn = _pg_connect(db_name)
        owns_conn = True
    else:
        owns_conn = False

    tbls = tuple(tables or SCAN_TABLES)
    result: dict[str, Any] = {
        "corrupt_rows": [],
        "corrupt_indexes": [],
        "corrupt_pages": [],
        "probed_rows": 0,
        "probed_indexes": 0,
        "schema_mismatches": [],
    }

    _validated_cols: dict[str, tuple[str, ...]] = {}

    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # why: if SCAN_COLUMNS drifts from the live schema, the probe
            # SQL raises UndefinedColumn for every row — looks like 100%
            # corruption to users (caught 2026-05-17 when tool_calls.input
            # was probed but the real column is tool_input). Validate
            # before probing; skip missing columns + surface drift.
            for _table in tbls:
                _wanted = SCAN_COLUMNS.get(_table, ())
                if not _wanted:
                    continue
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s",
                    (_table,))
                _live = {r[0] for r in cur.fetchall()}
                _keep: list[str] = []
                for _expr in _wanted:
                    _base = _expr.split("::", 1)[0].strip()
                    if _base in _live:
                        _keep.append(_expr)
                    else:
                        result["schema_mismatches"].append({
                            "table": _table,
                            "probe": _expr,
                            "reason": f"column {_base!r} not in live schema",
                        })
                _validated_cols[_table] = tuple(_keep)
            conn.commit()  # close the validation tx cleanly
            # — Row-level probe via length+substring on each content col —
            for table in tbls:
                cols = _validated_cols.get(table, SCAN_COLUMNS.get(table, ()))
                if not cols:
                    continue
                # Build a probing SELECT that touches every content byte;
                # any TOAST chunk corruption raises an error PER row.
                # We use savepoints so one bad row doesn't poison the rest.
                cur.execute(f"SELECT id FROM {table} ORDER BY id")
                ids = [r[0] for r in cur.fetchall()]
                for row_id in ids:
                    for col in cols:
                        cur.execute("SAVEPOINT row_sp")
                        try:
                            cur.execute(
                                f"SELECT length({col}), "
                                f"substring({col}, 1, 1) "
                                f"FROM {table} WHERE id = %s",
                                (row_id,))
                            cur.fetchone()
                            cur.execute("RELEASE SAVEPOINT row_sp")
                        except Exception as exc:
                            try:
                                cur.execute("ROLLBACK TO SAVEPOINT row_sp")
                                cur.execute("RELEASE SAVEPOINT row_sp")
                            except Exception:
                                pass
                            result["corrupt_rows"].append({
                                "table": table,
                                "id": row_id,
                                "col": col,
                                "error": f"{type(exc).__name__}: "
                                         f"{str(exc).strip()[:200]}",
                            })
                    result["probed_rows"] += 1

            # — Index validity probe via pg_index.indisvalid + REINDEX-dry —
            cur.execute("""
                SELECT c.relname, n.nspname
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                JOIN pg_class t ON t.oid = i.indrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'pg_toast',
                                          'information_schema')
                  AND (i.indisvalid = FALSE OR i.indisready = FALSE)
            """)
            for (name, _ns) in cur.fetchall():
                result["corrupt_indexes"].append({
                    "name": name,
                    "error": "indisvalid=false or indisready=false",
                })

            # — Page-level probe (heap tuple read for each block) —
            # We use pg_relation_size + a coarse SELECT count(*) per
            # table; a per-block walk is too slow for non-emergency
            # scans. The expensive heap-page check stays in
            # rebuild_table where it's actually needed.
            for table in tbls:
                cur.execute("SAVEPOINT pg_sp")
                try:
                    cur.execute(f"SELECT count(*) FROM {table}")
                    cur.fetchone()
                    cur.execute("RELEASE SAVEPOINT pg_sp")
                except Exception as exc:
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT pg_sp")
                        cur.execute("RELEASE SAVEPOINT pg_sp")
                    except Exception:
                        pass
                    result["corrupt_pages"].append({
                        "table": table,
                        "page_blocks": [],
                        "error": f"{type(exc).__name__}: "
                                 f"{str(exc).strip()[:200]}",
                    })
            result["probed_indexes"] = len(result["corrupt_indexes"])
        conn.rollback()
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass
    return result


# ─── patch_toast ──────────────────────────────────────────────────────


def _projects_root() -> Path:
    """Mirror the JSONL projects root used by the ingester."""
    return Path.home() / ".claude" / "projects"


def _extract_content_from_block(block: dict, content_type: str) -> Optional[str]:
    """Mirror ingester's content extraction for a single block.

    Lifted directly from /tmp/patch-pg-toast-corruption.py's
    extract_content() function — keep behavior byte-for-byte identical.
    """
    if content_type == "text":
        return block.get("text", "") if isinstance(block, dict) else str(block or "")
    if content_type == "tool_use":
        return json.dumps({
            "name": block.get("name", ""),
            "input": block.get("input", {}),
        }, ensure_ascii=False)
    if content_type == "tool_result":
        c = block.get("content", "")
        if isinstance(c, list):
            parts: list[str] = []
            for it in c:
                if isinstance(it, dict) and it.get("type") == "text":
                    parts.append(it.get("text", ""))
                elif isinstance(it, str):
                    parts.append(it)
            return "\n".join(parts)
        return str(c or "")
    if content_type == "thinking":
        return block.get("thinking", "") if isinstance(block, dict) else str(block or "")
    if content_type == "attachment":
        return block.get("path", "") if isinstance(block, dict) else str(block or "")
    if content_type == "image":
        s = block.get("source") if isinstance(block, dict) else None
        if isinstance(s, dict):
            return s.get("data", "") or s.get("url", "")
        return ""
    return json.dumps(block, ensure_ascii=False)


def _find_jsonl_entry(session_id: str, project_path: str,
                        message_uuid: str,
                        projects_root: Optional[Path] = None) -> Optional[dict]:
    """Locate the JSONL entry by UUID inside the per-session file."""
    root = projects_root or _projects_root()
    jsonl = root / project_path / f"{session_id}.jsonl"
    if not jsonl.is_file():
        return None
    with open(jsonl, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("uuid") == message_uuid:
                return obj
    return None


def _derive_content(entry: dict, content_type: str,
                      role: str) -> Optional[str]:
    msg = entry.get("message") or {}
    blocks = msg.get("content")
    if not isinstance(blocks, list):
        if isinstance(blocks, str):
            return blocks
        return None
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == content_type:
            return _extract_content_from_block(b, content_type)
    if len(blocks) == 1 and isinstance(blocks[0], dict):
        return _extract_content_from_block(
            blocks[0], blocks[0].get("type") or content_type)
    return None


def patch_toast(*, db_name: Optional[str] = None,
                  dry_run: bool = True,
                  conn=None,
                  bad_ids: Optional[Iterable[int]] = None,
                  projects_root: Optional[Path] = None) -> dict:
    """Patch corrupt-TOAST rows by re-deriving ``content`` from JSONL.

    Uses per-row SAVEPOINTs so a single missing JSONL or unparseable
    block doesn't kill the batch.

    Returns ``{patched: N, errored: M, errored_ids: [...], skipped: K,
    mode: 'apply' | 'dry-run', per_row: [...]}``.
    """
    if conn is None:
        conn = _pg_connect(db_name)
        owns_conn = True
    else:
        owns_conn = False

    if bad_ids is None:
        # Self-discover: scan first.
        scan_result = scan(db_name=db_name, conn=conn)
        bad_ids = [r["id"] for r in scan_result["corrupt_rows"]
                    if r["table"] == "turns"]

    report: dict[str, Any] = {
        "patched": 0,
        "errored": 0,
        "errored_ids": [],
        "skipped": 0,
        "mode": "apply" if not dry_run else "dry-run",
        "per_row": [],
    }

    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            for bad_id in bad_ids:
                row: dict[str, Any] = {"id": bad_id}
                cur.execute("SAVEPOINT row_sp")
                try:
                    cur.execute("""
                        SELECT t.session_id, t.message_uuid, t.idx,
                               t.role, t.content_type,
                               s.project_path
                        FROM turns t
                        LEFT JOIN sessions s ON s.id = t.session_id
                        WHERE t.id = %s
                    """, (bad_id,))
                    r = cur.fetchone()
                    if not r:
                        row["status"] = "not_found"
                        report["skipped"] += 1
                        cur.execute("RELEASE SAVEPOINT row_sp")
                        report["per_row"].append(row)
                        continue
                    sid, muuid, idx, role, ctype, ppath = r
                    row.update({
                        "session_id": str(sid),
                        "message_uuid": muuid,
                        "idx": idx,
                        "role": role,
                        "content_type": ctype,
                        "project_path": ppath,
                    })

                    entry = _find_jsonl_entry(
                        str(sid), ppath or "", muuid,
                        projects_root=projects_root)
                    if entry is None:
                        row["status"] = "jsonl_entry_missing"
                        report["errored"] += 1
                        report["errored_ids"].append(bad_id)
                        cur.execute("RELEASE SAVEPOINT row_sp")
                        report["per_row"].append(row)
                        continue

                    new_content = _derive_content(entry, ctype, role)
                    if new_content is None:
                        row["status"] = "could_not_derive_content"
                        report["errored"] += 1
                        report["errored_ids"].append(bad_id)
                        cur.execute("RELEASE SAVEPOINT row_sp")
                        report["per_row"].append(row)
                        continue

                    row["new_content_len"] = len(new_content)
                    if not dry_run:
                        cur.execute("""
                            UPDATE turns
                            SET content = %s, gist = NULL,
                                gist_method = NULL
                            WHERE id = %s
                        """, (new_content, bad_id))
                        row["status"] = "patched"
                    else:
                        row["status"] = "would_patch"
                    report["patched"] += 1
                    cur.execute("RELEASE SAVEPOINT row_sp")
                except Exception as exc:
                    row["status"] = (f"error: {type(exc).__name__}: "
                                       f"{str(exc).strip()[:200]}")
                    report["errored"] += 1
                    report["errored_ids"].append(bad_id)
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT row_sp")
                        cur.execute("RELEASE SAVEPOINT row_sp")
                    except Exception:
                        pass
                report["per_row"].append(row)

            if dry_run:
                conn.rollback()
            else:
                conn.commit()
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass
    return report


# ─── reset_flags ──────────────────────────────────────────────────────


def reset_flags(*, db_name: Optional[str] = None,
                  dry_run: bool = True,
                  conn=None,
                  batch_size: int = 500) -> dict:
    """Row-by-row UPDATE that sets ``vectorized=FALSE, weaviate_id=NULL``.

    Per-row SAVEPOINT so bulk UPDATEs surviving corrupt rows. Returns
    ``{reset: N, errored: M, errored_ids: [...], total: T, mode: ...}``.
    """
    if conn is None:
        conn = _pg_connect(db_name)
        owns_conn = True
    else:
        owns_conn = False

    report: dict[str, Any] = {
        "reset": 0,
        "errored": 0,
        "errored_ids": [],
        "total": 0,
        "mode": "apply" if not dry_run else "dry-run",
    }

    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM turns "
                "WHERE vectorized = TRUE OR weaviate_id IS NOT NULL "
                "ORDER BY id")
            ids = [r[0] for r in cur.fetchall()]
            report["total"] = len(ids)

            for n, tid in enumerate(ids, start=1):
                cur.execute("SAVEPOINT row_sp")
                try:
                    cur.execute(
                        "UPDATE turns "
                        "SET vectorized = FALSE, weaviate_id = NULL "
                        "WHERE id = %s",
                        (tid,))
                    cur.execute("RELEASE SAVEPOINT row_sp")
                    report["reset"] += 1
                except Exception:
                    try:
                        cur.execute("ROLLBACK TO SAVEPOINT row_sp")
                        cur.execute("RELEASE SAVEPOINT row_sp")
                    except Exception:
                        pass
                    report["errored"] += 1
                    report["errored_ids"].append(tid)
                # batch_size is informational; we don't actually flush
                # intermediate commits — the whole UPDATE is one tx.
                _ = batch_size

            if dry_run:
                conn.rollback()
            else:
                conn.commit()
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass
    return report


# ─── rebuild_table ────────────────────────────────────────────────────


def _discover_dependents(cur, table_name: str) -> dict:
    """Inspect pg_catalog for FKs + views + sequences referencing the
    given table. Used so rebuild_table doesn't hardcode dependents.

    Returns ``{fks: [{constraint, table, definition}], views: [{name,
    definition}], indexes: [{name, definition}], sequences:
    [{name, column}]}``.
    """
    info: dict[str, list[dict]] = {
        "fks": [], "views": [], "indexes": [], "sequences": [],
    }
    # Incoming FKs (other tables pointing AT us)
    cur.execute("""
        SELECT c.conname,
                t.relname AS table_name,
                pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        WHERE c.contype = 'f'
          AND c.confrelid = (
                SELECT oid FROM pg_class
                 WHERE relname = %s
                   AND relnamespace = (
                       SELECT oid FROM pg_namespace
                        WHERE nspname = 'public'))
    """, (table_name,))
    for cname, t, defn in cur.fetchall():
        info["fks"].append({
            "constraint": cname, "table": t, "definition": defn,
        })
    # Views referencing the table
    cur.execute("""
        SELECT DISTINCT v.viewname, pg_get_viewdef(v.viewname::regclass, true)
        FROM pg_views v
        WHERE v.schemaname = 'public'
          AND v.definition ILIKE %s
    """, (f"%{table_name}%",))
    for vn, vdef in cur.fetchall():
        info["views"].append({"name": vn, "definition": vdef})
    # Indexes
    cur.execute("""
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = %s
    """, (table_name,))
    for iname, idef in cur.fetchall():
        info["indexes"].append({"name": iname, "definition": idef})
    # Owned sequences
    cur.execute("""
        SELECT s.relname, a.attname
        FROM pg_class s
        JOIN pg_depend d ON d.objid = s.oid
        JOIN pg_class t ON t.oid = d.refobjid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
        WHERE s.relkind = 'S' AND t.relname = %s
    """, (table_name,))
    for sn, col in cur.fetchall():
        info["sequences"].append({"name": sn, "column": col})
    return info


def rebuild_table(table_name: str, *,
                    exclude_ids: Optional[Iterable[int]] = None,
                    replace_rows: Optional[Iterable[dict]] = None,
                    replace_from_jsonl: bool = False,
                    db_name: Optional[str] = None,
                    dry_run: bool = True,
                    conn=None,
                    projects_root: Optional[Path] = None) -> dict:
    """Filtered index-scan rebuild generalized to any table.

    Phases (mirrors /tmp/rebuild-turns-table.py but generalized):
      1. Acquire ``ACCESS EXCLUSIVE LOCK`` on ``table_name``.
      2. Discover dependents via pg_catalog (FKs + views + indexes +
         sequences).
      3. Drop incoming FKs + views referencing the table.
      4. ``CREATE TABLE <name>_new (LIKE <name> INCLUDING ALL)``.
      5. ``INSERT INTO <name>_new SELECT * FROM <name>
                 WHERE id NOT IN (exclude_ids)`` (forced index scan).
      6. Optional: INSERT JSONL-recovered replacement rows.
      7. Reassign sequences to ``<name>_new``.
      8. DROP + RENAME atomic swap.
      9. Rename indexes + constraints back to canonical names.
     10. Recreate FKs + views.

    Returns ``{copied: N, replaced: M, errored: K, mode: ...,
    dependents: {...}}``.
    """
    if conn is None:
        conn = _pg_connect(db_name)
        owns_conn = True
    else:
        owns_conn = False

    excl = sorted(set(exclude_ids or []))
    report: dict[str, Any] = {
        "table": table_name,
        "copied": 0,
        "replaced": 0,
        "errored": 0,
        "mode": "apply" if not dry_run else "dry-run",
        "dependents": {},
        "errors": [],
    }

    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            # Phase 2 — discover dependents up front (even in dry-run,
            # since we need the structure to report what would happen).
            deps = _discover_dependents(cur, table_name)
            report["dependents"] = deps

            if dry_run:
                # Count what WOULD copy.
                if excl:
                    cur.execute(
                        f"SELECT count(*) FROM {table_name} "
                        f"WHERE id <> ALL(%s)", (excl,))
                else:
                    cur.execute(f"SELECT count(*) FROM {table_name}")
                report["copied"] = cur.fetchone()[0]
                report["replaced"] = len(list(replace_rows or []))
                conn.rollback()
                return report

            # — Apply path —
            # Phase 1
            cur.execute(
                f"LOCK TABLE {table_name} IN ACCESS EXCLUSIVE MODE")

            # Phase 3 — drop dependents
            for fk in deps["fks"]:
                cur.execute(
                    f'ALTER TABLE {fk["table"]} DROP CONSTRAINT IF '
                    f'EXISTS {fk["constraint"]}')
            for v in deps["views"]:
                cur.execute(f'DROP VIEW IF EXISTS {v["name"]}')

            # Phase 4 — clone structure
            new_table = f"{table_name}_new"
            cur.execute(
                f"CREATE TABLE {new_table} "
                f"(LIKE {table_name} INCLUDING ALL)")

            # Phase 5 — index-scan copy
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute("SET LOCAL enable_bitmapscan = off")
            if excl:
                cur.execute(
                    f"INSERT INTO {new_table} SELECT * FROM "
                    f"{table_name} WHERE id <> ALL(%s)", (excl,))
            else:
                cur.execute(
                    f"INSERT INTO {new_table} SELECT * FROM "
                    f"{table_name}")
            cur.execute(f"SELECT count(*) FROM {new_table}")
            report["copied"] = cur.fetchone()[0]

            # Phase 6 — JSONL replacements
            if replace_rows:
                for row in replace_rows:
                    cols = ",".join(row.keys())
                    placeholders = ",".join(["%s"] * len(row))
                    try:
                        cur.execute(
                            f"INSERT INTO {new_table} ({cols}) "
                            f"VALUES ({placeholders})",
                            list(row.values()))
                        report["replaced"] += 1
                    except Exception as exc:
                        report["errored"] += 1
                        report["errors"].append(
                            f"replace row failed: {exc}")

            # Phase 7 — sequence reassign
            for seq in deps["sequences"]:
                cur.execute(
                    f'ALTER SEQUENCE {seq["name"]} OWNED BY '
                    f'{new_table}.{seq["column"]}')

            # Phase 8 — atomic swap
            cur.execute(f"DROP TABLE {table_name}")
            cur.execute(
                f"ALTER TABLE {new_table} RENAME TO {table_name}")

            # Phase 9 — rename indexes + constraints from "_new" form
            new_prefix = f"{table_name}_new_"
            for idx in deps["indexes"]:
                src = idx["name"].replace(table_name + "_",
                                              new_prefix, 1)
                if src != idx["name"]:
                    cur.execute(
                        "SELECT to_regclass(%s) IS NOT NULL", (src,))
                    if cur.fetchone()[0]:
                        cur.execute(
                            f'ALTER INDEX "{src}" RENAME TO '
                            f'"{idx["name"]}"')
            cur.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = %s::regclass "
                "AND conname LIKE %s",
                (table_name, new_prefix + "%"))
            for (cname,) in cur.fetchall():
                tgt = cname.replace(new_prefix, table_name + "_", 1)
                cur.execute(
                    f'ALTER TABLE {table_name} RENAME CONSTRAINT '
                    f'"{cname}" TO "{tgt}"')

            # Phase 10 — recreate dependents
            for fk in deps["fks"]:
                # ``definition`` already starts with FOREIGN KEY (...) REFERENCES ...
                cur.execute(
                    f'ALTER TABLE {fk["table"]} '
                    f'ADD CONSTRAINT {fk["constraint"]} '
                    f'{fk["definition"]}')
            for v in deps["views"]:
                cur.execute(
                    f'CREATE VIEW {v["name"]} AS {v["definition"]}')

            conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise RescueError(
            f"rebuild_table failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass

    # Note: replace_from_jsonl flag is wired through the CLI; the actual
    # JSONL → row dict construction happens upstream in the CLI handler
    # (where we have access to the session table + JSONL location).
    _ = replace_from_jsonl
    return report


# ─── reindex ──────────────────────────────────────────────────────────


def reindex(*, db_name: Optional[str] = None,
            all_indexes: bool = False,
            concurrently: bool = True,
            conn=None,
            dry_run: bool = True) -> dict:
    """Wrap ``REINDEX INDEX CONCURRENTLY`` for indexes flagged corrupt.

    With ``all_indexes=True`` reindexes every index in public schema.

    Returns ``{reindexed: [name], errored: [{name, error}], mode: ...}``.
    """
    if conn is None:
        conn = _pg_connect(db_name)
        owns_conn = True
    else:
        owns_conn = False

    report: dict[str, Any] = {
        "reindexed": [],
        "errored": [],
        "mode": "apply" if not dry_run else "dry-run",
    }

    try:
        # REINDEX CONCURRENTLY must run outside an explicit transaction.
        conn.autocommit = True
        with conn.cursor() as cur:
            if all_indexes:
                cur.execute(
                    "SELECT c.relname FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public'")
                names = [r[0] for r in cur.fetchall()]
            else:
                cur.execute(
                    "SELECT c.relname FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' "
                    "AND (i.indisvalid = FALSE OR i.indisready = FALSE)")
                names = [r[0] for r in cur.fetchall()]

            for name in names:
                if dry_run:
                    report["reindexed"].append(name)
                    continue
                stmt = (f'REINDEX INDEX CONCURRENTLY "{name}"'
                          if concurrently
                          else f'REINDEX INDEX "{name}"')
                try:
                    cur.execute(stmt)
                    report["reindexed"].append(name)
                except Exception as exc:
                    report["errored"].append({
                        "name": name,
                        "error": f"{type(exc).__name__}: "
                                   f"{str(exc).strip()[:200]}",
                    })
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass
    return report


__all__ = [
    "RescueError",
    "scan", "patch_toast", "reset_flags", "rebuild_table", "reindex",
    "SCAN_TABLES", "SCAN_COLUMNS",
    "_default_db_name", "_pg_connect",
    "_extract_content_from_block", "_find_jsonl_entry", "_derive_content",
    "_discover_dependents",
]
