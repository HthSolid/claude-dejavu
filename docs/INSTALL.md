# claude-dejavu — Installation Guide

If you've installed Claude Code, you're 90 % of the way there. This guide walks
you through the rest assuming **zero prior knowledge** of Docker, PostgreSQL,
or Weaviate.

> **Recommended path: install via the Claude Code marketplace.** It's a
> two-line install with everything else automatic.
>
> ```
> /plugin marketplace add HthSolid/claude-dejavu
> /plugin install claude-dejavu
> ```
>
> The plugin's `Setup` hook auto-runs the installer (provisions Postgres +
> Weaviate via Docker, creates the venv, applies the schema, wires MCP).
> First-install takes 1-5 minutes; subsequent sessions are instant.
>
> The **only thing you need to install yourself first is Docker** — see
> the per-OS section below for a one-line install. If anything else goes
> wrong (Docker not running, port conflict), the next time you open
> Claude Code it'll tell you in chat what to fix — no terminal hunting.
>
> The standalone path (clone + `python3 scripts/install.py`) is the
> backup option for CI environments, dev forks, or hosts without the
> `claude` CLI. See the [standalone section at the bottom](#standalone-install-no-marketplace-plugin).

If you already have Docker + Postgres + Weaviate running, skip to the
[Quick install](#quick-install-i-already-have-docker) section at the bottom.

---

## Pick your operating system

- [macOS](#macos)
- [Linux (Ubuntu / Debian)](#linux-ubuntu--debian)
- [Linux (Fedora / RHEL / CentOS)](#linux-fedora--rhel--centos)
- [Linux (Arch / Manjaro)](#linux-arch--manjaro)
- [Windows (native)](#windows-native)
- [Windows (WSL2 — recommended for Windows)](#windows-wsl2--recommended)

---

## macOS

### 1. Install Homebrew (if you don't have it)

Homebrew is the standard package manager for macOS. Open Terminal and run:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the on-screen instructions. When done, run `brew --version` to verify.

### 2. Install Docker Desktop

Docker is what runs the database services for claude-dejavu.

```bash
brew install --cask docker
```

After install, open Docker Desktop from your Applications folder and wait for
it to start (whale icon in the menu bar should show "Docker Desktop is running").

### 3. Install Python 3.11+ and the Postgres client

```bash
brew install python@3.11 postgresql@16
brew link postgresql@16     # make psql command available in your shell
```

Verify:
```bash
python3 --version    # 3.11.x or higher
psql --version       # 16.x
docker --version     # 27.x or higher
```

### 4. Install claude-dejavu (recommended: marketplace)

In Claude Code:

```
/plugin marketplace add HthSolid/claude-dejavu
/plugin install claude-dejavu
```

The plugin's `Setup` hook auto-runs the installer end-to-end (Docker
auto-provision, schema, venv, MCP wiring). First-install takes 1-5
minutes while images pull.

Standalone alternative (CI / dev forks / hosts without `claude` CLI) at
the [bottom of this guide](#standalone-install-no-marketplace-plugin).

---

## Linux (Ubuntu / Debian)

### 1. Update package lists

```bash
sudo apt update
```

### 2. Install Docker

```bash
# Add Docker's official GPG key + repository
curl -fsSL https://get.docker.com | sudo sh

# Let your user run docker without sudo (re-login after)
sudo usermod -aG docker $USER
```

Log out and log back in (or `newgrp docker`), then verify:
```bash
docker --version
docker compose version
```

### 3. Install Python 3.11+ and the Postgres client

```bash
sudo apt install -y python3 python3-venv postgresql-client git
```

Verify:
```bash
python3 --version    # 3.10+ acceptable, 3.11+ recommended
psql --version
```

### 4. Install claude-dejavu (recommended: marketplace)

In Claude Code:

```
/plugin marketplace add HthSolid/claude-dejavu
/plugin install claude-dejavu
```

The plugin's `Setup` hook auto-runs the installer end-to-end. Done.

---

## Linux (Fedora / RHEL / CentOS)

### 1. Install Docker

```bash
sudo dnf install -y dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

Log out and back in.

### 2. Install Python + Postgres client + git

```bash
sudo dnf install -y python3 python3-pip postgresql git
```

### 3. Install claude-dejavu (recommended: marketplace)

In Claude Code:

```
/plugin marketplace add HthSolid/claude-dejavu
/plugin install claude-dejavu
```

---

## Linux (Arch / Manjaro)

```bash
sudo pacman -S --needed docker docker-compose python postgresql-libs git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
# log out, log back in
```

Then, in Claude Code:

```
/plugin marketplace add HthSolid/claude-dejavu
/plugin install claude-dejavu
```

---

## Windows (WSL2 — recommended)

Claude Code itself runs on Windows, but for claude-dejavu we strongly recommend
WSL2. Install once and forget — far simpler than native Windows + native
Postgres + native Docker juggling.

### 1. Install WSL2 + Ubuntu

Open PowerShell as Administrator:

```powershell
wsl --install -d Ubuntu
```

Reboot, then set a Linux username + password when prompted.

### 2. From inside WSL Ubuntu, follow the [Linux (Ubuntu / Debian)](#linux-ubuntu--debian) steps above.

The plugin and your Claude Code session both work seamlessly across the
Windows / WSL boundary. Claude Code on Windows can talk to a WSL-hosted
Postgres + Weaviate without any extra config.

---

## Windows (native, no WSL)

We support native Windows, but the install is more manual.

### 1. Install Docker Desktop

Download from <https://www.docker.com/products/docker-desktop/> and install.
Enable the WSL2 backend during setup if prompted (it's faster than Hyper-V).

### 2. Install Python 3.11+ and Git

Download Python from <https://www.python.org/downloads/windows/> — make sure
to check "Add python.exe to PATH" during install.

Download Git from <https://git-scm.com/download/win>.

### 3. Install Postgres client (psql.exe)

Two options:

**Option A — Scoop** (a CLI package manager for Windows):

```powershell
# Install Scoop first if you don't have it
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
iwr -useb get.scoop.sh | iex

scoop install postgresql
```

**Option B — official PG installer**:

Download from <https://www.postgresql.org/download/windows/> — during install,
**uncheck** the "Server" component (you only need the client/psql, not a local
PG server — Docker provides that). Make sure the install dir is added to PATH.

Verify in a fresh PowerShell:
```powershell
psql --version
```

### 4. Install claude-dejavu (recommended: marketplace)

In Claude Code:

```
/plugin marketplace add HthSolid/claude-dejavu
/plugin install claude-dejavu
```

The plugin's `Setup` hook auto-runs the installer. Windows users:
ensure Docker Desktop is **running** (whale icon steady) before the
install — if it isn't, the plugin's first SessionStart will tell you
in chat to launch it.

---

## What the installer actually does (so you trust it)

When you press `a` for auto-provision, `scripts/install.py`:

1. **Creates** `~/.local/share/claude-dejavu/` (or platform-equivalent) for
   off-tree backups + config + venv.
2. **Spins up** the bundled `docker-compose.minimal.yml` stack:
   - `claude-dejavu-postgres` (postgres:16) on port 5454
   - `claude-dejavu-weaviate` (semitechnologies/weaviate:1.27.5) on port 8889
   - `claude-dejavu-transformers` (sentence-transformers/all-MiniLM-L6-v2)
3. **Creates** the `claude_dejavu_<your-username>` database + applies the schema.
4. **Creates** the `ClaudeDejavuTurn` Weaviate class with text2vec-transformers.
5. **Creates** a Python virtualenv at `~/.local/share/claude-dejavu/venv/` and
   installs `psycopg2-binary` + `mcp` (the Anthropic MCP SDK).
6. **Symlinks** the `claude-dejavu` CLI into `~/.local/bin/` (or
   `%LOCALAPPDATA%\Microsoft\WindowsApps\` on Windows).
7. **Registers** the MCP server in your `~/.claude/settings.json` (with a
   `.bak` backup of the original).
8. **Wires** the Stop hook so future sessions get mirrored + ingested
   automatically.

**Nothing critical lives in your project directory.** All state is off-tree
under `~/.local/share/claude-dejavu/`. You can `git clean -xfd` your project
freely; claude-dejavu's data survives.

---

## Quick install (I already have Docker)

In Claude Code:

```
/plugin marketplace add HthSolid/claude-dejavu
/plugin install claude-dejavu
```

The Setup hook detects existing Postgres + Weaviate on common ports
(verified by shape, not just port — a SearXNG on 8889 won't pose as
Weaviate). If nothing is found, it auto-provisions via the bundled
docker-compose. No prompts.

---

## Standalone install (no marketplace plugin)

For CI environments, dev forks, or hosts without the `claude` CLI:

```bash
git clone https://github.com/HthSolid/claude-dejavu
cd claude-dejavu
python3 scripts/install.py --register-mcp
```

`--register-mcp` writes a `claude-dejavu` entry into
`~/.claude/settings.json` so a Claude Code instance can find the MCP
server without going through the marketplace path. **Don't combine
this with the marketplace install** — you'll get a duplicate
registration; the installer detects this and skips the registration
step in that case.

---

## Verifying the install

```bash
# Diagnose every layer (venv, PG, Weaviate, schema, symbol index, hooks)
claude-dejavu doctor

# List + change settings (lint mode, scope defaults, etc.)
claude-dejavu config list

# Should list any sessions captured so far
claude-dejavu sessions

# Run the full smoke battery (sandboxed, won't touch your real data)
PG_HOST=localhost PG_PORT=5454 PG_USER=postgres PG_PASS=postgres \
  WV_URL=http://localhost:8889 \
  python3 tests/smoke.py
```

Expected smoke output: `PASS: 52     FAIL: 0     OK` (as of v0.3.3).

In Claude Code, the same diagnostics are available as slash commands:
`/dejavu-doctor`, `/dejavu-config list`, `/dejavu-sessions`.

---

## Common problems

### "psql: command not found"
Postgres client isn't on your PATH. See the per-OS install steps above.

### "docker: command not found"
Install Docker per the OS-specific steps. On Linux: `curl -fsSL https://get.docker.com | sudo sh`.

### "Got permission denied while trying to connect to the Docker daemon" (Linux)
Your user isn't in the `docker` group. Run `sudo usermod -aG docker $USER`,
log out, log back in.

### "Postgres didn't come up within 30s"
First-time Docker pulls can take a while on slow connections. Re-run the
installer; the second time the images are cached and PG starts in <5 s.

### "Weaviate didn't come up within 60s"
The transformers container downloads a ~110 MB model on first start. Slow.
Re-run; second start is fast.

### "claude-dejavu: command not found" after install
Check that `~/.local/bin` is in your `PATH`. Add to your shell rc:

```bash
# ~/.bashrc or ~/.zshrc
export PATH="$HOME/.local/bin:$PATH"
```

Open a new terminal, retry.

### "I want to uninstall everything"

```bash
docker compose -f docker/docker-compose.minimal.yml down -v
rm -rf ~/.local/share/claude-dejavu
rm ~/.local/bin/claude-dejavu
# Remove claude-dejavu MCP entry from ~/.claude/settings.json (it has a .bak)
```

---

## Still stuck?

Open an issue at <https://github.com/HthSolid/claude-dejavu/issues> with:

- Your OS + version (`uname -a` or Windows version)
- Output of `python3 --version`, `docker --version`, `psql --version`
- Full output of `python3 scripts/install.py 2>&1` (redact any sensitive paths)

We'll figure it out.
