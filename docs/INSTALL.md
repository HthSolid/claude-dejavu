# claude-dejavu — Installation Guide

If you've installed Claude Code, you're 90 % of the way there. This guide walks
you through the rest assuming **zero prior knowledge** of Docker, PostgreSQL,
or Weaviate.

> ## ⚠️ Note for Windows users
>
> Versions **v0.5.0c and earlier** had a hook-dispatch bug that broke
> the marketplace install on Windows native (symptom:
> `MCP error -32000: Connection closed`). Fixed in **v0.5.0d**.
>
> If you previously installed an older version and the plugin appeared
> "enabled" but did nothing, refresh the marketplace clone and re-run
> the installer manually:
>
> ```powershell
> cd "$env:APPDATA\claude\plugins\marketplaces\claude-dejavu"
> git fetch origin main
> git reset --hard origin/main
> python "$env:APPDATA\claude\plugins\marketplaces\claude-dejavu\scripts\install.py"
> ```
>
> Then fully restart Claude Code. Fresh installs of v0.5.0d+ don't
> need this — the Setup hook fires correctly.
>
> If you still hit issues after upgrading: tracking
> [issue #2](https://github.com/HthSolid/claude-dejavu/issues/2) — please
> add `claude-dejavu doctor` output + `winver` to a comment so we can
> rule out remaining edge cases.

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

When you press `a` for auto-provision, `scripts/install.py` runs nine
labelled steps. Re-running is idempotent — each step skips if its
prerequisite is already satisfied.

- **`[1/9] Data root`** — creates `~/.local/share/claude-dejavu/` (or
  the platform-equivalent `LOCALAPPDATA` / `Application Support`
  directory) for off-tree backups + config + venv.
- **`[2/9] Postgres`** — detects an existing `claude-dejavu-postgres`
  container or spins up `postgres:16` on port 5454 via the bundled
  `docker-compose.minimal.yml`. Creates the
  `claude_dejavu_<your-username>` database and applies the schema.
- **`[3/9] Weaviate`** — same shape: `semitechnologies/weaviate:1.27.5`
  on port 8889 plus the matching `transformers-inference` container.
  Creates the `ClaudeDejavuTurn` class.
- **`[4/9] Python virtualenv`** — creates the venv at
  `~/.local/share/claude-dejavu/venv/` and installs `psycopg2-binary`,
  `mcp`, `tree-sitter`, `tree-sitter-languages`, `pyyaml`. **Also
  attempts to install the optional `dejavu_bridge` wheel** (v0.8.6 —
  see below) via
  `pip install --find-links $DEJAVU_BRIDGE_INDEX_URL dejavu_bridge>=0.1.0`.
  Falls back to the in-tree stub silently if the wheel isn't reachable.
- **`[5/9] Config + CLI`** — writes `config.env`, symlinks the
  `claude-dejavu` CLI into `~/.local/bin/` (or
  `%LOCALAPPDATA%\Microsoft\WindowsApps\` on Windows).
- **`[6/9] MCP server registration`** — registers `claude-dejavu` in
  your `~/.claude/settings.json` (with a `.bak` backup of the
  original).
- **`[7/9] Migrate legacy session-copy state`** (v0.8.0+) — auto-detects
  legacy rsync chains (`~/.claude-backups/snapshots/`,
  `claude-backup.timer`, etc.) and runs the 10-step `migrate-from-rsync`
  interactively (or silently under `--auto`). Also installs `restic`
  locally (cross-platform, no sudo) via `code/install_restic.py` and
  the rescue Go binary via `code/install_rescue.py`. Phase-marker
  resume after crashes; per-step rollback contracts.
- **`[8/9] Seed documentation embeddings`** — embeds every `.md` under
  the current repo into the `ClaudeDejavuDoc` Weaviate class so
  `dejavu_search(kinds=["doc"])` works on day one.
- **`[9/9] Backfill ClaudeDejavuCode.repo_id`** — back-tags any
  pre-v0.7 `ClaudeDejavuCode` objects with the resolved stable
  `repo_id`. Non-fatal — install never aborts on backfill failure.

**Nothing critical lives in your project directory.** All state is off-tree
under `~/.local/share/claude-dejavu/`. You can `git clean -xfd` your project
freely; claude-dejavu's data survives.

### `restic` install + GPG signature pinning (v0.8.0+)

Step `[7/9]` also installs `restic` (≥ 0.17.0; pinned 0.17.3) locally
into the dejavu data root without sudo. The installer:

1. Downloads the pinned release artifact + SHA256.
2. Verifies the signature against the pinned maintainer fingerprint
   (`BD3F7A907` last 10 hex). The pubkey is fetched from a fallback
   chain: `keys.openpgp.org` → `keyserver.ubuntu.com` → `pgp.mit.edu`
   (v0.8.3 fix — `keys.openpgp.org` doesn't host the restic
   maintainer's pubkey, so the v0.8.0 ship was broken on every fresh
   install).
3. Falls back to SHA256-only under `--auto` if `gpg` is missing or all
   keyservers are unreachable. **Signature-fingerprint mismatches
   always fail** (security-critical).

Cross-platform: clears macOS Gatekeeper quarantine, runs `Unblock-File`
on Windows.

### `dejavu_bridge` (closed-source binary wheel, v0.8.6)

claude-dejavu v0.8.6 can use the `dejavu_bridge` Python wheel for
compressed gist generation, anchor-preserving distill, and
token-budget-aware JIT recall. The wheel ships pre-compiled binary
code from the closed-source `dejavu` project (private repo, not
redistributable except as the published wheel artifacts).

**If the wheel is unavailable at install time, claude-dejavu runs in
STANDARD MODE** — `code/dejavu_bridge_stub.py` activates and
re-exposes the same six primitives with naive deterministic Python.
The whole test suite passes against the stub; recall keeps working.
What you lose: gist quality drops to "first-paragraph-ish" and
JIT recall falls back to fixed top-N truncation instead of greedy
token-budget fill.

To install the wheel, point `DEJAVU_BRIDGE_INDEX_URL` at a directory or
URL containing the wheel artifacts before running the installer:

```bash
export DEJAVU_BRIDGE_INDEX_URL=file:///path/to/wheels/
python3 scripts/install.py --auto
```

SessionStart logs which mode is active on every fresh session. Re-check
with `claude-dejavu doctor`.

### Post-install: opt-in features

These are off by default — flip them on in
`<DATA_ROOT>/config.env` when you want them.

| Setting | Default | What it does |
|---|---|---|
| `JIT_RECALL_ENABLED` | `false` | Master switch for the UserPromptSubmit JIT recall hook. When `true`, every user prompt fans out to a BM25 keyword search against past sessions and the top hits' gists get injected as additional context. Defensive-by-design — every failure mode exits 0 silently. |
| `JIT_RECALL_TOP_N` | `3` | Hits to inject (legacy v0.8.5 path; v0.8.6+ uses greedy fill instead). |
| `JIT_RECALL_TOKEN_CAP` | `500` | chars/4 budget for v0.8.6 `dejavu_bridge.disclose` greedy fill. |
| `JIT_RECALL_SCORE_THRESHOLD` | `2.0` | Silence floor on BM25 (range 1-20+, NOT 0-1 cosine — `2.0` is the retuned default; `0.6` from v0.8.5 silently rejected every real hit). |
| `JIT_RECALL_MIN_PROMPT_CHARS` | `40` | Skip short prompts (typo, "ok", "hm"). |
| `INJECT_SESSION_START` | `true` | Set `false` to disable the SessionStart preamble + the cross-project release digest. |

Preview what the hook would inject before flipping it on:

```bash
claude-dejavu jit-recall-preview "your typical prompt here"
```

### Post-install: legacy `.deleteme` tree (v0.8.3)

If you upgraded from v0.5 / v0.6 / v0.7 / pre-v0.8.0, the
`migrate-from-rsync` step renamed your old
`~/.claude-backups/snapshots/` chain to
`~/.claude-backups/snapshots.deleteme.<unix-ts>/` and left it on disk
as a safety net. The `[7/9]` summary reports its size + the
`reclaim-legacy` command. Once you've verified the new restic repo
has everything you need, reclaim it:

```bash
claude-dejavu session-copy reclaim-legacy --dry-run
claude-dejavu session-copy reclaim-legacy --yes
```

The 2026-05-15 incident left a 513 GB legacy tree on one host; running
`reclaim-legacy` after verifying the restic repo brought that back to
the ~5-10 GB the new policy targets.

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

Expected smoke output: `PASS: NNN     FAIL: 0     OK`. The full suite
(`tests/run_all.py`) reports **926 / 0** as of v0.8.6 (was 858 in v0.8.5,
808 in v0.8.4, 714 in v0.8.3).

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
