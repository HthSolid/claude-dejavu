#!/usr/bin/env python3
"""
claude-dejavu doctor — diagnose install / runtime health and offer fixes.

Used by:
  - `claude-dejavu doctor [--fix]` CLI
  - `dejavu_doctor` MCP tool (Claude can call this in-conversation)
  - hooks/session_start.py (lightweight check on every session)
  - tests/smoke.py

Returns a structured report plus a human-readable summary. Each check is
independent — one failure doesn't abort the rest, so the user always gets
the full picture.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Windows defaults stdout to cp1252 which can't encode the ✓/⚠/✗
# characters we use in the report. Reconfigure to UTF-8 on import so
# `python doctor.py` from cmd.exe / PowerShell doesn't crash with
# UnicodeEncodeError. `errors="replace"` is the belt-and-suspenders:
# anything truly unencodable becomes `?` instead of an exception.
if sys.platform.startswith("win") and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


@dataclass
class CheckResult:
    name: str
    status: str          # 'ok' | 'warn' | 'fail' | 'skip'
    detail: str = ""
    fix: str = ""        # actionable instruction (one-shot command preferred)


@dataclass
class Report:
    overall: str = "ok"     # ok | warn | fail
    plugin_version: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    install_status: dict | None = None

    def add(self, c: CheckResult) -> None:
        self.checks.append(c)
        # Worst-status wins.
        order = {"ok": 0, "skip": 0, "warn": 1, "fail": 2}
        if order.get(c.status, 0) > order.get(self.overall, 0):
            self.overall = c.status if c.status != "skip" else self.overall


# ─── Per-OS data root (mirrors install.py / hooks/) ────────────────────────


def _data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or str(Path.home() / "AppData" / "Local")
        )
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else Path.home() / ".local" / "share" / "claude-dejavu"


def _load_cfg() -> dict[str, str]:
    cfg: dict[str, str] = {}
    f = _data_root() / "config.env"
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


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _plugin_version() -> str:
    pj = _plugin_root() / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(pj.read_text()).get("version", "?")
    except Exception:
        return "?"


# ─── Individual checks ────────────────────────────────────────────────────


def _check_install_status_marker(rep: Report) -> None:
    p = _data_root() / "INSTALL_STATUS"
    if not p.exists():
        rep.add(CheckResult("install_status", "ok",
                            detail="no marker file (install completed cleanly)"))
        return
    try:
        d = json.loads(p.read_text())
    except Exception:
        rep.add(CheckResult("install_status", "warn",
                            detail=f"marker file present but unparseable: {p}"))
        return
    rep.install_status = d
    if d.get("status") == "in_progress":
        rep.add(CheckResult(
            "install_status", "warn",
            detail=f"install IN PROGRESS — stage='{d.get('stage')}'. "
                   f"This is normal during the first plugin install (1-5 min).",
        ))
    elif d.get("status") == "incomplete":
        rep.add(CheckResult(
            "install_status", "fail",
            detail=f"install INCOMPLETE: {d.get('summary', '')}",
            fix=d.get("fix", "Re-run scripts/install.py"),
        ))
    else:
        rep.add(CheckResult("install_status", "ok",
                            detail=f"status={d.get('status')}"))


def _check_venv(rep: Report) -> Path | None:
    venv_dir = _data_root() / "venv"
    candidates = [
        venv_dir / "Scripts" / "python.exe",
        venv_dir / "bin" / "python3",
        venv_dir / "bin" / "python",
    ]
    py = next((c for c in candidates if c.exists()), None)
    if py is None:
        rep.add(CheckResult(
            "venv", "fail",
            detail=f"bundled venv not found at {venv_dir}",
            fix=f"python scripts/install.py --auto",
        ))
        return None
    # Verify it can import psycopg2 + mcp + tree_sitter (the three we need).
    deps_check = subprocess.run(
        [str(py), "-c",
         "import psycopg2, mcp, tree_sitter, tree_sitter_languages; "
         "print('ok')"],
        capture_output=True, text=True, timeout=10,
    )
    if deps_check.returncode != 0:
        rep.add(CheckResult(
            "venv_deps", "fail",
            detail=f"venv exists at {py} but is missing deps: "
                   f"{(deps_check.stderr or deps_check.stdout).strip()[:200]}",
            fix=f"python scripts/install.py --auto",
        ))
        return py
    rep.add(CheckResult("venv", "ok", detail=str(py)))
    return py


def _check_postgres(cfg: dict, rep: Report) -> bool:
    host = cfg.get("PG_HOST", "localhost")
    port = cfg.get("PG_PORT", "5432")
    user = cfg.get("PG_USER", "postgres")
    password = cfg.get("PG_PASS", "postgres")
    db = cfg.get("PG_DB", "")
    psql = shutil.which("psql")
    pg_isready = shutil.which("pg_isready")

    if pg_isready:
        r = subprocess.run(
            [pg_isready, "-h", host, "-p", str(port), "-t", "3", "-U", user, "-d", "postgres"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            rep.add(CheckResult(
                "postgres", "fail",
                detail=f"pg_isready says {host}:{port} is not accepting connections",
                fix=("Start the bundled stack:\n"
                     f"  docker compose -f {_plugin_root()}/docker/docker-compose.minimal.yml up -d"),
            ))
            return False
    elif not psql:
        rep.add(CheckResult(
            "postgres", "skip",
            detail="neither pg_isready nor psql found in PATH; cannot verify",
        ))
        return False

    # Verify schema by counting expected tables.
    if not psql or not db:
        rep.add(CheckResult("postgres", "warn",
                            detail=f"reachable on {host}:{port} but couldn't verify schema (no psql or PG_DB)"))
        return True
    env = {**os.environ, "PGPASSWORD": password}
    r = subprocess.run(
        [psql, "-h", host, "-p", str(port), "-U", user, "-d", db, "-tA",
         "-c", "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"],
        env=env, capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        rep.add(CheckResult(
            "postgres_schema", "fail",
            detail=f"connected but couldn't query schema: {(r.stderr or '').strip()[:160]}",
            fix=f"Re-apply schema:  psql -h {host} -p {port} -U {user} -d {db} -f code/schema.sql",
        ))
        return True
    n = int((r.stdout or "0").strip() or 0)

    # Table-by-table integrity check — sourced from schema.sql so
    # every CREATE TABLE IF NOT EXISTS is verified. Catches the
    # silent-data-loss mode where Postgres is up but our tables are
    # gone (orphan-container destruction, fresh container against a
    # stale config.env, manual `docker compose down -v`, etc.).
    try:
        from health import expected_pg_tables
        expected = expected_pg_tables()
    except Exception:  # noqa: BLE001
        expected = set()
    if expected:
        # Get the actual set of public tables
        r2 = subprocess.run(
            [psql, "-h", host, "-p", str(port), "-U", user, "-d", db,
              "-tA", "-c",
              "SELECT table_name FROM information_schema.tables "
              "WHERE table_schema='public'"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        if r2.returncode == 0:
            actual = {ln.strip() for ln in (r2.stdout or "").splitlines()
                          if ln.strip()}
            missing = sorted(expected - actual)
            if missing:
                shown = ", ".join(missing[:5])
                if len(missing) > 5:
                    shown += f", … (+{len(missing) - 5} more)"
                rep.add(CheckResult(
                    "postgres_schema", "fail",
                    detail=(f"{len(missing)}/{len(expected)} expected "
                              f"tables missing in {db}: {shown}"),
                    fix=(f"Re-apply schema:  psql -h {host} -p {port} "
                           f"-U {user} -d {db} -f code/schema.sql\n"
                           f"Or restart Claude Code — SessionStart "
                           f"will auto-repair on next launch."),
                ))
                return True
            rep.add(CheckResult(
                "postgres", "ok",
                detail=f"{host}:{port} db={db} ({n} tables, "
                          f"all {len(expected)} expected present)"))
            return True
    # Fallback when we can't load the expected list — fall back to
    # the original count-based check.
    if n < 6:
        rep.add(CheckResult(
            "postgres_schema", "fail",
            detail=f"only {n} tables in {db} (expected ≥6)",
            fix=f"Re-apply schema:  psql -h {host} -p {port} -U {user} -d {db} -f code/schema.sql",
        ))
        return True
    rep.add(CheckResult("postgres", "ok", detail=f"{host}:{port} db={db} ({n} tables)"))
    return True


def _check_weaviate(cfg: dict, rep: Report) -> bool:
    url = cfg.get("WV_URL")
    if not url:
        rep.add(CheckResult("weaviate", "skip", detail="WV_URL not configured"))
        return False
    import urllib.request
    try:
        with urllib.request.urlopen(f"{url}/v1/meta", timeout=3) as r:
            data = json.loads(r.read())
        ver = data.get("version", "?")
    except Exception as e:
        rep.add(CheckResult(
            "weaviate", "fail",
            detail=f"{url} not responding: {type(e).__name__}: {e}",
            fix=("Start the bundled stack:\n"
                 f"  docker compose -p claude-dejavu -f {_plugin_root()}/docker/docker-compose.minimal.yml up -d"),
        ))
        return False

    # Weaviate is up — now verify our class is registered. This
    # closes the silent-failure mode where the user destroyed +
    # recreated their Weaviate container, ending up with a working
    # Weaviate but no `ClaudeDejavuTurn` class. Searches would
    # silently return zero hits forever.
    try:
        from health import (check_weaviate_class,
                                expected_weaviate_class)
        cname = expected_weaviate_class()
        wv_status = check_weaviate_class(url, cname)
    except Exception as e:  # noqa: BLE001
        wv_status = {"ok": False, "exists": False,
                       "error": f"{type(e).__name__}: {e}"}
    if not wv_status.get("ok"):
        if not wv_status.get("exists"):
            rep.add(CheckResult(
                "weaviate", "fail",
                detail=(f"v{ver} at {url}, but class "
                          f"`{cname}` is missing"),
                fix=("Restart Claude Code — SessionStart will "
                       "auto-repair the class. Or manually:\n"
                       f"  python {_plugin_root()}/scripts/install.py "
                       f"--auto"),
            ))
        else:
            rep.add(CheckResult(
                "weaviate", "warn",
                detail=(f"v{ver} at {url}, class check error: "
                          f"{wv_status.get('error')}"),
            ))
        return True

    rep.add(CheckResult("weaviate", "ok",
                          detail=(f"v{ver} at {url}, modules="
                                    f"{list(data.get('modules', {}))}, "
                                    f"class `{cname}` ok")))
    return True


def _check_symbol_index(cfg: dict, rep: Report) -> None:
    """Did anyone actually run reindex on at least one project?"""
    try:
        import psycopg2  # noqa
    except Exception:
        rep.add(CheckResult("symbol_index", "skip",
                            detail="psycopg2 unavailable in this interpreter"))
        return
    import psycopg2
    try:
        conn = psycopg2.connect(
            host=cfg.get("PG_HOST", "localhost"),
            port=cfg.get("PG_PORT", "5432"),
            user=cfg.get("PG_USER", "postgres"),
            password=cfg.get("PG_PASS", "postgres"),
            dbname=cfg.get("PG_DB") or "postgres",
            connect_timeout=2,
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT count(DISTINCT project_slug), count(*) FROM symbols")
            projects, syms = cur.fetchone()
        conn.close()
    except Exception as e:
        rep.add(CheckResult("symbol_index", "warn",
                            detail=f"couldn't query symbols table: {e}"))
        return
    if syms == 0:
        rep.add(CheckResult(
            "symbol_index", "warn",
            detail="no symbols indexed yet (lint hook will silent-pass until first reindex)",
            fix="cd <project>; claude-dejavu reindex",
        ))
    else:
        rep.add(CheckResult("symbol_index", "ok",
                            detail=f"{syms} symbols across {projects} project(s)"))


def _check_perf_class(cfg: dict, rep: Report) -> None:
    """Surface the machine's local-LLM performance classification.

    Set by `claude-dejavu bench`. Used by `code/summarize.py` to decide
    whether to put cloud providers first in the cascade. Doctor surfaces
    this so users on slow hardware know why their session-end summaries
    are slow + that the Pro tier solves it.

    Status semantics:
      - ok    : perf_class=fast (machine handles local Ollama gist
                under threshold)
      - skip  : perf_class=untested (bench has never been run; not a
                problem, just informational)
      - warn  : perf_class=slow (machine struggles; nudge user toward
                Pro tier or cloud API key)
    """
    perf = (cfg.get("PERF_CLASS") or "").strip().lower()
    benched_at = cfg.get("PERF_BENCHED_AT", "")

    if not perf or perf == "untested":
        rep.add(CheckResult(
            "perf_class", "skip",
            detail="not yet benchmarked — run `claude-dejavu bench --apply`",
        ))
        return

    if perf == "fast":
        rep.add(CheckResult(
            "perf_class", "ok",
            detail=f"local LLM is fast on this machine"
                   f"{f' (benched {benched_at})' if benched_at else ''}",
        ))
        return

    # perf == "slow"
    rep.add(CheckResult(
        "perf_class", "warn",
        detail=(
            f"local Ollama is slow on this machine"
            f"{f' (benched {benched_at})' if benched_at else ''}; "
            "session-end summaries fall back to cloud (if API key set) "
            "or rule-based digest"
        ),
        fix=(
            "Add a cloud API key (ANTHROPIC_API_KEY / GEMINI_API_KEY / "
            "OPENROUTER_API_KEY) for fast summaries, OR upgrade to "
            "claude-dejavu Pro (managed cloud gists, sub-100ms, "
            "no key setup required) when v0.6 ships."
        ),
    ))


def _check_error_log(rep: Report) -> None:
    """Surface the error log path + a quick-look at recent activity.
    Helps diagnostic forwarding: a user filing an issue can attach
    the log directly. Never fails — error logging not having errors
    is the good path."""
    try:
        from error_log import (read_recent, log_path_str, _log_path)
    except Exception as e:  # noqa: BLE001
        rep.add(CheckResult("error_log", "warn",
                              detail=f"error_log module unavailable: {e}"))
        return

    path = _log_path()
    if not path.exists():
        rep.add(CheckResult(
            "error_log", "ok",
            detail=f"no errors logged yet  ({log_path_str()})"))
        return

    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    # Count by level over the last 200 records.
    recs = read_recent(limit=200)
    n_err = sum(1 for r in recs if r.get("level") == "error")
    n_warn = sum(1 for r in recs if r.get("level") == "warning")
    n_info = sum(1 for r in recs
                   if r.get("level") not in ("error", "warning"))
    last_24h = 0
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc)
                    - timedelta(hours=24)).isoformat()
        last_24h = sum(1 for r in recs if r.get("ts", "") >= cutoff)
    except Exception:  # noqa: BLE001
        pass

    detail = (f"{len(recs)} record(s) in tail "
                f"(error={n_err}  warn={n_warn}  info={n_info}  "
                f"last_24h={last_24h})  size={size}B  {log_path_str()}")
    # The error log existing + having records means the plugin has
    # been used. Don't fail doctor over historical errors — at most
    # warn, and only when there are recent ones the user might want
    # to look at. Doctor's overall should reflect SYSTEM HEALTH, not
    # diagnostic-log activity.
    status = "ok" if n_err == 0 else "warn"
    fix = (None if status == "ok"
             else f"Recent errors logged. Inspect with: "
                    f"claude-dejavu logs --with-traceback "
                    f"--component=...  (log: {log_path_str()})")
    rep.add(CheckResult("error_log", status, detail=detail, fix=fix))


def _check_ingest_coverage(cfg: dict, rep: Report) -> None:
    """Detect ingest coverage gaps: jsonl files on disk that don't have a
    matching cursor in the index.

    Why this matters: silent ingest failures (NUL-byte rows, transaction
    aborts, encoding errors) used to leave entire sessions un-indexed
    while the system still reported 'OK' — fatal for a memory plugin.
    This check makes those gaps loud.

    'fix' field tells the user the exact recovery command. Doctor stays
    'warn' rather than 'fail' so the rest of the system still reports
    health, but the gap is visible.
    """
    live_root = Path.home() / ".claude" / "projects"
    if not live_root.is_dir():
        rep.add(CheckResult("ingest_coverage", "skip",
                            detail="no ~/.claude/projects/ directory"))
        return

    # Walk live tree, collect every jsonl session id.
    on_disk: list[tuple[str, str, int]] = []  # (session_id, project, size_bytes)
    for proj in sorted(live_root.iterdir()):
        if not proj.is_dir():
            continue
        for f in sorted(proj.iterdir()):
            if not f.name.endswith(".jsonl"):
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            sid = f.name[:-len(".jsonl")]
            on_disk.append((sid, proj.name, size))

    if not on_disk:
        rep.add(CheckResult("ingest_coverage", "ok",
                            detail="no jsonl files on disk to ingest"))
        return

    try:
        import psycopg2  # noqa
    except Exception:
        rep.add(CheckResult("ingest_coverage", "skip",
                            detail="psycopg2 unavailable in this interpreter"))
        return

    try:
        conn = psycopg2.connect(
            host=cfg.get("PG_HOST", "localhost"),
            port=cfg.get("PG_PORT", "5432"),
            user=cfg.get("PG_USER", "postgres"),
            password=cfg.get("PG_PASS", "postgres"),
            dbname=cfg.get("PG_DB") or "postgres",
            connect_timeout=2,
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT session_id::text, last_byte FROM ingest_cursors")
            cursors = {row[0]: row[1] or 0 for row in cur.fetchall()}
        conn.close()
    except Exception as e:
        rep.add(CheckResult("ingest_coverage", "warn",
                            detail=f"couldn't query ingest_cursors: {e}"))
        return

    missing: list[str] = []   # never ingested at all
    stale: list[str] = []     # ingested but cursor lags > 100KB behind file size
    STALE_BYTES = 102_400     # 100 KB drift threshold
    for sid, proj, size in on_disk:
        last_byte = cursors.get(sid)
        if last_byte is None:
            missing.append(sid)
        elif size - last_byte > STALE_BYTES:
            stale.append(f"{sid[:8]}({(size - last_byte) / 1024:.0f}KB behind)")

    n_total = len(on_disk)
    n_indexed = n_total - len(missing)
    summary_parts = [f"{n_indexed}/{n_total} sessions indexed"]
    if missing:
        summary_parts.append(f"{len(missing)} never ingested")
    if stale:
        summary_parts.append(f"{len(stale)} stale")
    detail = " · ".join(summary_parts)

    if not missing and not stale:
        rep.add(CheckResult("ingest_coverage", "ok", detail=detail))
        return

    fix_parts = []
    if missing:
        fix_parts.append("claude-dejavu reingest --missing-only")
    if stale and not missing:
        fix_parts.append("claude-dejavu reingest --session <SID>  (or full: --reingest)")
    rep.add(CheckResult(
        "ingest_coverage", "warn",
        detail=detail,
        fix="  ".join(fix_parts),
    ))


def _check_hooks_wiring(rep: Report) -> None:
    h = _plugin_root() / "hooks" / "hooks.json"
    if not h.exists():
        rep.add(CheckResult("hooks", "fail",
                            detail=f"missing {h}", fix="claude plugin update claude-dejavu"))
        return
    try:
        d = json.loads(h.read_text())
    except Exception as e:
        rep.add(CheckResult("hooks", "fail", detail=f"unparseable: {e}"))
        return
    have = set(d.get("hooks", {}).keys())
    need = {"Setup", "SessionStart", "Stop", "SessionEnd", "PreToolUse"}
    missing = need - have
    if missing:
        rep.add(CheckResult(
            "hooks", "warn",
            detail=f"hooks.json missing events: {sorted(missing)}",
            fix="claude plugin update claude-dejavu",
        ))
    else:
        rep.add(CheckResult("hooks", "ok", detail=f"{len(have)} events wired"))


# ─── Public entry points ───────────────────────────────────────────────────


def run_doctor(verbose: bool = False) -> Report:
    rep = Report(plugin_version=_plugin_version())
    cfg = _load_cfg()
    _check_install_status_marker(rep)
    if rep.install_status and rep.install_status.get("status") == "incomplete":
        # Don't bother with downstream checks — they'll all fail until install completes.
        rep.add(CheckResult("downstream_checks", "skip",
                            detail="skipping (install incomplete)"))
        return rep
    _check_venv(rep)
    _check_postgres(cfg, rep)
    _check_weaviate(cfg, rep)
    _check_symbol_index(cfg, rep)
    _check_ingest_coverage(cfg, rep)
    _check_perf_class(cfg, rep)
    _check_hooks_wiring(rep)
    _check_error_log(rep)
    return rep


def report_to_text(rep: Report) -> str:
    lines = [f"claude-dejavu doctor — overall: {rep.overall.upper()}  (v{rep.plugin_version})"]
    icon = {"ok": "✓", "warn": "⚠", "fail": "✗", "skip": "•"}
    for c in rep.checks:
        lines.append(f"  {icon.get(c.status, '?')} {c.name:18s} {c.detail}")
        if c.fix:
            for fl in c.fix.splitlines():
                lines.append(f"      → {fl}")
    return "\n".join(lines)


def report_to_dict(rep: Report) -> dict:
    return {
        "overall": rep.overall,
        "plugin_version": rep.plugin_version,
        "install_status": rep.install_status,
        "checks": [asdict(c) for c in rep.checks],
    }


if __name__ == "__main__":
    rep = run_doctor()
    print(report_to_text(rep))
    sys.exit({"ok": 0, "warn": 1, "fail": 2}.get(rep.overall, 0))
