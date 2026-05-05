#!/usr/bin/env python3
"""
claude-dejavu SessionStart hook (v0.2).

When a new Claude Code session opens, inject context from past sessions in the
SAME project / workspace:
  1. Most recent session summary (request / completed / next_steps)
  2. Top-3 observations relevant to the project
  3. Optional handoff cue from `next_steps`

Output is a compact ~80-token preamble. Token-disciplined: no full-content dumps.

Reads {session_id, transcript_path} from stdin (the new session). Returns
JSON with `systemMessage` containing the injection text (Claude reads this on
startup).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "code"))


def _resolve_data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "claude-dejavu"
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else Path.home() / ".local" / "share" / "claude-dejavu"


def _load_cfg():
    cfg = {}
    f = _resolve_data_root() / "config.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
    return cfg


def _pg_dsn(cfg) -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    db = cfg.get("PG_DB") or f"claude_dejavu_{user}"
    return (
        f"host={cfg.get('PG_HOST','localhost')} port={cfg.get('PG_PORT','5432')} "
        f"user={cfg.get('PG_USER','postgres')} password={cfg.get('PG_PASS','postgres')} "
        f"dbname={db}"
    )


def _project_slug_from_cwd_or_env() -> str:
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return os.path.basename(os.path.realpath(cwd))


def _local_plugin_version() -> str | None:
    """Read version from .claude-plugin/plugin.json (one dir above ours)."""
    try:
        plugin_root = Path(__file__).resolve().parent.parent
        manifest = plugin_root / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            return json.loads(manifest.read_text()).get("version")
    except Exception:
        pass
    return None


def _check_for_update(local_version: str | None, cfg: dict) -> str | None:
    """Probe GitHub for the latest tag and return a one-line update notice.

    Cached for 24h in DATA_ROOT/last_update_check to avoid hammering the API.
    Honors `UPDATE_CHECK=false` in config.env, set CLAUDE_DEJAVU_NO_UPDATE_CHECK=1
    to disable per-session.
    """
    if not local_version:
        return None
    if os.environ.get("CLAUDE_DEJAVU_NO_UPDATE_CHECK") == "1":
        return None
    if cfg.get("UPDATE_CHECK", "true").lower() in ("0", "false", "no", "off"):
        return None

    import time
    cache = _resolve_data_root() / "last_update_check"
    cached_remote: str | None = None
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if time.time() - data.get("ts", 0) < 86400:  # 24h
                cached_remote = data.get("remote")
        except Exception:
            pass

    remote_version = cached_remote
    if remote_version is None:
        # Use `git ls-remote --tags` so this works for both private (SSH auth)
        # and public repos without needing a GitHub API token.
        try:
            import subprocess, re as _re
            r = subprocess.run(
                ["git", "ls-remote", "--tags",
                 "git@github.com:HthSolid/claude-dejavu.git"],
                capture_output=True, text=True, timeout=4,
            )
            tags: list[tuple[int, ...]] = []
            for line in (r.stdout or "").splitlines():
                m = _re.search(r"refs/tags/v?(\d+\.\d+\.\d+)(?:\^\{\})?$", line)
                if m:
                    tags.append(tuple(int(x) for x in m.group(1).split(".")))
            if tags:
                top = max(tags)
                remote_version = ".".join(str(x) for x in top)
            try:
                cache.write_text(json.dumps({"ts": time.time(), "remote": remote_version}))
            except Exception:
                pass
        except Exception:
            return None

    if not remote_version:
        return None

    def vparts(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split(".")[:3])
        except Exception:
            return (0, 0, 0)

    if vparts(remote_version) > vparts(local_version):
        return (f"  ⮕ claude-dejavu update available: {local_version} → {remote_version}  "
                f"(claude plugin update claude-dejavu)")
    return None


def _read_install_status() -> str | None:
    """Read INSTALL_STATUS marker (written by scripts/install.py --auto)
    and format a Claude-readable systemMessage block. Returns None if
    install is in healthy 'done' state or marker is absent.

    The marker is the bridge between install.py (where errors normally
    only show in the terminal) and the user's IDE/VS-Code Claude session
    (where they can't see install.py output). When SessionStart sees an
    incomplete or in-progress install, it injects a block that tells
    Claude exactly what's wrong and what the user needs to do — Claude
    then relays that to the user in a way they will see no matter what
    UI they're in.
    """
    p = _resolve_data_root() / "INSTALL_STATUS"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    status = d.get("status")
    if status == "done":
        return None
    if status == "in_progress":
        stage = d.get("stage", "?")
        detail = d.get("detail", "")
        return (f"⚠ claude-dejavu install IN PROGRESS — current stage: '{stage}'.\n"
                f"  {detail}\n"
                f"  Tell the user to wait — typical first install is 1-5 minutes "
                f"(Docker image pull is the slow part). Subsequent sessions are instant.")
    if status == "incomplete":
        reason = d.get("reason_code", "unknown")
        summary = d.get("summary", "")
        fix = d.get("fix", "")
        fix_indented = "\n".join("  " + line for line in fix.splitlines())
        return ("⚠ claude-dejavu install INCOMPLETE — reason: " + reason + ".\n"
                "  " + summary + "\n"
                "\n"
                "  Tell the user to do the following, then re-open Claude Code:\n"
                + fix_indented + "\n"
                "\n"
                "  Once fixed, the Setup hook re-runs on next plugin update OR the "
                "user can run:  claude-dejavu doctor --fix")
    return None


def _kickoff_bootstrap_if_needed(conn) -> str | None:
    """Detect 'many live .jsonl files vs few DB sessions' and kick off
    a background bootstrap to import the missing ones. Mirrors the
    install.py auto-bootstrap path — runs whenever SessionStart sees
    a meaningful gap (which is the only time the user would notice).
    """
    home_data = _resolve_data_root()
    venv_py = home_data / "venv" / ("Scripts" if sys.platform.startswith("win") else "bin") / (
        "python.exe" if sys.platform.startswith("win") else "python3"
    )
    if not venv_py.exists():
        return None  # venv missing; the install_status path handles this

    # Count live .jsonl files
    live_root = Path.home() / ".claude" / "projects"
    if not live_root.is_dir():
        return None
    n_live = 0
    try:
        for proj_enc in os.listdir(live_root):
            pdir = live_root / proj_enc
            if pdir.is_dir():
                n_live += sum(1 for f in os.listdir(pdir) if f.endswith(".jsonl"))
    except OSError:
        return None
    if n_live == 0:
        return None  # no past sessions to import

    # Count DB sessions
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sessions")
            n_db = int(cur.fetchone()[0] or 0)
    except Exception:
        return None

    # Only kick off if the DB lags by a meaningful margin (avoid flap on
    # every single SessionStart). Threshold: live > DB + 2.
    if n_db >= n_live - 2:
        return None

    plugin_root = Path(__file__).resolve().parent.parent
    ingester = plugin_root / "code" / "ingester.py"
    if not ingester.exists():
        return None

    import subprocess
    try:
        log_path = home_data / "bootstrap.log"
        if sys.platform.startswith("win"):
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            with open(log_path, "ab") as logf:
                subprocess.Popen(
                    [str(venv_py), str(ingester), "--bootstrap"],
                    stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
        else:
            with open(log_path, "ab") as logf:
                subprocess.Popen(
                    [str(venv_py), str(ingester), "--bootstrap"],
                    stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                    start_new_session=True, close_fds=True,
                )
    except Exception:
        return None

    return (f"  ⮕ claude-dejavu: {n_live - n_db} past session(s) found in "
            f"~/.claude/projects/ but not yet in DB. Bootstrap running in "
            f"background — searches over those sessions will gradually become "
            f"available over the next few minutes.")


def _kickoff_reindex_if_needed(project_slug: str, conn) -> str | None:
    """If the project has no symbols indexed yet, spawn a background
    `claude-dejavu reindex` so the lint hook + dejavu_resolve_symbol have
    real data to ground against. Returns a one-line user-facing notice
    when a reindex was kicked off, else None.

    This is what makes the plugin work out-of-the-box: install + first
    SessionStart in a project = automatic indexing, no manual step.
    """
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if not Path(cwd).is_dir():
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM symbols WHERE project_slug = %s",
                (project_slug,),
            )
            n = cur.fetchone()[0]
    except Exception:
        return None
    if n > 0:
        return None  # already indexed

    # Find the claude-dejavu CLI. Prefer ~/.local/bin/claude-dejavu (the
    # symlink scripts/install.py creates) so the spawn matches the user's
    # installed CLI, not whatever happens to be on PATH first.
    candidates = [
        Path.home() / ".local" / "bin" / "claude-dejavu",
        Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps" / "claude-dejavu.bat",
    ]
    cli = next((str(c) for c in candidates if c.exists()), None)
    if not cli:
        # No CLI symlink found — user hasn't run install.py yet. Bail
        # silently rather than spam them.
        return None

    import subprocess
    try:
        # Detached background spawn. We DO NOT wait — the SessionStart
        # hook must return promptly. The reindex finishes asynchronously
        # (typically <60s for a 20k-file repo, <5s for small projects).
        # On Linux/macOS use Popen with stdin/out/err nulled and a new
        # session so it survives the parent hook exiting.
        if sys.platform.startswith("win"):
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                [cli, "reindex", "--path", cwd, "--project", project_slug],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                [cli, "reindex", "--path", cwd, "--project", project_slug],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
    except Exception:
        return None

    return (f"  ⮕ claude-dejavu: indexing project '{project_slug}' for symbol "
            f"grounding (running in background, ~seconds for small repos)…")


def _build_preamble(latest_summary: dict | None, observations: list[dict],
                    update_notice: str | None = None,
                    reindex_notice: str | None = None,
                    install_notice: str | None = None,
                    bootstrap_notice: str | None = None,
                    health_notice: str | None = None) -> str:
    if not any([latest_summary, observations, update_notice, reindex_notice,
                install_notice, bootstrap_notice, health_notice]):
        return ""
    lines = ["[claude-dejavu — context from prior sessions on this project]"]
    # Install status comes FIRST — if install is incomplete, nothing else
    # works and the user needs to see this before any normal preamble noise.
    if install_notice:
        lines.append(install_notice)
    # Health-repair notice next — if storage was just auto-repaired
    # the user needs to know (their data may have been lost; auto-
    # repair only restores schema, not contents).
    if health_notice:
        lines.append(health_notice)
    if update_notice:
        lines.append(update_notice)
    if bootstrap_notice:
        lines.append(bootstrap_notice)
    if reindex_notice:
        lines.append(reindex_notice)
    if latest_summary:
        if latest_summary.get("request"):
            lines.append(f"  last request: {latest_summary['request'][:200]}")
        if latest_summary.get("completed"):
            lines.append(f"  completed:    {latest_summary['completed'][:200]}")
        if latest_summary.get("next_steps"):
            lines.append(f"  next steps:   {latest_summary['next_steps'][:300]}")
    if observations:
        lines.append("  recent observations:")
        for o in observations[:3]:
            lines.append(f"    [{o['type']}] {o['title'][:120]}")
    lines.append("(suppress this preamble: dejavu config set inject_session_start false)")
    return "\n".join(lines)


def main():
    # Recursion guard for nested `claude -p` invocations from our summarizer.
    if os.environ.get("CLAUDE_DEJAVU_NESTED") == "1":
        print(json.dumps({"systemMessage": ""}))
        return

    raw = sys.stdin.read() or "{}"
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {}

    cfg = _load_cfg()
    if cfg.get("INJECT_SESSION_START", "true").lower() in ("0", "false", "no", "off"):
        print(json.dumps({"systemMessage": ""}))
        return

    # ─── Install status surfaces FIRST, even if everything else is broken ───
    # Before we try to import psycopg2 or hit the DB, check the install
    # marker. If install hasn't completed (Docker missing, daemon down,
    # mid-pull, etc.) the DB call would fail anyway — and the install
    # notice is what the user actually needs to see.
    install_notice = _read_install_status()

    project_slug = _project_slug_from_cwd_or_env()

    try:
        import psycopg2
    except Exception:
        # No psycopg2 — install almost certainly incomplete. Make sure the
        # install_notice gets through even without a DB connection.
        if install_notice:
            print(json.dumps({"systemMessage": _build_preamble(
                None, [], install_notice=install_notice)}))
        else:
            print(json.dumps({"systemMessage": ""}))
        return

    latest_summary = None
    observations: list[dict] = []
    reindex_notice: str | None = None
    bootstrap_notice: str | None = None
    health_notice: str | None = None
    try:
        with psycopg2.connect(_pg_dsn(cfg)) as conn:
            # Storage-integrity check + auto-repair. Closes the silent-
            # failure loop where Postgres or Weaviate is reachable but
            # our tables / classes are gone (orphan-container destruction
            # from Compose project-name collision, manual `docker compose
            # down -v`, fresh container against a stale config.env, etc.).
            # Without this, every query below would raise, get caught by
            # the broad except, and the user would see an empty preamble
            # with no idea why nothing is working.
            try:
                from health import (check_and_repair_storage,
                                          format_repair_notice)
                report = check_and_repair_storage(
                    conn=conn,
                    wv_url=cfg.get("WV_URL"),
                    auto_repair=(cfg.get("AUTO_HEAL_STORAGE", "true")
                                  .lower() not in ("0", "false", "no",
                                                     "off")),
                )
                health_notice = format_repair_notice(report)
            except Exception as e:  # noqa: BLE001
                # Health module load failure or unhandled error — still
                # try to use the connection for the preamble queries
                # below. We don't want a bug in health.py to break the
                # whole hook.
                health_notice = (f"[claude-dejavu] storage health "
                                    f"check errored: "
                                    f"{type(e).__name__}: {e}")
            cur = conn.cursor()
            # Latest summary for this project (skip the current session itself)
            cur.execute("""
                SELECT ss.request, ss.completed, ss.next_steps, ss.generated_at
                FROM session_summaries ss
                JOIN sessions s ON s.id = ss.session_id
                WHERE s.project_slug = %s AND s.id <> %s::uuid
                ORDER BY ss.generated_at DESC
                LIMIT 1
            """, (project_slug, payload.get("session_id") or "00000000-0000-0000-0000-000000000000"))
            r = cur.fetchone()
            if r:
                latest_summary = {"request": r[0], "completed": r[1], "next_steps": r[2]}

            cur.execute("""
                SELECT o.type::text, o.title
                FROM observations o
                JOIN sessions s ON s.id = o.session_id
                WHERE s.project_slug = %s
                ORDER BY o.ts DESC NULLS LAST
                LIMIT 3
            """, (project_slug,))
            for typ, title in cur.fetchall():
                observations.append({"type": typ, "title": title})

            # First-session-in-project bootstrap: kick off symbol indexing
            # in the background if it's never run for this project. The
            # PreToolUse lint hook treats unindexed projects as 'lint
            # disabled', so this auto-reindex is what makes the lint
            # actually work the first time — no manual `claude-dejavu
            # reindex` step required.
            if cfg.get("AUTO_REINDEX", "true").lower() not in ("0", "false", "no", "off"):
                reindex_notice = _kickoff_reindex_if_needed(project_slug, conn)
            # If past Claude Code sessions exist on disk but aren't in the
            # DB (e.g. just-installed plugin OR reinstalled after a long
            # gap), kick off a background bootstrap to import them.
            if cfg.get("AUTO_BOOTSTRAP", "true").lower() not in ("0", "false", "no", "off"):
                bootstrap_notice = _kickoff_bootstrap_if_needed(conn)
            else:
                bootstrap_notice = None
    except Exception:
        bootstrap_notice = None

    update_notice = _check_for_update(_local_plugin_version(), cfg)
    msg = _build_preamble(latest_summary, observations,
                          update_notice=update_notice,
                          reindex_notice=reindex_notice,
                          install_notice=install_notice,
                          bootstrap_notice=bootstrap_notice if 'bootstrap_notice' in dir() else None,
                          health_notice=health_notice)
    print(json.dumps({"systemMessage": msg}))


if __name__ == "__main__":
    main()
