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
      1. pg_isready (best — speaks the Postgres startup protocol)
      2. psql -c 'SELECT 1' against the default `postgres` db
      3. Refuse to declare success on TCP-only.
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
            cyan("    Spinning up bundled Postgres + Weaviate via docker-compose…")
            compose_file = PLUGIN_ROOT / "docker" / "docker-compose.minimal.yml"
            r = subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                red(f"    docker compose up failed: {r.stderr[:400]}")
                yellow(f"    Try manually: docker compose -f {compose_file} up -d")
                sys.exit(1)
            # Wait for PG to become ready (up to 30s)
            green("    Waiting for Postgres to accept connections (up to 30s)…")
            ready = False
            last_reason = ""
            for i in range(30):
                ok, why = probe_pg("localhost", 5454)
                if ok:
                    ready = True; break
                last_reason = why
                import time as _t; _t.sleep(1)
            if not ready:
                red(f"    Postgres didn't come up within 30s ({last_reason}). Check `docker logs claude-dejavu-postgres`")
                sys.exit(1)
            host, port, user, password = "localhost", "5454", "postgres", "postgres"
            green(f"    ✓ Bundled Postgres up on {host}:{port}")
        else:
            host = ask("    PG host", "localhost", args.non_interactive)
            port = ask("    PG port", "5432", args.non_interactive)
            user = ask("    PG user", "postgres", args.non_interactive)
            password = ask("    PG password", "postgres", args.non_interactive)

    db = f"claude_dejavu_team" if args.shared else f"claude_dejavu_{OS_USER}"
    db = ask("    PG database name", db, args.non_interactive)

    # Create DB + apply schema using psql if available.
    psql = shutil.which("psql")
    if not psql:
        red("    psql not found in PATH.")
        if OS_NAME == "Linux":
            yellow("    Install: sudo apt install postgresql-client      (Debian/Ubuntu)")
            yellow("             sudo dnf install postgresql              (Fedora/RHEL)")
            yellow("             sudo pacman -S postgresql-libs           (Arch)")
        elif OS_NAME == "Darwin":
            yellow("    Install: brew install libpq && brew link --force libpq")
        elif OS_NAME == "Windows":
            yellow("    Install: download Postgres installer from https://www.postgresql.org/download/windows/")
            yellow("    Or use scoop: scoop install postgresql")
        yellow("    Then re-run: python3 scripts/install.py")
        sys.exit(1)
    env = {**os.environ, "PGPASSWORD": password}
    # Create DB if missing (idempotent)
    subprocess.run([psql, "-h", host, "-p", port, "-U", user, "-tc",
                    f"SELECT 1 FROM pg_database WHERE datname='{db}'"],
                   env=env, capture_output=True)
    subprocess.run([psql, "-h", host, "-p", port, "-U", user,
                    "-c", f"CREATE DATABASE {db}"],
                   env=env, capture_output=True)
    r = subprocess.run([psql, "-h", host, "-p", port, "-U", user, "-d", db,
                        "-f", str(SCHEMA_FILE)],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        red(f"    schema apply failed: {r.stderr[:300]}")
        sys.exit(1)
    green(f"  ✓ Database {db} ready on {host}:{port}")
    return {"PG_HOST": host, "PG_PORT": port, "PG_USER": user, "PG_PASS": password, "PG_DB": db}


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
                red("    Docker not found in PATH. See docs/INSTALL.md to install Docker.")
                sys.exit(1)
            # docker-compose.minimal.yml brings up weaviate + transformers along with postgres
            compose_file = PLUGIN_ROOT / "docker" / "docker-compose.minimal.yml"
            cyan("    Bringing up Weaviate via docker-compose…")
            subprocess.run(["docker", "compose", "-f", str(compose_file), "up", "-d", "weaviate", "transformers"],
                           capture_output=True, text=True)
            green("    Waiting for Weaviate to be ready (up to 60s — first start pulls model)…")
            import time as _t
            for i in range(60):
                meta = probe_weaviate("http://localhost:8889")
                if meta:
                    detected = "http://localhost:8889"
                    break
                _t.sleep(1)
            if not detected:
                red("    Weaviate didn't come up within 60s. Check `docker logs claude-dejavu-weaviate`")
                sys.exit(1)
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
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    bin_dir = venv_dir / ("Scripts" if OS_NAME == "Windows" else "bin")
    py = bin_dir / ("python.exe" if OS_NAME == "Windows" else "python3")
    pip = bin_dir / ("pip.exe" if OS_NAME == "Windows" else "pip")
    subprocess.run([str(pip), "install", "--quiet", "--upgrade", "pip"], capture_output=True)
    subprocess.run([str(pip), "install", "--quiet",
                    "psycopg2-binary", "mcp",
                    # tree-sitter for accurate code symbol grounding (v0.3.0+).
                    # Pinned to known-good wheel-shipping versions so installs are
                    # offline-capable on Linux/macOS/Windows. If wheels are
                    # unavailable on a given platform, the indexer transparently
                    # falls back to the regex/ast parsers — install still succeeds.
                    "tree-sitter==0.21.3", "tree-sitter-languages==1.10.2",
                    # PyYAML for parsing OpenAPI/Swagger YAML files in the
                    # route indexer (v0.3.2.1+). Falls back to no-op if absent.
                    "pyyaml>=6.0"],
                   capture_output=True)
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
        bin_dir = Path.home() / "AppData" / "Local" / "Microsoft" / "WindowsApps"
    else:
        bin_dir = Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    if OS_NAME == "Windows":
        cli_path = bin_dir / "claude-dejavu.bat"
        cli_path.write_text(
            f'@echo off\n'
            f'"{venv / "Scripts" / "python.exe"}" "{PLUGIN_ROOT / "code" / "recall.py"}" %*\n'
        )
    else:
        cli_path = bin_dir / "claude-dejavu"
        cli_path.write_text(
            f'#!/bin/bash\n'
            f'exec "{venv / "bin" / "python3"}" "{PLUGIN_ROOT / "code" / "recall.py"}" "$@"\n'
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
    """
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
