#!/usr/bin/env python3
"""
claude-dejavu installer — cross-platform (Linux, macOS, Windows).

What it does:
  1. Resolves a per-OS data root (XDG / Library / LOCALAPPDATA).
  2. Probes for an existing Postgres + Weaviate, or asks the user to provide one.
  3. Creates per-Linux-user database `claude_dejavu_<user>` (or `_team` if --shared).
  4. Applies the schema and creates the Weaviate `ClaudeDejavuTurn` class.
  5. Sets up a Python virtualenv inside the data root and installs deps.
  6. Writes config.env with all connection details.
  7. Registers the MCP server in ~/.claude/settings.json so Claude can call
     dejavu_search / dejavu_restore / dejavu_resume mid-session.

What it does NOT do:
  - Does NOT install systemd / launchd / Task Scheduler timers.
    Snapshots and healthchecks are driven by the Stop hook with a 5-min throttle,
    which means they only fire while Claude Code is actually being used —
    avoiding the cross-distro init-system tarpit.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PLUGIN_ROOT / "code" / "schema.sql"
WV_CLASS_FILE = PLUGIN_ROOT / "code" / "weaviate-class.json"

OS_NAME = platform.system()  # 'Linux' | 'Darwin' | 'Windows'

# Windows defaults stdout to cp1252 which can't encode the
# ✓/⚠/✗/│/─/└ glyphs the installer prints. Reconfigure to UTF-8
# so the Setup hook on Windows doesn't crash with UnicodeEncodeError
# halfway through the install (we saw this on real installs — the
# user's terminal showed the install hanging because the python
# process had already died on a print).
if sys.platform.startswith("win") and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
OS_USER = os.environ.get("USER") or os.environ.get("USERNAME") or os.environ.get("LOGNAME") or "unknown"


def _default_data_root() -> Path:
    """Default data dir (ignoring CLAUDE_DEJAVU_DATA_ROOT). Used to
    decide whether the install is running in 'sandbox mode' so we don't
    clobber the user's production wrapper from a smoke test."""
    home = Path.home()
    if OS_NAME == "Darwin":
        return home / "Library" / "Application Support" / "claude-dejavu"
    if OS_NAME == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(home / "AppData" / "Local")
        return Path(base) / "claude-dejavu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) / "claude-dejavu" if xdg else home / ".local" / "share" / "claude-dejavu"


def data_root() -> Path:
    override = os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
    if override:
        return Path(override)
    return _default_data_root()


def ask(prompt: str, default: str = "", non_interactive: bool = False) -> str:
    if non_interactive:
        return default
    try:
        v = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        return default
    return v or default


def cyan(s):  print(f"\033[36m{s}\033[0m")
def green(s): print(f"\033[32m{s}\033[0m")
def yellow(s):print(f"\033[33m{s}\033[0m")
def red(s):   print(f"\033[31m{s}\033[0m")


# ─── Install status marker (read by SessionStart hook) ──────────────────────
#
# When --auto is in effect (Setup hook), we never sys.exit on a missing
# external dep — instead we write a JSON file describing what's incomplete
# and what the user should do. The SessionStart hook then injects a
# systemMessage that tells Claude (and via Claude, the user) what to fix.
# This is the only way to surface install failures to VS Code / IDE
# users who never see the install.py terminal output.

def _install_status_path() -> Path:
    return data_root() / "INSTALL_STATUS"


def _flag_install_incomplete(reason_code: str, summary: str, fix_instructions: str) -> None:
    p = _install_status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "status": "incomplete",
        "reason_code": reason_code,
        "summary": summary,
        "fix": fix_instructions,
        "ts": __import__("time").time(),
    }, indent=2))


def _clear_install_status() -> None:
    p = _install_status_path()
    if p.exists():
        try: p.unlink()
        except OSError: pass


def _fail(args, reason_code: str, summary: str,
            fix_instructions: str = "") -> None:
    """Hard-fail an installer step with full user-visible plumbing:

      - Print red error + yellow fix hint to stderr
      - In --auto mode, write INSTALL_STATUS so SessionStart can
        surface the failure to Claude in-chat
      - sys.exit(1)

    Centralizing this prevents the v0.5.0d-and-earlier bug where
    most auto-mode failures (PG timeout, schema apply, pip install,
    Weaviate timeout) just sys.exit'd silently with no marker file
    written — leaving the user with a "plugin enabled, nothing
    works, no idea why" state.
    """
    red(f"    {summary}")
    if fix_instructions:
        for line in fix_instructions.splitlines():
            yellow(f"    {line}")
    if getattr(args, "auto", False):
        _flag_install_incomplete(reason_code, summary, fix_instructions)
        yellow(f"    [auto] INSTALL_STATUS marker written. "
                f"SessionStart will surface this to Claude.")
    sys.exit(1)


# Bundled-stack flag so the schema applier can pick `docker exec` vs
# host psql. Set in step_postgres when we auto-provision via Docker;
# stays False when the user pointed install.py at their own PG.
_USE_BUNDLED_PG_PSQL = False


def _bundled_psql_available() -> bool:
    """Return True iff `docker exec claude-dejavu-postgres psql` works.

    Used as a Windows-friendly fallback when host psql isn't on PATH —
    the bundled Postgres container ships psql inside it. Eliminates
    the hard host-postgres-client dependency that previously blocked
    every Windows install."""
    if not shutil.which("docker"):
        return False
    r = subprocess.run(
        ["docker", "exec", "claude-dejavu-postgres",
          "psql", "--version"],
        capture_output=True, text=True, timeout=5,
    )
    return r.returncode == 0


def _run_psql(host: str, port: str, user: str, password: str,
                db: str | None, args_list: list[str], *,
                input_file: Path | None = None
                ) -> subprocess.CompletedProcess:
    """Run a psql command via host binary OR via `docker exec` against
    the bundled container, transparently. Hides the Windows-doesn't-
    have-psql issue from callers."""
    env = {**os.environ, "PGPASSWORD": password}
    host_psql = shutil.which("psql")
    if host_psql:
        cmd = [host_psql, "-h", host, "-p", str(port), "-U", user]
        if db:
            cmd += ["-d", db]
        if input_file:
            cmd += ["-f", str(input_file)]
        cmd += args_list
        return subprocess.run(cmd, env=env, capture_output=True,
                                  text=True)
    if _USE_BUNDLED_PG_PSQL:
        cmd = ["docker", "exec", "-i",
                 "-e", f"PGPASSWORD={password}",
                 "claude-dejavu-postgres",
                 "psql", "-h", "localhost", "-p", "5432",
                 "-U", user]
        if db:
            cmd += ["-d", db]
        cmd += args_list
        if input_file:
            return subprocess.run(
                cmd, capture_output=True, text=True,
                input=Path(input_file).read_text(encoding="utf-8"))
        return subprocess.run(cmd, capture_output=True, text=True)
    # Last-ditch: caller's args_list might still work with raw shell
    # if neither host psql nor docker is available — but practically
    # we can't do anything. Return a fake completed process so the
    # caller's error-path runs.
    cp = subprocess.CompletedProcess(args=args_list, returncode=127)
    cp.stdout = ""
    cp.stderr = ("psql not found on host AND bundled Postgres "
                   "container not running")
    return cp


def _docker_state() -> tuple[str, str]:
    """Probe Docker thoroughly. Returns (state, detail).

    state:
      'absent'          — `docker` binary not on PATH
      'daemon_down'     — binary present but `docker info` fails (daemon
                          not running — common on Windows/macOS where the
                          user installed Docker Desktop but didn't launch
                          it, or on Linux where the daemon socket isn't
                          accessible to the user)
      'ok'              — binary present AND daemon responsive
    """
    if not shutil.which("docker"):
        return "absent", "docker binary not in PATH"
    r = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True, timeout=8,
    )
    if r.returncode != 0:
        return "daemon_down", (r.stderr or r.stdout).strip()[:200]
    return "ok", (r.stdout or "").strip()


def _docker_daemon_start_hint() -> str:
    """Tell the user how to bring the daemon up, OS-specific."""
    if OS_NAME == "Darwin":
        return "Open Docker Desktop from /Applications and wait for the whale icon to settle. Then re-run."
    if OS_NAME == "Windows":
        return ("Launch Docker Desktop from the Start menu and wait for the whale icon "
                "in the taskbar to be steady (not animated). Then re-run.")
    return ("Start the daemon:  sudo systemctl start docker\n"
            "  Add yourself to the docker group (one-time):  sudo usermod -aG docker $USER && newgrp docker")


def _docker_install_instructions() -> str:
    """OS-specific one-shot install command, formatted for Claude to relay."""
    if OS_NAME == "Darwin":
        return (
            "macOS:\n"
            "  brew install --cask docker        # easiest\n"
            "  open -a Docker                    # or download from\n"
            "                                    # https://www.docker.com/products/docker-desktop/"
        )
    if OS_NAME == "Windows":
        return (
            "Windows:\n"
            "  winget install Docker.DockerDesktop    # via winget (ships with modern Win)\n"
            "    or: download from https://www.docker.com/products/docker-desktop/\n"
            "  After install: launch Docker Desktop and wait for the whale icon to be steady."
        )
    return (
        "Linux:\n"
        "  curl -fsSL https://get.docker.com | sudo sh\n"
        "  sudo usermod -aG docker $USER && newgrp docker"
    )


def _print_docker_install_instructions() -> None:
    yellow("\n  Docker is required for the bundled Postgres + Weaviate stack.")
    yellow("  Install it first, then re-run this installer:\n")
    for line in _docker_install_instructions().splitlines():
        yellow("    " + line)
    print()


# ─── Probe helpers ───────────────────────────────────────────────────────────

def probe_pg(host: str, port: int, timeout: float = 2.0,
             user: str = "postgres", password: str = "postgres") -> tuple[bool, str]:
    """Confirm a Postgres server is actually behind (host, port).

    Returns (ok, reason). `ok=True` only when we got a real Postgres handshake,
    NEVER on a bare TCP open — port 5432 could be anything (a leftover socat,
    a different RDBMS, a reverse-proxy). Lesson learned the hard way when
    SearXNG colonized port 8889 and a Weaviate-shaped check accepted it.

    Verification ladder:
      1. pg_isready on host PATH (best — speaks the Postgres
         startup protocol).
      2. psql -c 'SELECT 1' on host PATH against the default
         `postgres` db.
      3. `docker exec claude-dejavu-postgres pg_isready` — Windows
         users almost never have host pg_isready/psql, but if our
         bundled container is up they're inside it. Without this
         path the install on Windows would 60s-timeout despite the
         container being healthy.
      4. Refuse to declare success on TCP-only.
    """
    pg_isready = shutil.which("pg_isready")
    if pg_isready:
        r = subprocess.run(
            [pg_isready, "-h", host, "-p", str(port), "-t", str(int(timeout)),
             "-U", user, "-d", "postgres"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return True, "pg_isready"
        # pg_isready returned non-zero — could mean rejected, no listener, or wrong server.
        # Fall through to psql which gives clearer diagnostics.

    psql = shutil.which("psql")
    if psql:
        env = {**os.environ, "PGPASSWORD": password, "PGCONNECT_TIMEOUT": str(int(timeout))}
        r = subprocess.run(
            [psql, "-h", host, "-p", str(port), "-U", user, "-d", "postgres",
             "-tA", "-c", "SELECT 1"],
            env=env, capture_output=True, text=True,
        )
        if r.returncode == 0 and (r.stdout or "").strip() == "1":
            return True, "psql"
        return False, f"psql_failed: {(r.stderr or '').strip()[:120]}"

    # No host-side tools. If the bundled Postgres container is up,
    # pg_isready lives INSIDE it — usable via docker exec. This is
    # the Windows-without-postgres-client path that the v0.5.0d-and-
    # earlier installer would silently 60s-timeout on.
    if shutil.which("docker"):
        r = subprocess.run(
            ["docker", "exec", "claude-dejavu-postgres",
              "pg_isready", "-U", user, "-d", "postgres",
              "-t", str(int(timeout))],
            capture_output=True, text=True,
            timeout=max(int(timeout) + 2, 5),
        )
        if r.returncode == 0:
            return True, "docker_exec_pg_isready"
        # If docker exec succeeded but pg_isready returned non-zero,
        # the container exists but PG isn't ready yet.
        if "Error response from daemon" in (r.stderr or ""):
            # Container doesn't exist — distinct from "PG not ready"
            return False, "container_not_found"
        return False, ("docker_exec_pg_isready_not_ready: "
                          + (r.stdout or r.stderr).strip()[:120])

    # No way to confirm — refuse to claim success. TCP-only is misleading.
    return False, "no_pg_isready_or_psql_available_for_verification"


_WEAVIATE_REQUIRED_META_KEYS = {"version", "modules"}


def probe_weaviate(url: str, timeout: float = 3.0) -> dict | None:
    """Confirm the URL really speaks Weaviate's REST API.

    /v1/meta is part of the Weaviate contract. We require:
      1. HTTP 200 with content-type: application/json
      2. JSON body parses cleanly
      3. Required keys present: 'version', 'modules'
      4. 'version' looks like a Weaviate semver (digit.digit.digit-ish)

    Anything else is rejected even if it returns 200/JSON, because port-squatting
    services are a real failure mode (e.g. a SearXNG instance on 8889 returning
    a 404 HTML page would already fail us, but we want shape-validation against
    services that DO return 200/JSON to anything).
    """
    try:
        req = urllib.request.Request(f"{url}/v1/meta")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "json" not in ctype:
                return None
            body = r.read()
            data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not _WEAVIATE_REQUIRED_META_KEYS.issubset(data.keys()):
        return None
    ver = str(data.get("version", ""))
    if not ver or not ver[0].isdigit():
        return None
    return data


def _reclaim_stale_named_containers() -> dict[str, Any]:
    """Free up our canonical container names if they're held by a
    container belonging to a different (or unlabeled) Compose
    project.

    Why this is necessary: docker container names are global, not
    project-scoped. If a previous install ran with a different
    Compose project name (e.g. v0.5.0d-and-earlier derived the name
    from the parent dir `docker/` and ended up using that as
    the project), the resulting `claude-dejavu-postgres` /
    `claude-dejavu-weaviate` / `claude-dejavu-transformers`
    containers are still squatting on the names. Compose v2 will
    refuse to create our pinned-project containers because:

        Error: Conflict. The container name "claude-dejavu-postgres"
        is already in use by container "abc123…"

    We safely remove (NOT prune) such stale containers. Volumes —
    which is where the actual data lives — are NOT touched, so
    repeated runs preserve session data across the migration.

    Returns: {
        "checked": [container_name, ...],
        "reclaimed": [{"name", "from_project"}],
        "errors": [str, ...],
    }
    """
    from typing import Any  # noqa: F401  (already imported earlier)
    out: dict[str, Any] = {"checked": [], "reclaimed": [], "errors": []}
    if not shutil.which("docker"):
        return out

    canonical = ["claude-dejavu-postgres",
                   "claude-dejavu-weaviate",
                   "claude-dejavu-transformers"]
    for name in canonical:
        out["checked"].append(name)
        # Check if a container with this name exists at all.
        r = subprocess.run(
            ["docker", "inspect", "--format",
              "{{ index .Config.Labels \"com.docker.compose.project\" }}",
              name],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            # No container with that name — nothing to reclaim.
            continue
        existing_project = (r.stdout or "").strip()
        if existing_project == "claude-dejavu":
            # Already ours — Compose will reuse it cleanly.
            continue
        # Stale: belongs to another project (or unlabeled). Remove.
        cyan(f"    Reclaiming stale container `{name}` "
                f"(was in project `{existing_project or '<none>'}`). "
                f"Volume data preserved.")
        # Stop + remove. -f forces removal even if running.
        rm = subprocess.run(["docker", "rm", "-f", name],
                                capture_output=True, text=True,
                                timeout=20)
        if rm.returncode != 0:
            err = (rm.stderr or rm.stdout).strip()[:200]
            out["errors"].append(f"{name}: {err}")
            yellow(f"    ⚠ couldn't remove `{name}`: {err}")
            continue
        out["reclaimed"].append({"name": name,
                                      "from_project": existing_project})
    return out


# ─── Step 1: data root ───────────────────────────────────────────────────────

def step_data_root(args) -> Path:
    cyan("[1/6] Data root")
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "chat-logs").mkdir(exist_ok=True)
    (root / "snapshots").mkdir(exist_ok=True)
    green(f"  ✓ {root}")
    return root


# ─── Step 2: Postgres ────────────────────────────────────────────────────────

def step_postgres(args, data_dir: Path) -> dict:
    cyan("[2/6] Postgres")
    detected_port = None
    detected_method = None
    for p in (5432, 5450, 5433, 5434):
        ok, why = probe_pg("localhost", p)
        if ok:
            detected_port = p; detected_method = why; break

    if detected_port:
        green(f"  ✓ Detected Postgres on localhost:{detected_port}  (verified via {detected_method})")
        host = "localhost"; port = str(detected_port)
        user = ask("    PG user", "postgres", args.non_interactive)
        password = ask("    PG password", "postgres", args.non_interactive)
    else:
        yellow("  No Postgres detected on common ports (5432, 5450, 5433, 5434).")
        choice = ask(
            "    [a] auto-provision via Docker  [m] manual (point at your own)  [q] quit",
            "a", args.non_interactive
        ).strip().lower()
        if choice in ("q", "quit", "abort"):
            sys.exit(0)
        if choice in ("a", "auto", ""):
            if not shutil.which("docker"):
                _flag_install_incomplete(
                    "docker_missing",
                    "Docker is not installed. Postgres + Weaviate need either Docker "
                    "or your own running instances.",
                    _docker_install_instructions(),
                )
                if args.auto:
                    yellow("    [auto] Docker not found — install incomplete. Marker file written.")
                    yellow("    The SessionStart hook will tell Claude how to fix this in chat.")
                    sys.exit(0)
                red("    Docker not found in PATH.")
                _print_docker_install_instructions()
                yellow(f"    Then re-run: python scripts/install.py")
                sys.exit(1)
            # PER-SERVICE PROBE — only spin up services that are
            # genuinely missing. If the user already has a Weaviate
            # running on port 8080, we DO NOT replace it with our
            # bundled one (which would create a duplicate container
            # + wasted resources, and could be confusing).
            services_to_start = ["postgres"]  # always — we got here
                                                 # because PG was missing
            # Per-port probe so we can log the actual URL we found
            # (was previously logging the raw meta dict, which is
            # noisy and unhelpful). Also verify the existing Weaviate
            # has the `text2vec-transformers` module — without it
            # our class can't be created (it depends on that
            # vectorizer). If module missing, we DO spin up the
            # bundled stack so we have a Weaviate that can satisfy
            # our schema requirements.
            existing_wv_url = None
            existing_wv_meta = None
            for _try_url in ("http://localhost:8080",
                                "http://localhost:8889"):
                _meta = probe_weaviate(_try_url)
                if _meta:
                    existing_wv_url = _try_url
                    existing_wv_meta = _meta
                    break
            wv_modules = (existing_wv_meta or {}).get("modules", {}) \
                            if isinstance(existing_wv_meta, dict) else {}
            has_t2v = "text2vec-transformers" in (wv_modules or {})
            if existing_wv_url and has_t2v:
                green(f"    ✓ Detected existing Weaviate at "
                        f"{existing_wv_url} (v"
                        f"{existing_wv_meta.get('version', '?')}) "
                        f"with text2vec-transformers — will reuse.")
            elif existing_wv_url and not has_t2v:
                yellow(f"    ⚠ Found Weaviate at {existing_wv_url} "
                          f"but it lacks the text2vec-transformers "
                          f"module — bundled stack will be brought up "
                          f"separately so our class can be created.")
                services_to_start += ["weaviate", "transformers"]
            else:
                services_to_start += ["weaviate", "transformers"]
                yellow("    No Weaviate detected — will bring up the "
                          "bundled stack (weaviate + transformers).")

            # Reclaim any stale containers from prior installs
            # whose project name didn't match our pinned
            # `claude-dejavu`. Without this, Compose fails with
            # "container name X is already in use" on every retry.
            reclaim_report = _reclaim_stale_named_containers()
            if reclaim_report["reclaimed"]:
                names = ", ".join(r["name"] for r
                                       in reclaim_report["reclaimed"])
                green(f"    ✓ Reclaimed stale containers: {names} "
                        f"(volumes preserved)")
            if reclaim_report["errors"]:
                _fail(args, "stale_container_reclaim_failed",
                        "Could not reclaim stale container name(s) "
                        "from a previous install",
                        "\n".join([
                            "Errors:",
                            *("  " + e for e in reclaim_report["errors"]),
                            "",
                            "Manual fix:",
                            "  docker rm -f claude-dejavu-postgres "
                              "claude-dejavu-weaviate "
                              "claude-dejavu-transformers",
                            "Then re-run: python scripts/install.py "
                              "--auto",
                            "Volumes are NOT touched by `docker rm` — "
                            "your session data is safe.",
                        ]))

            cyan(f"    Spinning up: {', '.join(services_to_start)} "
                    f"via docker-compose…")
            cyan("    First-time pulls can take 2-5 min on slow "
                    "connections.")
            cyan("    Streaming docker output below — if it stops "
                    "scrolling for several minutes, check your network "
                    "or `docker pull` rate limits.")
            print()  # blank line so docker's output stands out
            compose_file = PLUGIN_ROOT / "docker" / "docker-compose.minimal.yml"
            # Stream stdout/stderr live so the user sees image-pull progress,
            # not a silent multi-minute hang. Docker's `--progress plain`
            # avoids ANSI cursor jumping that doesn't render well on
            # Windows / non-tty terminals; the indent prefix makes the
            # docker lines visually distinct from installer output.
            try:
                proc = subprocess.Popen(
                    # `-p claude-dejavu` pins the Compose project name
                    # explicitly. Without it Compose derives the project
                    # name from the parent directory (`docker/`) — which
                    # collides with any other stack also using `docker/`,
                    # and Compose would remove the other stack's
                    # containers as orphans. Defense in depth: the
                    # compose file itself also declares `name:
                    # claude-dejavu`.
                    ["docker", "compose", "-p", "claude-dejavu",
                      "-f", str(compose_file),
                      "--progress", "plain", "up", "-d",
                      *services_to_start],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except FileNotFoundError:
                _fail(args, "docker_missing_runtime",
                        "docker binary disappeared between probe and "
                        "compose call",
                        _docker_install_instructions())
            assert proc.stdout is not None
            for line in proc.stdout:
                # Strip trailing newline; print with indent prefix.
                sys.stdout.write(f"      │ {line.rstrip()}\n")
                sys.stdout.flush()
            rc = proc.wait()
            print()  # blank line after docker output
            if rc != 0:
                _fail(args, "docker_compose_up_failed",
                        f"docker compose up failed (exit {rc})",
                        f"Try manually: docker compose -p claude-dejavu "
                        f"-f {compose_file} up -d\n"
                        f"Then re-run: python scripts/install.py --auto")
            # Wait for PG to become ready (up to 60s — first start with
            # a fresh volume can be slow on low-resource VMs / Windows).
            green("    Waiting for Postgres to accept connections "
                    "(up to 60s)…")
            ready = False
            last_reason = ""
            import time as _t
            for i in range(60):
                ok, why = probe_pg("localhost", 5454)
                if ok:
                    ready = True; break
                last_reason = why
                # Heartbeat every second so the user knows we're still
                # alive. Carriage-return-based update on TTYs; plain on
                # non-TTYs (CI / Windows cmd may not handle \r cleanly).
                if sys.stdout.isatty():
                    sys.stdout.write(f"\r      … waiting {i+1}/60s "
                                          f"({last_reason[:40]})   ")
                    sys.stdout.flush()
                else:
                    if i % 5 == 0:  # every 5s on non-TTY
                        print(f"      … still waiting ({i+1}/60s): "
                                f"{last_reason[:60]}")
                _t.sleep(1)
            if sys.stdout.isatty():
                sys.stdout.write("\r" + " " * 70 + "\r")
            if not ready:
                _fail(args, "postgres_timeout",
                        f"Postgres didn't come up within 60s "
                        f"({last_reason})",
                        "Check `docker logs claude-dejavu-postgres` "
                        "for the actual startup error, then re-run "
                        "scripts/install.py --auto")
            host, port, user, password = "localhost", "5454", "postgres", "postgres"
            global _USE_BUNDLED_PG_PSQL
            _USE_BUNDLED_PG_PSQL = True
            green(f"    ✓ Bundled Postgres up on {host}:{port}")
        else:
            host = ask("    PG host", "localhost", args.non_interactive)
            port = ask("    PG port", "5432", args.non_interactive)
            user = ask("    PG user", "postgres", args.non_interactive)
            password = ask("    PG password", "postgres", args.non_interactive)

    db = f"claude_dejavu_team" if args.shared else f"claude_dejavu_{OS_USER}"
    db = ask("    PG database name", db, args.non_interactive)

    # Create DB + apply schema. Prefer host psql; fall back to
    # `docker exec` against the bundled container — this removes the
    # Windows hard-dependency on installing postgres-client manually.
    host_psql = shutil.which("psql")
    if not host_psql and not _USE_BUNDLED_PG_PSQL:
        fix_lines = []
        if OS_NAME == "Linux":
            fix_lines = [
                "Install postgres-client:",
                "  sudo apt install postgresql-client      (Debian/Ubuntu)",
                "  sudo dnf install postgresql              (Fedora/RHEL)",
                "  sudo pacman -S postgresql-libs           (Arch)",
            ]
        elif OS_NAME == "Darwin":
            fix_lines = ["Install: brew install libpq && "
                          "brew link --force libpq"]
        elif OS_NAME == "Windows":
            fix_lines = [
                "Install postgres-client (psql.exe):",
                "  winget install PostgreSQL.PostgreSQL    (recommended)",
                "  scoop install postgresql                 (alternative)",
            ]
        fix_lines.append("Then re-run: python scripts/install.py --auto")
        _fail(args, "psql_missing",
                "psql not found in PATH and bundled Postgres "
                "container is not in use.",
                "\n".join(fix_lines))
    if not host_psql and _USE_BUNDLED_PG_PSQL:
        cyan(f"    Using `docker exec claude-dejavu-postgres psql` "
                f"(host psql not on PATH — Windows-friendly path).")

    # Create DB if missing (idempotent). The CREATE DATABASE may emit
    # "already exists" — that's fine, we ignore the return code on
    # the create call.
    _run_psql(host, port, user, password, None, [
        "-tc", f"SELECT 1 FROM pg_database WHERE datname='{db}'"
    ])
    _run_psql(host, port, user, password, None, [
        "-c", f"CREATE DATABASE {db}"
    ])
    r = _run_psql(host, port, user, password, db, [],
                      input_file=SCHEMA_FILE)
    if r.returncode != 0:
        _fail(args, "schema_apply_failed",
                f"schema apply failed",
                f"psql stderr: {(r.stderr or '')[:300]}\n"
                f"Re-run: python scripts/install.py --auto\n"
                f"If persistent, manually: psql -h {host} -p {port} "
                f"-U {user} -d {db} -f code/schema.sql")

    # Apply any column-level / type-change / rename migrations on top
    # of the base schema. New tables are handled by schema.sql itself
    # (CREATE TABLE IF NOT EXISTS), so this only runs for changes
    # that can't be expressed idempotently in schema.sql.
    _apply_schema_migrations(args, host, port, user, password, db)

    green(f"  ✓ Database {db} ready on {host}:{port}")
    return {"PG_HOST": host, "PG_PORT": port, "PG_USER": user, "PG_PASS": password, "PG_DB": db}


def _apply_schema_migrations(args, host: str, port: str, user: str,
                                password: str, db: str) -> None:
    """Apply forward-only migrations from code/migrations/.

    Idempotent. Tracks applied migrations in the
    `_dejavu_schema_versions` table, which we create on first run.
    Each migration file is `NNNN_description.sql`. See
    code/migrations/README.md for the full design + naming rules.

    Failures use the standard _fail() path so SessionStart can
    surface them in --auto mode.
    """
    migrations_dir = PLUGIN_ROOT / "code" / "migrations"
    if not migrations_dir.is_dir():
        return  # No migrations directory — nothing to apply

    # Discover .sql files (filename-sorted)
    candidates = sorted(p for p in migrations_dir.glob("*.sql")
                              if p.is_file())
    if not candidates:
        return  # Empty migrations dir — nothing to apply

    # Bootstrap the version table.
    r = _run_psql(host, port, user, password, db, [
        "-c",
        "CREATE TABLE IF NOT EXISTS _dejavu_schema_versions ("
        "  name TEXT PRIMARY KEY, "
        "  applied_at TIMESTAMPTZ DEFAULT now()"
        ")",
    ])
    if r.returncode != 0:
        _fail(args, "schema_versions_table_failed",
                "Could not create _dejavu_schema_versions table",
                f"psql stderr: {(r.stderr or '')[:300]}")

    # Read already-applied set.
    r = _run_psql(host, port, user, password, db, [
        "-tA", "-c", "SELECT name FROM _dejavu_schema_versions",
    ])
    if r.returncode != 0:
        _fail(args, "schema_versions_query_failed",
                "Could not read _dejavu_schema_versions",
                f"psql stderr: {(r.stderr or '')[:300]}")
    applied = {ln.strip() for ln in (r.stdout or "").splitlines()
                  if ln.strip()}

    pending = [p for p in candidates if p.name not in applied]
    if not pending:
        return  # Everything's applied

    cyan(f"    Applying {len(pending)} schema migration(s)…")
    for mig in pending:
        cyan(f"      → {mig.name}")
        r = _run_psql(host, port, user, password, db, [],
                          input_file=mig)
        if r.returncode != 0:
            _fail(args, "migration_apply_failed",
                    f"Migration {mig.name} failed",
                    f"psql stderr: {(r.stderr or '')[:300]}\n"
                    f"Migrations are forward-only — fix the .sql or "
                    f"manually clean up partial state, then re-run "
                    f"install.py --auto.")
        # Record in version table only after successful apply.
        # ON CONFLICT no-op handles the case where the migration
        # itself self-recorded (some migrations may want to be
        # transactional with their own version row insert).
        _run_psql(host, port, user, password, db, [
            "-c",
            f"INSERT INTO _dejavu_schema_versions (name) "
            f"VALUES ('{mig.name}') "
            f"ON CONFLICT (name) DO NOTHING"
        ])
    green(f"    ✓ {len(pending)} migration(s) applied")


# ─── Step 3: Weaviate ────────────────────────────────────────────────────────

def step_weaviate(args, data_dir: Path) -> dict:
    cyan("[3/6] Weaviate")
    detected = None
    for url in ("http://localhost:8889", "http://localhost:8888", "http://localhost:8080", "http://localhost:8081"):
        meta = probe_weaviate(url)
        if meta:
            mods = list(meta.get("modules", {}).keys())
            ver = meta.get("version", "?")
            green(f"  ✓ Detected Weaviate v{ver} at {url}  modules={mods}")
            detected = url
            break

    if not detected:
        yellow("  No Weaviate detected on common ports (8889, 8888, 8080, 8081).")
        choice = ask(
            "    [a] auto-provision via Docker (already running if you used PG auto)  [m] manual  [q] quit",
            "a", args.non_interactive
        ).strip().lower()
        if choice in ("q", "quit", "abort"):
            sys.exit(0)
        if choice in ("a", "auto", ""):
            if not shutil.which("docker"):
                _fail(args, "docker_missing",
                        "Docker not found in PATH",
                        _docker_install_instructions())
            # Same stale-container reclaim as in step_postgres.
            # We're only ever bringing up weaviate + transformers
            # here (postgres path took a different branch above),
            # but the function is cheap to call and idempotent.
            reclaim_report = _reclaim_stale_named_containers()
            if reclaim_report["reclaimed"]:
                names = ", ".join(r["name"] for r
                                       in reclaim_report["reclaimed"])
                green(f"    ✓ Reclaimed stale containers: {names} "
                        f"(volumes preserved)")
            if reclaim_report["errors"]:
                _fail(args, "stale_container_reclaim_failed",
                        "Could not reclaim stale container name(s)",
                        "Manual fix:\n  docker rm -f "
                        "claude-dejavu-weaviate "
                        "claude-dejavu-transformers\n"
                        "Then re-run install.py --auto. Volumes are "
                        "NOT touched by docker rm — data is safe.")

            # docker-compose.minimal.yml brings up weaviate + transformers along with postgres
            compose_file = PLUGIN_ROOT / "docker" / "docker-compose.minimal.yml"
            cyan("    Bringing up Weaviate + transformers via "
                    "docker-compose…")
            cyan("    First-time pulls download the embedding model "
                    "(~110 MB) — can take 2-5 min on slow links.")
            cyan("    Streaming docker output below — if it stops "
                    "scrolling for several minutes, check your network.")
            print()
            try:
                wv_proc = subprocess.Popen(
                    ["docker", "compose", "-p", "claude-dejavu",
                      "-f", str(compose_file),
                      "--progress", "plain", "up", "-d",
                      "weaviate", "transformers"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except FileNotFoundError:
                _fail(args, "docker_missing_runtime",
                        "docker binary disappeared between probe and "
                        "compose call",
                        _docker_install_instructions())
            assert wv_proc.stdout is not None
            for line in wv_proc.stdout:
                sys.stdout.write(f"      │ {line.rstrip()}\n")
                sys.stdout.flush()
            rc = wv_proc.wait()
            print()
            if rc != 0:
                _fail(args, "docker_compose_up_failed_weaviate",
                        f"docker compose up weaviate failed (exit {rc})",
                        f"Try manually: docker compose -p claude-dejavu "
                        f"-f {compose_file} up -d weaviate transformers\n"
                        f"Then re-run: python scripts/install.py --auto")
            green("    Waiting for Weaviate to be ready (up to 60s — first start pulls model)…")
            import time as _t
            for i in range(60):
                meta = probe_weaviate("http://localhost:8889")
                if meta:
                    detected = "http://localhost:8889"
                    break
                if sys.stdout.isatty():
                    sys.stdout.write(f"\r      … waiting {i+1}/60s   ")
                    sys.stdout.flush()
                elif i % 5 == 0:
                    print(f"      … still waiting ({i+1}/60s)")
                _t.sleep(1)
            if sys.stdout.isatty():
                sys.stdout.write("\r" + " " * 70 + "\r")
            if not detected:
                _fail(args, "weaviate_timeout",
                        "Weaviate didn't come up within 60s",
                        "Check `docker logs claude-dejavu-weaviate` "
                        "for the actual startup error. First start may "
                        "be slow because the transformers-inference "
                        "container downloads the embedding model "
                        "(~110 MB). Re-run: python scripts/install.py "
                        "--auto")
            green(f"    ✓ Bundled Weaviate up on {detected}")

    wv_url = detected or ask("    Weaviate URL", "http://localhost:8080", args.non_interactive)

    # Create class (idempotent)
    cls_def = json.loads(WV_CLASS_FILE.read_text())
    try:
        urllib.request.urlopen(f"{wv_url}/v1/schema/{cls_def['class']}", timeout=5)
        green(f"  ✓ Class {cls_def['class']} already exists")
    except Exception:
        req = urllib.request.Request(f"{wv_url}/v1/schema",
                                     data=json.dumps(cls_def).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            green(f"  ✓ Class {cls_def['class']} created")
        except Exception as e:
            yellow(f"  ⚠ Could not create class (will retry on first ingest): {e}")

    tenant = "team" if args.shared else OS_USER
    return {"WV_URL": wv_url, "WV_TENANT": tenant}


# ─── Step 4: venv ────────────────────────────────────────────────────────────

def step_venv(args, data_dir: Path) -> Path:
    cyan("[4/6] Python virtualenv")
    venv_dir = data_dir / "venv"
    if not venv_dir.exists():
        cyan(f"    Creating venv at {venv_dir} (one-time, ~5s)…")
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)],
                          check=True)
    else:
        cyan(f"    Reusing existing venv at {venv_dir}")
    bin_dir = venv_dir / ("Scripts" if OS_NAME == "Windows" else "bin")
    py = bin_dir / ("python.exe" if OS_NAME == "Windows" else "python3")
    pip = bin_dir / ("pip.exe" if OS_NAME == "Windows" else "pip")

    # Two pip install rounds (pip itself + the dejavu deps). Both can
    # take 30-90s on slow connections — surface progress instead of
    # silent waits. We don't stream pip output by default (it's
    # noisy); a periodic heartbeat is enough for the user to know
    # the install is alive.
    cyan("    Upgrading pip…")
    subprocess.run([str(pip), "install", "--quiet", "--upgrade", "pip"],
                      capture_output=True)
    cyan("    Installing dependencies "
            "(psycopg2-binary, mcp, tree-sitter, tree-sitter-languages, "
            "pyyaml). First-time install can take 30-90s on slow "
            "connections — please wait…")
    pip_proc = subprocess.Popen(
        [str(pip), "install",
          "psycopg2-binary", "mcp",
          # tree-sitter for accurate code symbol grounding (v0.3.0+).
          # Pinned to known-good wheel-shipping versions so installs
          # are offline-capable on Linux/macOS/Windows. If wheels are
          # unavailable on a given platform, the indexer transparently
          # falls back to the regex/ast parsers — install still
          # succeeds.
          "tree-sitter==0.21.3", "tree-sitter-languages==1.10.2",
          # PyYAML for parsing OpenAPI/Swagger YAML files in the
          # route indexer (v0.3.2.1+). Falls back to no-op if absent.
          "pyyaml>=6.0"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        bufsize=1)
    assert pip_proc.stdout is not None
    # Stream pip's per-line output — keeps the user informed during
    # what was previously a silent multi-minute wait. We indent so
    # pip lines are visually distinct from the installer's own.
    for line in pip_proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        # Skip the very-noisy "Downloading X-Y.Z (...)" lines but keep
        # "Collecting", "Installing", and any error/warning output.
        if line.startswith(("Downloading ", "  Downloading ")):
            # Show downloads as a single dot so the screen doesn't
            # flood with progress bars.
            sys.stdout.write(".")
            sys.stdout.flush()
            continue
        sys.stdout.write(f"\n      │ {line}")
        sys.stdout.flush()
    print()  # final newline after the dotted progress line
    rc = pip_proc.wait()
    if rc != 0:
        _fail(args, "pip_install_failed",
                f"pip install failed (exit {rc})",
                f"Check pip output above for the actual error. "
                f"Common causes: no network, missing C compiler for "
                f"a wheel that fell back to source build, disk full. "
                f"Re-run: python scripts/install.py --auto")
    green(f"  ✓ venv at {venv_dir}")
    return venv_dir


# ─── Step 5: config + CLI symlink ────────────────────────────────────────────

def step_config(args, data_dir: Path, pg: dict, wv: dict, venv: Path):
    cyan("[5/6] Config + CLI")
    config = data_dir / "config.env"
    lines = ["# claude-dejavu config — auto-generated by install.py"]
    for k, v in {**pg, **wv}.items():
        lines.append(f"{k}={v}")
    lines.append(f"DATA_ROOT={data_dir}")
    lines.append(f"OS_USER={OS_USER}")
    lines.append(f"SHARED={int(bool(args.shared))}")
    lines.append("CLAUDE_DEJAVU_DEFAULT_SCOPE=workspace")
    # Cloud embed tier (v0.6.0+). Defaults to local — no behavior change.
    # Users opt in via `claude-dejavu login` (writes mode=cloud + key).
    lines.append("DEJAVU_EMBED_MODE=local")
    lines.append("DEJAVU_CLOUD_URL=https://embed.hte.digital")
    config.write_text("\n".join(lines) + "\n")
    green(f"  ✓ {config}")

    # CLI shim — works on all OSes.
    # IMPORTANT: when running with a non-default DATA_ROOT (sandbox / smoke
    # test), DO NOT clobber the user's real wrapper at ~/.local/bin/claude-dejavu.
    # That wrapper points at the production install; smoke runs with a /tmp
    # sandbox would overwrite it and leave a broken pointer when the
    # sandbox is cleaned up. Honor:
    #   1. CLAUDE_DEJAVU_BIN_DIR env var — explicit override
    #   2. Auto-detect: if CLAUDE_DEJAVU_DATA_ROOT is set AND points
    #      somewhere other than the default user data dir, write the
    #      wrapper inside DATA_ROOT/bin/ instead of ~/.local/bin/.
    bin_override = os.environ.get("CLAUDE_DEJAVU_BIN_DIR")
    if bin_override:
        bin_dir = Path(bin_override)
    elif (os.environ.get("CLAUDE_DEJAVU_DATA_ROOT")
            and Path(os.environ["CLAUDE_DEJAVU_DATA_ROOT"]).resolve()
            != _default_data_root().resolve()):
        bin_dir = data_root() / "bin"
    elif OS_NAME == "Windows":
        # Place inside our own data dir, NOT in the system-reserved
        # Microsoft\WindowsApps. The latter is read-only for regular
        # users on locked-down Windows installs (managed devices /
        # corp policies / antivirus quarantine), so writing the .bat
        # silently fails and the user has no `claude-dejavu` on PATH
        # despite the install reporting "complete". Per-user data dir
        # is always writable.
        bin_dir = data_root() / "bin"
    else:
        bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    if OS_NAME == "Windows":
        cli_path = bin_dir / "claude-dejavu.bat"
        # Windows wrapper: prefer venv python, fall back to system
        # python for read-only `doctor` so the user can see what's
        # broken even when the venv itself is missing.
        cli_path.write_text(
            f'@echo off\r\n'
            f'set "VENV_PY={venv / "Scripts" / "python.exe"}"\r\n'
            f'if exist "%VENV_PY%" (\r\n'
            f'  "%VENV_PY%" "{PLUGIN_ROOT / "code" / "recall.py"}" %*\r\n'
            f'  exit /b %errorlevel%\r\n'
            f')\r\n'
            f'if /i "%~1"=="doctor" (\r\n'
            f'  for %%P in (python py python3) do (\r\n'
            f'    where %%P >nul 2>nul && (\r\n'
            f'      echo claude-dejavu: venv missing - running doctor via %%P 1^>^&2\r\n'
            f'      %%P "{PLUGIN_ROOT / "code" / "doctor.py"}" %2 %3 %4 %5 %6 %7 %8 %9\r\n'
            f'      exit /b %errorlevel%\r\n'
            f'    )\r\n'
            f'  )\r\n'
            f')\r\n'
            f'echo claude-dejavu: venv python missing at %VENV_PY% 1^>^&2\r\n'
            f'echo   Re-run: python scripts\\install.py --auto 1^>^&2\r\n'
            f'exit /b 1\r\n'
        )
    else:
        cli_path = bin_dir / "claude-dejavu"
        # POSIX wrapper: prefer python3 venv, fall back to python
        # venv, then for `doctor` fall back to system python so the
        # user can see what's broken even with no venv.
        cli_path.write_text(
            f'#!/bin/bash\n'
            f'if [ -x "{venv / "bin" / "python3"}" ]; then\n'
            f'  exec "{venv / "bin" / "python3"}" '
            f'"{PLUGIN_ROOT / "code" / "recall.py"}" "$@"\n'
            f'elif [ -x "{venv / "bin" / "python"}" ]; then\n'
            f'  exec "{venv / "bin" / "python"}" '
            f'"{PLUGIN_ROOT / "code" / "recall.py"}" "$@"\n'
            f'fi\n'
            f'# venv missing — let `doctor` run via system python so the '
            f'user gets actionable diagnostic output instead of a hard exit.\n'
            f'if [ "${{1:-}}" = "doctor" ]; then\n'
            f'  for sys_py in python3 python py; do\n'
            f'    if command -v "$sys_py" >/dev/null 2>&1; then\n'
            f'      echo "claude-dejavu: venv missing — running '
            f'doctor via $sys_py" >&2\n'
            f'      exec "$sys_py" '
            f'"{PLUGIN_ROOT / "code" / "doctor.py"}" "${{@:2}}"\n'
            f'    fi\n'
            f'  done\n'
            f'fi\n'
            f'echo "claude-dejavu: venv python missing — '
            f're-run install.py" >&2\n'
            f'exit 1\n'
        )
        cli_path.chmod(0o755)
    green(f"  ✓ CLI: {cli_path}")
    if str(cli_path.parent) not in os.environ.get("PATH", ""):
        yellow(f"    add {cli_path.parent} to PATH to use 'claude-dejavu' globally")


# ─── Step 6: MCP registration ────────────────────────────────────────────────

def _plugin_already_installed() -> bool:
    """Detect a working Claude Code marketplace install of claude-dejavu.

    The plugin's own .mcp.json is what wires the MCP server when installed
    via `/plugin install claude-dejavu`. If that's present (and active),
    writing a duplicate to ~/.claude/settings.json gives a 'Failed to
    connect' line in `claude mcp list` because the user-settings entry
    points at the dev-source path, not the marketplace cache.

    We detect by looking for any plugin manifest under the standard
    plugins cache root that names claude-dejavu.
    """
    candidates = [
        Path.home() / ".claude" / "plugins" / "cache" / "claude-dejavu",
        Path.home() / ".claude" / "plugins" / "marketplaces" / "claude-dejavu",
    ]
    for c in candidates:
        if (c / ".claude-plugin" / "plugin.json").exists():
            return True
        if c.is_dir():
            for sub in c.iterdir():
                if (sub / ".claude-plugin" / "plugin.json").exists():
                    return True
                if sub.is_dir():
                    for sub2 in sub.iterdir():
                        if (sub2 / ".claude-plugin" / "plugin.json").exists():
                            return True
    return False


def step_mcp(args, venv: Path):
    cyan("[6/6] MCP server registration")
    settings = Path.home() / ".claude" / "settings.json"

    # If the plugin is installed via marketplace, the plugin's own .mcp.json
    # wires MCP correctly. Writing a duplicate here causes the second
    # 'claude-dejavu' entry in `claude mcp list` to fail. Skip unless the
    # user explicitly asks for the manual registration.
    plugin_installed = _plugin_already_installed()
    if plugin_installed and not args.register_mcp:
        green("  ✓ Plugin install detected — MCP already wired via plugin.")
        yellow("    (use --register-mcp to also write a manual entry to settings.json)")
        # Best-effort: clean a stale entry if one is left over from a prior
        # standalone install.
        if settings.exists():
            try:
                cfg = json.loads(settings.read_text())
                mcp = cfg.get("mcpServers", {})
                if "claude-dejavu" in mcp:
                    cmd = mcp["claude-dejavu"].get("command", "")
                    # Stale if it points at a no-longer-existing venv python.
                    if cmd and not Path(cmd).exists():
                        bak = settings.with_suffix(settings.suffix + ".bak")
                        bak.write_text(settings.read_text())
                        del mcp["claude-dejavu"]
                        settings.write_text(json.dumps(cfg, indent=2))
                        yellow(f"    Removed stale claude-dejavu MCP entry from {settings}")
                        yellow(f"    (backup at {bak})")
            except Exception:
                pass
        return

    if not settings.exists():
        yellow(f"  Skipping: {settings} does not exist")
        return
    try:
        cfg = json.loads(settings.read_text())
    except Exception as e:
        red(f"  Could not parse {settings}: {e}"); return

    # Backup
    bak = settings.with_suffix(settings.suffix + ".bak")
    bak.write_text(settings.read_text())

    py = str(venv / ("Scripts/python.exe" if OS_NAME == "Windows" else "bin/python3"))
    server = str(PLUGIN_ROOT / "mcp" / "server.py")
    cfg.setdefault("mcpServers", {})["claude-dejavu"] = {
        "command": py,
        "args": [server],
        "env": {"CLAUDE_DEJAVU_DATA_ROOT": str(data_root())},
    }
    settings.write_text(json.dumps(cfg, indent=2))
    green(f"  ✓ Registered manually (backup at {bak})")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="claude-dejavu installer (cross-platform)")
    ap.add_argument("--non-interactive", action="store_true")
    ap.add_argument("--shared", action="store_true",
                    help="single shared DB + tenant for all users on this machine (trusted teams only)")
    ap.add_argument("--register-mcp", action="store_true",
                    help="force-write a claude-dejavu entry to ~/.claude/settings.json "
                         "(only needed for non-plugin manual installs; the marketplace "
                         "plugin already wires MCP via its own .mcp.json)")
    ap.add_argument("--auto", action="store_true",
                    help="fully automatic mode for the Setup hook on plugin install. "
                         "Implies --non-interactive. Auto-provisions Docker stack if "
                         "Docker is found, but does NOT exit with a hard error if not. "
                         "Writes ~/.local/share/claude-dejavu/INSTALL_STATUS describing "
                         "any incomplete state, and the SessionStart hook surfaces it "
                         "to Claude so the user gets actionable instructions in-chat.")
    args = ap.parse_args()
    if args.auto:
        args.non_interactive = True

    cyan("===================================================")
    cyan(f"  claude-dejavu installer  (OS={OS_NAME}, user={OS_USER})")
    if args.auto:
        cyan(f"  Mode: --auto (Setup hook, non-interactive)")
    cyan("===================================================")
    print()

    # ─── Loud prereq check: Docker (binary + daemon) ────────────────────
    docker_state, docker_detail = _docker_state()
    if docker_state == "absent":
        yellow("┌────────────────────────────────────────────────────────────────────┐")
        yellow("│  PREREQUISITE: Docker — NOT INSTALLED                              │")
        yellow("│                                                                    │")
        yellow("│  claude-dejavu auto-provisions Postgres + Weaviate via Docker.     │")
        yellow("│  Without Docker the installer can still finish if you provide      │")
        yellow("│  your own running PG/Weaviate (PG_HOST/PG_PORT in config.env).     │")
        yellow("└────────────────────────────────────────────────────────────────────┘")
        for line in _docker_install_instructions().splitlines():
            yellow("    " + line)
        print()
    elif docker_state == "daemon_down":
        yellow("┌────────────────────────────────────────────────────────────────────┐")
        yellow("│  PREREQUISITE: Docker — INSTALLED BUT NOT RUNNING                  │")
        yellow("│                                                                    │")
        yellow("│  Found docker binary, but the daemon isn't responding.             │")
        yellow("└────────────────────────────────────────────────────────────────────┘")
        for line in _docker_daemon_start_hint().splitlines():
            yellow("    " + line)
        if docker_detail:
            yellow(f"    (daemon error: {docker_detail[:120]})")
        print()
        if args.auto:
            _flag_install_incomplete(
                "docker_daemon_down",
                "Docker is installed but the daemon isn't running.",
                _docker_daemon_start_hint(),
            )
            yellow("    [auto] Marker file written. SessionStart will surface this to Claude.")
            sys.exit(0)
    else:
        green(f"  ✓ Docker daemon responsive (server version {docker_detail})")
        print()

    print("This installer wires up a CROSS-PLATFORM Claude Code memory + recall layer.")
    print("No systemd / launchd / Task Scheduler — snapshots are Stop-hook-driven.")
    print()
    print("By default each Linux/Mac/Windows user gets isolated storage:")
    print(f"  - Postgres database: claude_dejavu_{OS_USER}")
    print(f"  - Weaviate tenant:   {OS_USER}")
    if args.shared:
        yellow("  --shared: single shared DB + tenant 'team' for ALL users on this machine.")
    print()

    # If we're in --auto mode and Docker is absent, mark incomplete and bail
    # before attempting steps that need DB. The SessionStart hook will pick
    # up the marker and tell Claude (and the user) what to install.
    if args.auto and docker_state == "absent":
        _flag_install_incomplete(
            "docker_missing",
            "Docker is not installed. claude-dejavu needs Docker to auto-provision "
            "Postgres + Weaviate (or you can point it at your own running instances "
            "via PG_HOST / PG_PORT in config.env).",
            _docker_install_instructions(),
        )
        yellow("[auto] Docker not found — INSTALL_STATUS marker written. "
               "Open a new Claude Code session after installing Docker and the "
               "installer will resume.")
        sys.exit(0)

    # Progress markers — SessionStart can surface "in-progress" state so the
    # user knows the install is still running on a slow connection.
    def _progress(stage: str, detail: str = "") -> None:
        try:
            (data_root() / "INSTALL_STATUS").parent.mkdir(parents=True, exist_ok=True)
            (data_root() / "INSTALL_STATUS").write_text(json.dumps({
                "status": "in_progress",
                "stage": stage,
                "detail": detail,
                "ts": __import__("time").time(),
            }, indent=2))
        except OSError:
            pass

    _progress("data_root_setup")
    root = step_data_root(args)

    _progress("postgres_setup", "auto-provisioning if Docker is up; this takes ~60s on first run")
    pg = step_postgres(args, root)

    _progress("weaviate_setup", "transformers model download is the slow part on first run (~110 MB)")
    wv = step_weaviate(args, root)

    _progress("venv_setup", "pip-installing psycopg2-binary, mcp, tree-sitter, tree-sitter-languages")
    venv = step_venv(args, root)

    _progress("config_write")
    step_config(args, root, pg, wv, venv)

    _progress("mcp_register")
    step_mcp(args, venv)

    # Rewrite the plugin's .mcp.json with the OS-correct python command.
    # On Windows the python executable is `python` (or `py.exe`); on
    # Linux/macOS prefer `python3` (which is the canonical name).
    _progress("mcp_json_normalize")
    _normalize_mcp_json()

    # ─── Auto-bootstrap: import past Claude Code sessions ────────────────
    # Closes the "reinstall after 4 months gives me back nothing" gap
    # (see DOGFOOD_FINDINGS Q3). Spawns ingester --bootstrap as a
    # detached background process — install.py returns to the user
    # promptly, the bootstrap finishes async (typically seconds for a
    # handful of sessions, minutes for hundreds).
    _progress("bootstrap_sessions",
              "scanning ~/.claude/projects for past sessions and ingesting in background")
    venv_py = venv / ("Scripts/python.exe" if OS_NAME == "Windows" else "bin/python3")
    ingester = PLUGIN_ROOT / "code" / "ingester.py"
    if venv_py.exists() and ingester.exists():
        try:
            log_path = data_root() / "bootstrap.log"
            if OS_NAME == "Windows":
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
            green(f"  ✓ session bootstrap running in background; tail {log_path}")
        except Exception as e:
            yellow(f"  (bootstrap spawn failed: {e}; you can run "
                   f"`{venv_py} {ingester} --bootstrap` manually)")

    # Success: clear any stale incomplete marker.
    _clear_install_status()

    print()
    green("Installation complete.")
    print()
    print("Next steps:")
    print("  - Ensure ~/.local/bin is in your PATH (or open a new shell)")
    print("  - Try:           claude-dejavu sessions")
    print("  - Search:        claude-dejavu search 'your query'")
    print("  - Restore lost:  claude-dejavu restore --list")
    print("  - Workspace:     claude-dejavu workspace create my-stack --projects=repo1,repo2")
    print()
    print(f"Config:  {root}/config.env")


def _normalize_mcp_json() -> None:
    """Rewrite plugin .mcp.json AND hooks/hooks.json with the OS-correct
    python command.

    On Windows: 'python' (works after standard Python installer / winget /
    scoop / choco).
    On Linux/macOS: 'python3' (canonical name; on Ubuntu 22+ 'python' may
    be missing entirely or symlinked to python2).

    Repo defaults to 'python' so Windows works out of the box. This step
    swaps in 'python3' on Linux/macOS during install so subsequent Setup
    hook runs and MCP launches use the correct interpreter.

    Also handles hooks.json — including the Setup hook itself, so plugin
    updates that re-trigger Setup will use the right python.

    SKIPPED when running from a git clone (PLUGIN_ROOT/.git exists).
    Mutating the source files there would leave the working tree dirty
    and confuse `git status` for contributors. Marketplace installs
    live in per-version cache dirs that are safe to mutate.
    """
    if (PLUGIN_ROOT / ".git").is_dir():
        cyan("    Skipping .mcp.json normalization "
                "(running from a git clone — would dirty the tree). "
                "If you need OS-correct command tokens for local dev, "
                "edit .mcp.json + hooks/hooks.json by hand.")
        return

    target_cmd = "python" if OS_NAME == "Windows" else "python3"

    # 1. .mcp.json
    mcp_path = PLUGIN_ROOT / ".mcp.json"
    if mcp_path.exists():
        try:
            d = json.loads(mcp_path.read_text())
            srv = d.get("mcpServers", {}).get("claude-dejavu", {})
            if srv.get("command") != target_cmd:
                srv["command"] = target_cmd
                mcp_path.write_text(json.dumps(d, indent=2) + "\n")
                green(f"  ✓ Normalized .mcp.json command → {target_cmd}")
        except Exception as e:
            yellow(f"  (couldn't normalize .mcp.json: {e})")

    # 2. hooks/hooks.json — every "command": "python ..." → "python3 ..." on
    # POSIX. Done as a string substitution that's safe because the only
    # 'python' tokens in the file are at command-start.
    hooks_path = PLUGIN_ROOT / "hooks" / "hooks.json"
    if hooks_path.exists() and target_cmd != "python":
        try:
            txt = hooks_path.read_text()
            new_txt = txt.replace('"command": "python ', f'"command": "{target_cmd} ')
            if new_txt != txt:
                hooks_path.write_text(new_txt)
                green(f"  ✓ Normalized hooks.json commands → {target_cmd}")
        except Exception as e:
            yellow(f"  (couldn't normalize hooks.json: {e})")


if __name__ == "__main__":
    main()
