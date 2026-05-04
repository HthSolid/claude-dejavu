"""Per-project fabrication memory.

Records every name the rewriter classifies as RESIDUAL or REPLACE so
that future encounters of the same name in the same project can be
surfaced fast. After N fabrications of the same name, dejavu can
warn the user: "this keeps appearing — either declare it or
blacklist it."

Schema lives in `code/schema.sql` (table: fabrication_history).
This module is the read/write API used by:
  - the rewriter (records on every cascade outcome)
  - the `dejavu fabrications` CLI (lists, marks status, deletes)
  - the doctor (surfaces high-count fabrications as warnings)

All operations are idempotent w.r.t. (project_slug, name, domain) —
calling record_fabrication() twice with the same triple just bumps
the count.
"""
from __future__ import annotations

from typing import Any, Optional


def record_fabrication(conn, project_slug: str, name: str,
                          domain: str, *,
                          verdict: str = "RESIDUAL") -> int:
    """Record a name the rewriter couldn't resolve. Returns the new
    cumulative count for this (project, name, domain) triple.

    `verdict` is informational ("RESIDUAL" | "REPLACE") so the caller
    can later distinguish "model invented something we couldn't fix"
    from "model typo'd something we auto-fixed but it keeps coming
    back." Stored in `notes` on first insertion.

    Never raises on DB errors — returns -1 instead so the caller's
    grounding pipeline isn't blocked by a logging-side failure."""
    if not project_slug or not name:
        return -1
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fabrication_history
                    (project_slug, name, domain, count, notes)
                VALUES (%s, %s, %s, 1, %s)
                ON CONFLICT (project_slug, name, domain) DO UPDATE
                  SET count = fabrication_history.count + 1,
                      last_seen_at = now()
                RETURNING count
                """,
                (project_slug, name, domain,
                  f"first verdict: {verdict}"),
            )
            row = cur.fetchone()
            return int(row[0]) if row else -1
    except Exception:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return -1


def lookup_fabrication(conn, project_slug: str, name: str,
                          domain: str) -> Optional[dict[str, Any]]:
    """Return the existing record for this (project, name, domain),
    or None if the rewriter has never seen this triple."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, domain, count, status, first_seen_at,
                       last_seen_at, notes
                  FROM fabrication_history
                 WHERE project_slug = %s
                   AND name = %s
                   AND domain = %s
                """,
                (project_slug, name, domain),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "name": row[0], "domain": row[1], "count": row[2],
            "status": row[3], "first_seen_at": row[4],
            "last_seen_at": row[5], "notes": row[6],
        }
    except Exception:  # noqa: BLE001
        return None


def list_fabrications(conn, project_slug: str, *,
                          status: Optional[str] = None,
                          min_count: int = 1,
                          limit: int = 100
                          ) -> list[dict[str, Any]]:
    """List fabrications for a project, ordered by count DESC. Use
    `status='open'` to filter out blacklisted/declared records."""
    try:
        sql = ("SELECT name, domain, count, status, first_seen_at, "
                 "last_seen_at, notes "
                 "  FROM fabrication_history "
                 " WHERE project_slug = %s AND count >= %s")
        params: list[Any] = [project_slug, min_count]
        if status:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY count DESC, last_seen_at DESC LIMIT %s"
        params.append(limit)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [{
            "name": r[0], "domain": r[1], "count": r[2],
            "status": r[3], "first_seen_at": r[4],
            "last_seen_at": r[5], "notes": r[6],
        } for r in rows]
    except Exception:  # noqa: BLE001
        return []


def set_status(conn, project_slug: str, name: str, domain: str,
                  status: str, *, notes: Optional[str] = None) -> bool:
    """Triage a fabrication: 'open' | 'declared' | 'blacklisted'.
    Once 'declared' or 'blacklisted', the rewriter can short-circuit
    its cascade — but right now we just store and surface."""
    if status not in ("open", "declared", "blacklisted"):
        raise ValueError(f"unknown status: {status}")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE fabrication_history
                   SET status = %s,
                       notes = COALESCE(%s, notes)
                 WHERE project_slug = %s
                   AND name = %s
                   AND domain = %s
                """,
                (status, notes, project_slug, name, domain),
            )
            return cur.rowcount > 0
    except Exception:  # noqa: BLE001
        return False


def is_blacklisted(conn, project_slug: str, name: str,
                      domain: str) -> bool:
    """Fast check: should the rewriter short-circuit and refuse this
    name entirely? Used by the cascade to skip the deterministic
    work for names the user has already triaged."""
    rec = lookup_fabrication(conn, project_slug, name, domain)
    return bool(rec and rec.get("status") == "blacklisted")


def project_summary(conn, project_slug: str) -> dict[str, Any]:
    """Compact counters for `dejavu doctor` and the MCP tool."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) AS n,
                       SUM(count) AS total_occurrences
                  FROM fabrication_history
                 WHERE project_slug = %s
                 GROUP BY status
                """,
                (project_slug,),
            )
            rows = cur.fetchall()
        out: dict[str, Any] = {"open": 0, "declared": 0,
                                  "blacklisted": 0,
                                  "total_occurrences": 0}
        for r in rows:
            out[r[0]] = r[1]
            out["total_occurrences"] += int(r[2] or 0)
        return out
    except Exception:  # noqa: BLE001
        return {"open": 0, "declared": 0, "blacklisted": 0,
                  "total_occurrences": 0}
