"""claude-dejavu health checks — schema + Weaviate-class integrity
+ data-loss detection + auto-recovery.

Detects the silent-failure mode where Postgres or Weaviate is
reachable but our tables / classes / data are gone (volume swap,
orphan-container destruction, manual `docker compose down -v`,
fresh container against a stale config.env, etc.). Without this
the SessionStart hook would just `try: query / except: silent` and
the plugin would appear installed but do nothing useful.

Three layers of auto-healing:

  Layer 1 — Schema integrity:
    Check actual tables vs expected (sourced from schema.sql).
    If any missing, re-apply schema.sql. Idempotent.

  Layer 2 — Weaviate class integrity:
    If `ClaudeDejavuTurn` class is missing, POST the class
    definition to /v1/schema. Idempotent (Weaviate's 422-on-
    duplicate is treated as no-op).

  Layer 3 — Session data loss:
    If schema is now healthy but `sessions` table is empty AND
    we have source data available (live ~/.claude/projects/ OR
    off-tree backup at DATA_ROOT/chat-logs/), spawn the ingester
    in the background to re-populate. Prefers live source (it's
    authoritative + auto-mirrors to the backup). Falls back to
    the backup if live tree is gone (rare but possible if user
    deleted ~/.claude/projects/).

All three layers surface human-readable status via
`format_repair_notice()` so SessionStart can include it in its
systemMessage. The user gets a clear picture of: what was broken,
what was repaired, what's now re-ingesting, and how long it
should take.

Failures (PG unreachable, schema apply error, Weaviate down,
ingester spawn failure) never raise — they return structured
dicts so the caller can decide whether to surface, retry, or
no-op."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_FILE = _PLUGIN_ROOT / "code" / "schema.sql"
_WV_CLASS_FILE = _PLUGIN_ROOT / "code" / "weaviate-class.json"


def expected_pg_tables() -> set[str]:
    """Parse schema.sql and return every `CREATE TABLE IF NOT EXISTS`
    name. Single source of truth — avoids duplicating the table list
    in code/health.py + code/doctor.py + tests."""
    if not _SCHEMA_FILE.exists():
        return set()
    txt = _SCHEMA_FILE.read_text(encoding="utf-8", errors="replace")
    # `CREATE TABLE IF NOT EXISTS <name> (` — name may have schema
    # prefix in future, keep regex permissive but exact for current
    # use.
    return {m.group(1) for m in
              re.finditer(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(",
                            txt)}


def expected_weaviate_class() -> Optional[str]:
    """Return the canonical Weaviate class name from
    weaviate-class.json. None if file missing/malformed."""
    try:
        d = json.loads(_WV_CLASS_FILE.read_text(encoding="utf-8"))
        return d.get("class")
    except Exception:  # noqa: BLE001
        return None


# ─── Postgres schema integrity ──────────────────────────────────────────────


def check_pg_schema(conn) -> dict[str, Any]:
    """Return integrity status for the PG schema.

    Returns:
        {
          "ok": bool,
          "expected": int,
          "actual": int,
          "missing": list[str],   # tables in expected but not in DB
          "extra":   list[str],   # tables in DB but not in expected
                                  # (informational; usually OK,
                                  #  user-added tables / migrations)
          "error":   str | None,  # populated on connection / query failure
        }
    """
    expected = expected_pg_tables()
    if not expected:
        # schema.sql missing or unparseable — can't check.
        return {"ok": False, "expected": 0, "actual": 0,
                  "missing": [], "extra": [],
                  "error": "schema.sql unreadable"}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            actual = {r[0] for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "expected": len(expected), "actual": 0,
                  "missing": sorted(expected), "extra": [],
                  "error": f"query failed: {type(e).__name__}: {e}"}

    missing = sorted(expected - actual)
    extra = sorted(actual - expected
                       - {"_dejavu_schema_versions",  # migration tracker
                           "indexer_fingerprints",     # v0.4.5
                           "fabrication_history"})     # v0.5.0a
    # Strip pg_trgm / vector extension tables (they live in public
    # too on default installs).
    extra = [t for t in extra
                if not t.startswith(("pg_", "spatial_"))]
    return {
        "ok": not missing,
        "expected": len(expected),
        "actual": len(actual),
        "missing": missing,
        "extra": extra,
        "error": None,
    }


def repair_pg_schema(conn, *, project_for_log: str = "") -> dict[str, Any]:
    """Re-apply schema.sql against the connection. Idempotent.

    Returns: {"ok": bool, "applied": bool, "error": str | None}
    `applied=True` means schema.sql ran and modified the DB;
    `applied=False` means it ran but everything was already up.
    """
    if not _SCHEMA_FILE.exists():
        return {"ok": False, "applied": False,
                  "error": "schema.sql not found"}
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
        return {"ok": True, "applied": True, "error": None}
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "applied": False,
                  "error": f"{type(e).__name__}: {e}"}


# ─── Weaviate class integrity ───────────────────────────────────────────────


def check_weaviate_class(wv_url: str, class_name: Optional[str] = None,
                            timeout: float = 3.0) -> dict[str, Any]:
    """Return integrity status for the Weaviate class.

    Returns:
        {
          "ok": bool,
          "url": str,
          "class": str | None,
          "exists": bool,    # the class is registered with Weaviate
          "error": str | None,
        }
    """
    cname = class_name or expected_weaviate_class()
    if not cname:
        return {"ok": False, "url": wv_url, "class": None,
                  "exists": False,
                  "error": "weaviate-class.json missing/malformed"}
    try:
        req = urllib.request.Request(f"{wv_url}/v1/schema/{cname}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "json" not in ctype:
                return {"ok": False, "url": wv_url, "class": cname,
                          "exists": False,
                          "error": "non-json response from /v1/schema"}
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "url": wv_url, "class": cname,
                      "exists": False, "error": None}
        return {"ok": False, "url": wv_url, "class": cname,
                  "exists": False,
                  "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": wv_url, "class": cname,
                  "exists": False,
                  "error": f"{type(e).__name__}: {e}"}
    # Class exists.
    return {"ok": True, "url": wv_url, "class": cname,
              "exists": True, "error": None}


def repair_weaviate_class(wv_url: str,
                              class_def_path: Optional[Path] = None,
                              timeout: float = 10.0) -> dict[str, Any]:
    """POST the class definition to Weaviate. Idempotent — Weaviate
    returns 422 on duplicate-class-create which we treat as no-op."""
    p = class_def_path or _WV_CLASS_FILE
    if not p.exists():
        return {"ok": False, "applied": False,
                  "error": f"class def file not found: {p}"}
    try:
        body = p.read_text(encoding="utf-8")
        req = urllib.request.Request(f"{wv_url}/v1/schema",
                                          data=body.encode("utf-8"),
                                          headers={"Content-Type":
                                                   "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return {"ok": True, "applied": True, "error": None}
    except urllib.error.HTTPError as e:
        if e.code == 422:
            # Already exists — no-op.
            return {"ok": True, "applied": False, "error": None}
        return {"ok": False, "applied": False,
                  "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "applied": False,
                  "error": f"{type(e).__name__}: {e}"}


# ─── Combined check + auto-repair entry point ──────────────────────────────


def _venv_python() -> Path:
    """Resolve the bundled venv interpreter (best effort)."""
    root = _resolve_data_root()
    if os.name == "nt":
        return root / "venv" / "Scripts" / "python.exe"
    py3 = root / "venv" / "bin" / "python3"
    if py3.exists():
        return py3
    return root / "venv" / "bin" / "python"


def check_and_repair_storage(*, conn=None, wv_url: Optional[str] = None,
                                  auto_repair: bool = True,
                                  auto_recover_data: bool = True
                                  ) -> dict[str, Any]:
    """Run both PG and Weaviate integrity checks, optionally
    repairing missing pieces. Returns a structured report the
    SessionStart hook can format into a systemMessage.

    Args:
        conn: psycopg2 connection (or None to skip PG checks)
        wv_url: Weaviate URL (or None to skip)
        auto_repair: re-apply schema / re-create class on miss.
            Default True — we want the plugin to self-heal silent
            data-loss scenarios (orphan-container destruction etc.)
            without the user having to know the right repair steps.

    Returns:
        {
          "pg":       {check + repair results} | None,
          "weaviate": {check + repair results} | None,
          "ok":       bool,           # overall — both green (or skipped)
          "any_repair_applied": bool, # something was repaired
          "any_repair_failed":  bool, # repair attempted but didn't succeed
        }
    """
    out: dict[str, Any] = {"pg": None, "weaviate": None,
                              "ok": True, "any_repair_applied": False,
                              "any_repair_failed": False,
                              "data_loss": None, "recovery": None}

    if conn is not None:
        pg_status = check_pg_schema(conn)
        out["pg"] = {"check": pg_status, "repair": None}
        if not pg_status["ok"] and auto_repair:
            rep = repair_pg_schema(conn)
            out["pg"]["repair"] = rep
            if rep.get("applied"):
                out["any_repair_applied"] = True
                # Re-check post-repair to confirm.
                out["pg"]["check_after_repair"] = check_pg_schema(conn)
                if not out["pg"]["check_after_repair"]["ok"]:
                    out["any_repair_failed"] = True
                    out["ok"] = False
            elif not rep.get("ok"):
                out["any_repair_failed"] = True
                out["ok"] = False
        elif not pg_status["ok"]:
            out["ok"] = False

    if wv_url:
        wv_status = check_weaviate_class(wv_url)
        out["weaviate"] = {"check": wv_status, "repair": None}
        if not wv_status["ok"] and auto_repair:
            # Don't try to repair if the URL itself is unreachable —
            # only when the class is missing.
            if wv_status.get("error") in (None,):
                # ok=False but error=None → 404 (class missing, URL ok)
                rep = repair_weaviate_class(wv_url)
                out["weaviate"]["repair"] = rep
                if rep.get("applied"):
                    out["any_repair_applied"] = True
                    out["weaviate"]["check_after_repair"] = \
                        check_weaviate_class(wv_url)
                    if not out["weaviate"]["check_after_repair"]["ok"]:
                        out["any_repair_failed"] = True
                        out["ok"] = False
                elif not rep.get("ok"):
                    out["any_repair_failed"] = True
                    out["ok"] = False
        elif not wv_status["ok"]:
            out["ok"] = False

    # ─── Layer 3: data-loss detection + auto-recovery ──────────────
    # Only run if PG is reachable (we have a connection) AND the
    # schema is healthy after any repair. If the structure is still
    # broken there's nothing to ingest into.
    pg_ok = (conn is not None
               and out.get("pg")
               and (out["pg"].get("check_after_repair", {}).get("ok",
                       out["pg"]["check"]["ok"])))
    if pg_ok:
        try:
            loss = detect_session_data_loss(conn)
        except Exception as e:  # noqa: BLE001
            loss = {"data_loss": False, "sessions_in_db": -1,
                      "error": f"{type(e).__name__}: {e}",
                      "recovery": assess_recovery_sources()}
        out["data_loss"] = loss

        if loss.get("data_loss") and auto_recover_data:
            rec_src = loss["recovery"]["preferred_source"]
            spawn = kickoff_recovery_ingest(
                plugin_root=Path(__file__).resolve().parents[1],
                venv_python=_venv_python(),
                source=rec_src,
            )
            out["recovery"] = {"triggered": True,
                                  "source": rec_src,
                                  "spawn": spawn}
            if not spawn.get("ok"):
                out["any_repair_failed"] = True
                out["ok"] = False

    return out


# ─── Data-loss recovery (post-schema-repair re-ingest) ─────────────────────


def _resolve_data_root() -> Path:
    """Mirror install.py / hooks/ data-root resolution."""
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if os.name == "nt":
        base = (os.environ.get("LOCALAPPDATA")
                  or os.environ.get("APPDATA")
                  or str(Path.home() / "AppData" / "Local"))
        return Path(base) / "claude-dejavu"
    if "darwin" in (sys.platform if 'sys' in dir() else ""):
        return (Path.home() / "Library" / "Application Support"
                  / "claude-dejavu")
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) / "claude-dejavu" if xdg
              else Path.home() / ".local" / "share" / "claude-dejavu")


def _count_jsonl_files(root: Path) -> int:
    """Count *.jsonl recursively under root. Returns 0 if root
    missing/unreadable. Cheap — just an iglob count."""
    if not root.is_dir():
        return 0
    n = 0
    try:
        for _ in root.rglob("*.jsonl"):
            n += 1
    except OSError:
        pass
    return n


def assess_recovery_sources() -> dict[str, Any]:
    """Catalog where session data CAN be recovered from.

    Returns:
        {
          "live_source_count": int,    # ~/.claude/projects/<X>/<uuid>.jsonl
          "off_tree_backup_count": int,# DATA_ROOT/chat-logs/<X>/<uuid>.jsonl
          "preferred_source": "live" | "backup" | None,
          "live_source_path": str,
          "off_tree_backup_path": str,
        }

    Live source preferred when present (it's authoritative + the
    bootstrap also re-mirrors it to chat-logs/, healing any drift
    in the backup as a side effect). Backup is the fallback when
    ~/.claude/projects/ is empty (e.g. user nuked it accidentally).
    """
    live = Path.home() / ".claude" / "projects"
    backup = _resolve_data_root() / "chat-logs"
    n_live = _count_jsonl_files(live)
    n_backup = _count_jsonl_files(backup)
    if n_live > 0:
        preferred = "live"
    elif n_backup > 0:
        preferred = "backup"
    else:
        preferred = None
    return {
        "live_source_count": n_live,
        "off_tree_backup_count": n_backup,
        "preferred_source": preferred,
        "live_source_path": str(live),
        "off_tree_backup_path": str(backup),
    }


def detect_session_data_loss(conn, *, threshold: int = 1
                                  ) -> dict[str, Any]:
    """Decide whether the sessions table has lost data — i.e.
    schema is healthy but the DB is empty (or near-empty) while
    we have source files available to re-ingest.

    Returns:
        {
          "data_loss": bool,         # True iff DB is below threshold
                                      #  AND a recovery source exists
          "sessions_in_db": int,
          "recovery": <assess_recovery_sources output>,
        }

    `threshold=1` means we consider 0 sessions in DB a loss. Bump
    to a higher value to require a more drastic delta — but in
    practice the user's first reaction to "0 sessions" is "did I
    lose everything?" so we should help them recover immediately.
    """
    n_db = -1
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sessions")
            n_db = int(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        # Sessions table doesn't exist → schema repair would have
        # been triggered upstream. No data loss to detect because
        # the structure itself is missing.
        return {"data_loss": False, "sessions_in_db": -1,
                  "recovery": assess_recovery_sources()}

    rec = assess_recovery_sources()
    has_source = rec["preferred_source"] is not None
    data_loss = (n_db < threshold) and has_source
    return {"data_loss": data_loss, "sessions_in_db": n_db,
              "recovery": rec}


def kickoff_recovery_ingest(*, plugin_root: Path,
                                venv_python: Path,
                                source: str) -> dict[str, Any]:
    """Spawn the ingester in the background to re-ingest from the
    selected source. Detached / non-blocking so the SessionStart
    hook returns to Claude promptly.

    Args:
        plugin_root: PLUGIN_ROOT (where code/ingester.py lives)
        venv_python: venv interpreter path (resolved by caller)
        source: "live" → `--bootstrap` (reads ~/.claude/projects/)
                "backup" → `--backfill` (reads chat-logs/)

    Returns: {"ok": bool, "pid": int | None, "log": str,
              "command": list[str], "error": str | None}
    """
    if source not in ("live", "backup"):
        return {"ok": False, "pid": None, "log": "",
                  "command": [],
                  "error": f"unknown source: {source!r}"}

    ingester = plugin_root / "code" / "ingester.py"
    if not ingester.is_file():
        return {"ok": False, "pid": None, "log": "",
                  "command": [],
                  "error": f"ingester.py not found at {ingester}"}
    if not venv_python.exists():
        return {"ok": False, "pid": None, "log": "",
                  "command": [],
                  "error": f"venv python missing at {venv_python}"}

    log_path = _resolve_data_root() / "recovery.log"
    flag = "--bootstrap" if source == "live" else "--backfill"
    cmd = [str(venv_python), str(ingester), flag]
    try:
        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            with open(log_path, "ab") as logf:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL,
                    stdout=logf, stderr=logf,
                    creationflags=DETACHED_PROCESS
                                  | CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
        else:
            with open(log_path, "ab") as logf:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL,
                    stdout=logf, stderr=logf,
                    start_new_session=True, close_fds=True,
                )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "pid": None, "log": str(log_path),
                  "command": cmd,
                  "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "pid": proc.pid, "log": str(log_path),
              "command": cmd, "error": None}


def format_repair_notice(report: dict[str, Any]) -> Optional[str]:
    """Render `check_and_repair_storage` output as a one-paragraph
    notice for the SessionStart systemMessage. Returns None when
    everything is healthy (so the preamble doesn't show needless
    noise)."""
    has_recovery = (report.get("recovery")
                       and report["recovery"].get("triggered"))
    if (report.get("ok")
            and not report.get("any_repair_applied")
            and not has_recovery):
        return None  # all green, no repair needed — silent success

    lines: list[str] = []

    pg = report.get("pg") or {}
    if pg.get("check") and not pg["check"].get("ok"):
        chk = pg["check"]
        if chk.get("error"):
            lines.append(f"⚠ Postgres schema check failed: "
                            f"{chk['error']}")
        elif chk.get("missing"):
            n = len(chk["missing"])
            sample = ", ".join(chk["missing"][:3])
            if n > 3:
                sample += f", … (+{n-3} more)"
            lines.append(f"⚠ Postgres: {n} table(s) missing "
                            f"({sample})")
        rep = pg.get("repair")
        if rep and rep.get("applied") and rep.get("ok"):
            lines.append("  → schema re-applied successfully")
        elif rep and not rep.get("ok"):
            lines.append(f"  → repair FAILED: {rep.get('error')}")
            lines.append("  → run `claude-dejavu doctor` for "
                            "details, or re-run install.py --auto")

    wv = report.get("weaviate") or {}
    if wv.get("check") and not wv["check"].get("ok"):
        chk = wv["check"]
        if chk.get("error"):
            lines.append(f"⚠ Weaviate unreachable at "
                            f"{chk.get('url')}: {chk['error']}")
        elif not chk.get("exists"):
            lines.append(f"⚠ Weaviate class `{chk.get('class')}` "
                            f"missing at {chk.get('url')}")
        rep = wv.get("repair")
        if rep and rep.get("applied") and rep.get("ok"):
            lines.append("  → class re-created successfully")
        elif rep and not rep.get("ok"):
            lines.append(f"  → repair FAILED: {rep.get('error')}")
            lines.append("  → run `claude-dejavu doctor` for details")

    # Layer 3: data-loss recovery notice.
    rec = report.get("recovery") or {}
    if rec.get("triggered"):
        loss = report.get("data_loss") or {}
        n_db = loss.get("sessions_in_db", -1)
        rec_info = loss.get("recovery") or {}
        n_live = rec_info.get("live_source_count", 0)
        n_backup = rec_info.get("off_tree_backup_count", 0)
        spawn = rec.get("spawn") or {}
        if rec.get("source") == "live":
            src_label = (f"live source ({n_live} session(s) at "
                            f"~/.claude/projects/)")
        else:
            src_label = (f"off-tree backup ({n_backup} session(s) at "
                            f"chat-logs/)")
        if spawn.get("ok"):
            lines.append(
                f"⟳ Postgres sessions table empty (count={n_db}); "
                f"started re-ingest in background from {src_label}.")
            lines.append(f"  → progress log: {spawn.get('log')}")
            lines.append(
                "  → ingest happens async; subsequent sessions will "
                "see the data as it lands. To disable auto-recovery: "
                "`dejavu config set AUTO_HEAL_STORAGE false`.")
        else:
            lines.append(
                f"⟳ Postgres sessions table empty (count={n_db}); "
                f"tried to re-ingest from {src_label} but failed: "
                f"{spawn.get('error')}")
            lines.append(
                "  → run manually: `claude-dejavu reindex` (per "
                "project) or invoke the ingester directly.")
    elif (report.get("data_loss")
              and report["data_loss"].get("data_loss")
              and not (report.get("recovery")
                          and report["recovery"].get("triggered"))):
        # Data loss detected but auto-recovery was disabled.
        loss = report["data_loss"]
        rec_info = loss.get("recovery") or {}
        n_live = rec_info.get("live_source_count", 0)
        n_backup = rec_info.get("off_tree_backup_count", 0)
        lines.append(
            f"⟳ Postgres sessions table empty (count="
            f"{loss.get('sessions_in_db', -1)}). Auto-recovery is "
            f"disabled. Available sources: live={n_live}, "
            f"backup={n_backup}.")
        lines.append(
            "  → enable: `dejavu config set AUTO_HEAL_STORAGE true`, "
            "or re-ingest manually: "
            "`<venv-python> code/ingester.py --bootstrap`")

    if not lines:
        return None
    header = ("[claude-dejavu] storage health check found issues "
                 "(auto-repair attempted):")
    return header + "\n" + "\n".join(lines)
