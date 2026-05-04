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
        rep.add(CheckResult("weaviate", "ok",
                            detail=f"v{ver} at {url}, modules={list(data.get('modules', {}))}"))
        return True
    except Exception as e:
        rep.add(CheckResult(
            "weaviate", "fail",
            detail=f"{url} not responding: {type(e).__name__}: {e}",
            fix=("Start the bundled stack:\n"
                 f"  docker compose -f {_plugin_root()}/docker/docker-compose.minimal.yml up -d"),
        ))
        return False


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
