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
import urllib.error
import urllib.request
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


def _check_repo_identity(cfg: dict, rep: Report) -> None:
    """Verify cwd resolves to a stable repo_id; flag missing git root.

    Three outcomes:
      ok:    resolver returned a UUID + the resolved root is a git repo
             (or has a pre-existing .dejavu/id from a previous resolve).
      warn:  resolver fell back to cwd because no .git was found above.
      fail:  .dejavu/id exists but is malformed.
      skip:  repo_id module not importable (defensive — should never
             happen in a working install).
    """
    try:
        import repo_id as _rid
    except Exception as e:
        rep.add(CheckResult(
            "repo_identity", "skip",
            detail=f"could not import repo_id module: {e}"))
        return
    try:
        rid, rroot = _rid.resolve(start=Path.cwd())
    except _rid.MalformedRepoIdError as e:
        rep.add(CheckResult(
            "repo_identity", "fail",
            detail=f"malformed .dejavu/id: {e}",
            fix="Inspect <repo_root>/.dejavu/id; delete it to regenerate.",
        ))
        return
    # Was the repo root the same as cwd AND no .git above it?
    if rroot.resolve() == Path.cwd().resolve() and not (rroot / ".git").exists():
        rep.add(CheckResult(
            "repo_identity", "warn",
            detail=f"no git root above cwd; using cwd as repo boundary ({rid})",
            fix="If this isn't intended, cd into a git repo and re-run.",
        ))
        return
    rep.add(CheckResult(
        "repo_identity", "ok",
        detail=f"{rroot.name} ({rid})"))


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


def _docker_exec_psql_available() -> bool:
    """True iff `docker exec claude-dejavu-postgres psql --version` works.

    Used as a Windows-friendly fallback when the host doesn't have
    psql/pg_isready on PATH but the bundled container is running.
    Without this, doctor would report a misleading "no psql or PG_DB"
    warning even on a fully healthy install (PG_DB is in config.env;
    only host psql is missing)."""
    if not shutil.which("docker"):
        return False
    r = subprocess.run(
        ["docker", "exec", "claude-dejavu-postgres",
          "psql", "--version"],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=5,
    )
    return r.returncode == 0


def _run_psql_dejavu(host: str, port: str, user: str, password: str,
                      db: str | None, args: list[str], *,
                      use_docker_exec: bool,
                      ) -> subprocess.CompletedProcess:
    """Mirror of install._run_psql for doctor — host psql first, else
    `docker exec claude-dejavu-postgres psql` against the bundled
    container."""
    env = {**os.environ, "PGPASSWORD": password}
    if not use_docker_exec:
        psql = shutil.which("psql") or "psql"
        cmd = [psql, "-h", host, "-p", str(port), "-U", user]
        if db:
            cmd += ["-d", db]
        cmd += args
        return subprocess.run(cmd, env=env, capture_output=True,
                                  text=True, encoding="utf-8",
                                  errors="replace", timeout=5)
    cmd = ["docker", "exec", "-i",
            "-e", f"PGPASSWORD={password}",
            "claude-dejavu-postgres", "psql",
            "-h", "localhost", "-p", "5432", "-U", user]
    if db:
        cmd += ["-d", db]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=8)


def _check_postgres(cfg: dict, rep: Report) -> bool:
    host = cfg.get("PG_HOST", "localhost")
    port = cfg.get("PG_PORT", "5432")
    user = cfg.get("PG_USER", "postgres")
    password = cfg.get("PG_PASS", "postgres")
    db = cfg.get("PG_DB", "")
    psql = shutil.which("psql")
    pg_isready = shutil.which("pg_isready")
    # Windows installs (and any *nix box without postgres-client)
    # have neither binary on PATH but DO have the bundled container.
    # Mirror install._run_psql's behavior: fall back to
    # `docker exec claude-dejavu-postgres psql` so doctor can still
    # verify the schema. Without this, doctor warns "no psql or
    # PG_DB" on a fully healthy Windows install.
    use_docker_exec = (not psql) and _docker_exec_psql_available()

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
    elif use_docker_exec:
        # Probe via the bundled container's pg_isready. Doesn't go
        # through host networking — verifies the container itself is
        # serving rather than the host port. That's actually what we
        # want here: install.py's earlier port-mapping fix already
        # ensures config.env carries the correct host port; this
        # check just confirms the cluster is alive.
        r = subprocess.run(
            ["docker", "exec", "claude-dejavu-postgres",
              "pg_isready", "-U", user, "-d", "postgres", "-t", "3"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
        if r.returncode != 0:
            rep.add(CheckResult(
                "postgres", "fail",
                detail=("docker exec pg_isready says container is "
                          "not accepting connections"),
                fix=("Start the bundled stack:\n"
                     f"  docker compose -f {_plugin_root()}/docker/docker-compose.minimal.yml up -d"),
            ))
            return False
    elif not psql:
        rep.add(CheckResult(
            "postgres", "skip",
            detail="neither pg_isready nor psql nor bundled container found; cannot verify",
        ))
        return False

    # Verify schema by counting expected tables. With the docker-exec
    # fallback, we no longer need host psql to do this — only PG_DB.
    if (not psql and not use_docker_exec) or not db:
        if not db:
            detail = (f"reachable on {host}:{port} but PG_DB is empty in "
                          f"config.env — re-run scripts/install.py --auto")
        else:
            detail = (f"reachable on {host}:{port} but no psql on PATH "
                          f"and bundled container not running for docker-exec "
                          f"fallback")
        rep.add(CheckResult("postgres", "warn", detail=detail))
        return True
    r = _run_psql_dejavu(
        host, port, user, password, db,
        ["-tA", "-c",
         "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"],
        use_docker_exec=use_docker_exec,
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
        r2 = _run_psql_dejavu(
            host, port, user, password, db,
            ["-tA", "-c",
              "SELECT table_name FROM information_schema.tables "
              "WHERE table_schema='public'"],
            use_docker_exec=use_docker_exec,
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
    cname = "?"  # default for the error-path message
    try:
        from health import (check_weaviate_class,
                                expected_weaviate_class)
    except ImportError as e:
        # `health` not on path — treat as "can't verify" rather than
        # "class missing", since the class might be perfectly fine.
        rep.add(CheckResult(
            "weaviate", "skip",
            detail=(f"v{ver} at {url}, class verification skipped "
                    f"(health module unavailable: {e})"),
        ))
        return True  # Weaviate itself is up, that's all we proved
    try:
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
            detail="not yet benchmarked — run `python "
                    "${CLAUDE_DEJAVU_PLUGIN_ROOT:-$(claude-dejavu about "
                    "--plugin-root)}/code/bench_local.py --apply`",
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


def _check_cloud_embed(cfg: dict, rep: Report) -> None:
    """Check cloud embedding mode health.

    Surfaces 4 states:
      - skip  : DEJAVU_EMBED_MODE != "cloud" (user is on local mode)
      - warn  : cloud mode set but DEJAVU_CLOUD_KEY missing
      - fail  : worker unreachable / key invalid / quota exhausted
      - ok    : worker reachable, key valid, embed path returns 1024-dim vectors
    """
    mode = (os.environ.get("DEJAVU_EMBED_MODE")
            or cfg.get("DEJAVU_EMBED_MODE", "local")).strip().lower()
    if mode != "cloud":
        rep.add(CheckResult(
            "cloud_embed", "skip",
            detail="local mode (set DEJAVU_EMBED_MODE=cloud + DEJAVU_CLOUD_KEY to enable)",
        ))
        return

    key = (os.environ.get("DEJAVU_CLOUD_KEY") or cfg.get("DEJAVU_CLOUD_KEY", "")).strip()
    url = (os.environ.get("DEJAVU_CLOUD_URL")
           or cfg.get("DEJAVU_CLOUD_URL", "https://embed.hte.digital")).rstrip("/")

    if not key:
        rep.add(CheckResult(
            "cloud_embed", "warn",
            detail=f"cloud mode active but DEJAVU_CLOUD_KEY is empty",
            fix="set DEJAVU_CLOUD_KEY to a dvk_live_… Pro key (run `dejavu login`)",
        ))
        return

    # Liveness ping
    try:
        with urllib.request.urlopen(f"{url}/v1/ping", timeout=5) as r:
            ping = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        rep.add(CheckResult(
            "cloud_embed", "fail",
            detail=f"cloud worker unreachable at {url}: {type(e).__name__}: {e}",
        ))
        return

    # Auth + quota check via /v1/whoami — costs ZERO cloud tokens.
    # (Earlier versions called /v1/embed which consumed ~10 tokens per
    # doctor run; that surprised users on tight quotas. Whoami is free
    # and surfaces the same auth/quota signals.)
    req = urllib.request.Request(
        f"{url}/v1/whoami", method="GET",
        headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_txt = e.read()[:200].decode(errors="replace")
        if e.code == 401:
            rep.add(CheckResult(
                "cloud_embed", "fail",
                detail=f"key rejected (401): {body_txt}",
                fix="run `dejavu logout && dejavu login` to refresh the key",
            ))
        elif e.code == 402:
            rep.add(CheckResult(
                "cloud_embed", "fail",
                detail=f"monthly quota exhausted (402): {body_txt}",
                fix="upgrade plan or wait until next month bucket",
            ))
        elif e.code == 429:
            rep.add(CheckResult(
                "cloud_embed", "warn",
                detail=f"rate-limited (429): {body_txt} (transient)",
            ))
        else:
            rep.add(CheckResult(
                "cloud_embed", "fail",
                detail=f"whoami HTTP {e.code}: {body_txt}",
            ))
        return
    except Exception as e:  # noqa: BLE001
        rep.add(CheckResult(
            "cloud_embed", "fail",
            detail=f"whoami error: {type(e).__name__}: {e}",
        ))
        return

    cap = data.get("monthly_cap", 0)
    used = data.get("quota_used", 0)
    remaining = data.get("quota_remaining", 0)
    pct = (used / cap * 100) if cap else 0
    status = "ok"
    if pct > 90:
        status = "warn"
    rep.add(CheckResult(
        "cloud_embed", status,
        detail=(f"cloud at {url} ({ping.get('version', '?')}) ok — "
                f"tier={data.get('tier')} prefix={data.get('prefix')} "
                f"used={used}/{cap} ({pct:.1f}%) remaining={remaining}"),
        fix=("approaching cap — consider upgrading or wait for next month"
             if pct > 90 else None),
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


# ─── v0.8.0 — session-copy health section (spec §3.4) ──────────────────────


def _check_session_copy(rep: Report) -> None:
    """Four checks on the v0.8.0 restic-backed session-copy subsystem:

      1. Repo reachable (read metadata via `session_copy.list_snapshots`)
      2. Last-snapshot age < 48h (warn at > 48h; OK if < 24h)
      3. Repo size under 20 GB (warn) / 50 GB (crit)
      4. `restic check` clean — via session_copy.check(max_age_hours=24)

    Lazy-imports session_copy so doctor still runs cleanly when restic
    isn't installed yet — in that case we emit a single 'skip' note
    instead of failing four separate checks.
    """
    data_root = _data_root()
    repo_path = data_root / "restic-repo"
    pass_file = data_root / ".restic-pass"
    if not repo_path.is_dir() or not pass_file.is_file():
        rep.add(CheckResult(
            "session_copy", "skip",
            detail="session-copy not configured (run "
                    "`claude-dejavu session-copy init`)",
            fix="claude-dejavu session-copy init"))
        return

    try:
        sys.path.insert(0, str(_plugin_root() / "code"))
        import session_copy  # type: ignore[import]
    except ImportError as exc:
        rep.add(CheckResult(
            "session_copy", "fail",
            detail=f"session_copy module unavailable: {exc}",
            fix="re-run scripts/install.py"))
        return

    spec = session_copy.RepoSpec(
        repo_path=repo_path,
        passphrase_file=pass_file,
        bin_path=data_root / "bin" / "restic",
    )

    # Check 1 + 2 + 3 via session_copy.status() (it calls list_snapshots
    # internally, which doubles as the reachable probe).
    try:
        st = session_copy.status(spec)
    except Exception as exc:
        rep.add(CheckResult(
            "session_copy.reachable", "fail",
            detail=f"repo unreachable: {exc}",
            fix="claude-dejavu session-copy check --force"))
        return
    rep.add(CheckResult(
        "session_copy.reachable", "ok",
        detail=f"{st['snapshot_count']} snapshot(s) at {st['repo_path']}"))

    # Check 2 — last-snapshot age. Warn at > 48h (2× the default doctor
    # cadence) given the legacy migration window may have just landed.
    age_s = st.get("last_snapshot_age_s")
    if age_s is None:
        rep.add(CheckResult(
            "session_copy.last_snapshot_age", "warn",
            detail="no snapshots yet",
            fix="claude-dejavu session-copy take"))
    elif age_s > 48 * 3600:
        rep.add(CheckResult(
            "session_copy.last_snapshot_age", "warn",
            detail=f"last snapshot was {age_s/3600:.1f}h ago (> 48h)",
            fix="check scheduler: claude-dejavu doctor"))
    else:
        rep.add(CheckResult(
            "session_copy.last_snapshot_age", "ok",
            detail=f"{age_s/3600:.1f}h ago"))

    # Check 3 — repo size threshold (warn at > 20 GB, crit at > 50 GB).
    size_bytes = st.get("repo_size") or 0
    size_gb = size_bytes / (1024 ** 3)
    if size_gb > 50:
        rep.add(CheckResult(
            "session_copy.repo_size", "fail",
            detail=f"repo size {size_gb:.1f} GB > 50 GB threshold",
            fix="claude-dejavu session-copy prune"))
    elif size_gb > 20:
        rep.add(CheckResult(
            "session_copy.repo_size", "warn",
            detail=f"repo size {size_gb:.1f} GB > 20 GB",
            fix="claude-dejavu session-copy prune --dry-run"))
    else:
        rep.add(CheckResult(
            "session_copy.repo_size", "ok",
            detail=f"{size_gb:.2f} GB"))

    # Check 4 — `restic check`, cached to 24h (max_age_hours=24).
    try:
        r = session_copy.check(spec, max_age_hours=24, force=False)
    except Exception as exc:
        rep.add(CheckResult(
            "session_copy.check", "fail",
            detail=f"restic check raised: {exc}",
            fix="claude-dejavu session-copy check --force"))
        return
    if r.get("ok"):
        rep.add(CheckResult(
            "session_copy.check", "ok",
            detail=("cached" if r.get("cached") else "ran") + " — clean"))
    else:
        rep.add(CheckResult(
            "session_copy.check", "fail",
            detail=f"restic check FAILED:\n{(r.get('output') or '')[:300]}",
            fix="claude-dejavu session-copy check --force"))


# ─── v0.8.2: Weaviate shard rescue-pending probe ───────────────────────


def _check_weaviate_shard_health(cfg: dict, rep: Report) -> None:
    """Probe Weaviate's /v1/nodes endpoint for shards in an unloaded /
    error state. When any class has a sick shard, surface a `rescue:
    pending — run claude-dejavu rescue-shard --class <C>` warning.

    Skipped if WV_URL isn't configured or Weaviate isn't reachable
    (the _check_weaviate gate already surfaces that as a fail).
    """
    url = cfg.get("WV_URL")
    if not url:
        return
    try:
        req = urllib.request.Request(f"{url}/v1/nodes")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
    except Exception:  # noqa: BLE001
        # Don't double-report — _check_weaviate already covered this.
        return

    sick_classes: set[str] = set()
    nodes = data.get("nodes") or []
    for node in nodes:
        shards = node.get("shards") or []
        for sh in shards:
            status = (sh.get("status") or "").upper()
            cls = sh.get("class") or sh.get("className") or ""
            if not cls:
                continue
            # READY / INDEXING are healthy. Anything else (UNLOADED,
            # READONLY, FROZEN_UPLOAD_FAILED, etc.) is suspect.
            if status and status not in ("READY", "INDEXING"):
                sick_classes.add(cls)
    if not sick_classes:
        rep.add(CheckResult(
            "weaviate_shards", "ok",
            detail=f"all shards across {len(nodes)} node(s) READY"))
        return

    for cls in sorted(sick_classes):
        rep.add(CheckResult(
            f"weaviate_shards.{cls}", "fail",
            detail=f"one or more shards in class {cls!r} are not READY",
            fix=f"rescue: pending — run "
                f"`claude-dejavu rescue-shard --class {cls} --dry-run` "
                f"first to inspect, then re-run without --dry-run to apply"))


# ─── Public entry points ───────────────────────────────────────────────────


def _check_pg_rescue_deep(rep: Report) -> None:
    """v0.8.3 — invoke pg_rescue.scan() and surface corrupt rows + indexes.

    Each finding includes the exact ``pg-rescue`` command that fixes it.
    Used only when ``run_doctor(deep=True)``.
    """
    try:
        sys.path.insert(0, str(_plugin_root() / "code"))
        import pg_rescue as _pg  # type: ignore[import]
    except Exception as exc:
        rep.add(CheckResult("pg_rescue_scan", "skip",
                              detail=f"pg_rescue module unavailable: {exc}"))
        return
    try:
        r = _pg.scan()
    except Exception as exc:
        rep.add(CheckResult("pg_rescue_scan", "warn",
                              detail=f"scan failed: {exc}",
                              fix="claude-dejavu doctor (re-run with PG up)"))
        return
    n_rows = len(r.get("corrupt_rows", []))
    n_idx = len(r.get("corrupt_indexes", []))
    n_pages = len(r.get("corrupt_pages", []))
    if n_rows == 0 and n_idx == 0 and n_pages == 0:
        rep.add(CheckResult("pg_rescue_scan", "ok",
                              detail=(f"probed {r.get('probed_rows', 0)} "
                                          f"rows, no corruption")))
        return
    detail_parts = []
    if n_rows:
        detail_parts.append(f"{n_rows} corrupt row(s)")
    if n_idx:
        detail_parts.append(f"{n_idx} invalid index(es)")
    if n_pages:
        detail_parts.append(f"{n_pages} unreadable table(s)")
    rep.add(CheckResult(
        "pg_rescue_scan", "fail",
        detail="; ".join(detail_parts),
        fix=("claude-dejavu pg-rescue patch-toast --yes  "
              "# if corrupt rows are turns.content TOAST chunks\n"
              "claude-dejavu pg-rescue reindex --yes  "
              "# if indexes are invalid\n"
              "claude-dejavu pg-rescue rebuild-table <table> --yes  "
              "# last resort for irreparable rows"),
    ))


def _check_weaviate_rescue_deep(rep: Report) -> None:
    """v0.8.3 — invoke weaviate_rescue.scan() to surface dim mismatches +
    unhealthy shards + missing classes."""
    try:
        sys.path.insert(0, str(_plugin_root() / "code"))
        import weaviate_rescue as _wv  # type: ignore[import]
    except Exception as exc:
        rep.add(CheckResult("weaviate_rescue_scan", "skip",
                              detail=f"weaviate_rescue unavailable: {exc}"))
        return
    try:
        r = _wv.scan()
    except Exception as exc:
        rep.add(CheckResult("weaviate_rescue_scan", "warn",
                              detail=f"scan failed: {exc}",
                              fix="claude-dejavu doctor (re-run with WV up)"))
        return
    # why: scan() catches its own network errors and returns
    # {reachable: False, ...} with the rest of the fields as defaults
    # (zero-length lists, embed_dim=None). Without this guard the
    # caller misreads "no unhealthy_shards listed" as "all shards
    # READY". Caught in 2026-05-18 hell test H4: doctor falsely
    # reported `✓ weaviate_rescue_scan all shards READY` while
    # Weaviate was stopped.
    if not r.get("reachable", False):
        rep.add(CheckResult(
            "weaviate_rescue_scan", "fail",
            detail="Weaviate unreachable — scan could not run",
            fix="start the Weaviate container, then re-run "
                "`claude-dejavu doctor --deep`"))
        return
    issues: list[str] = []
    fix_parts: list[str] = []
    if r.get("dim_mismatches"):
        for dm in r["dim_mismatches"]:
            issues.append(
                f"{dm['class']} at {dm['actual']}-dim (cloud emits "
                f"{dm['expected']}-dim)")
            fix_parts.append(
                f"claude-dejavu weaviate-rescue redo-class "
                f"{dm['class']} --reset-pg-flags --yes")
    if r.get("unhealthy_shards"):
        for sh in r["unhealthy_shards"]:
            issues.append(
                f"shard {sh['shard']} of {sh['class']} status="
                f"{sh['status']}")
            fix_parts.append(
                f"claude-dejavu rescue-shard --class {sh['class']} "
                f"--shard {sh['shard']}")
    if r.get("missing_classes"):
        issues.append(f"missing classes: {r['missing_classes']}")
    if not issues:
        rep.add(CheckResult(
            "weaviate_rescue_scan", "ok",
            detail=f"all shards READY, embed_dim={r.get('embed_dim')}"))
        return
    rep.add(CheckResult(
        "weaviate_rescue_scan", "fail",
        detail="; ".join(issues),
        fix="\n".join(fix_parts) if fix_parts else "",
    ))


def run_doctor(verbose: bool = False, deep: bool = False) -> Report:
    rep = Report(plugin_version=_plugin_version())
    cfg = _load_cfg()
    _check_install_status_marker(rep)
    if rep.install_status and rep.install_status.get("status") == "incomplete":
        # Don't bother with downstream checks — they'll all fail until install completes.
        rep.add(CheckResult("downstream_checks", "skip",
                            detail="skipping (install incomplete)"))
        return rep
    _check_venv(rep)
    _check_repo_identity(cfg, rep)
    _check_postgres(cfg, rep)
    _check_weaviate(cfg, rep)
    _check_weaviate_shard_health(cfg, rep)
    _check_symbol_index(cfg, rep)
    _check_ingest_coverage(cfg, rep)
    _check_perf_class(cfg, rep)
    _check_cloud_embed(cfg, rep)
    _check_hooks_wiring(rep)
    _check_session_copy(rep)
    _check_error_log(rep)
    if deep:
        # v0.8.3 — destructive diagnostics that walk every row + every
        # shard. Disabled by default because they're slow on large DBs.
        _check_pg_rescue_deep(rep)
        _check_weaviate_rescue_deep(rep)
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
