# Troubleshooting

If something went wrong during install or a tool isn't working, this
is the page. Most issues are one of: Docker not running, port
conflict, stale install path, or a backgrounded service that didn't
come up cleanly. **Always start with `/dejavu-doctor` —** it
auto-diagnoses everything below and tells you exactly what to fix.

```bash
# Inside Claude Code:
/dejavu-doctor

# Or from a terminal:
claude-dejavu doctor
```

The output is self-explanatory: each check reports `ok` / `warn` /
`fail`, and failed checks include a `→ fix:` line you can copy-paste.

---

## Quick reference — common error messages

| Error you see | What it means | How to fix |
|---|---|---|
| `MCP error -32000: Connection closed` | The dejavu MCP server isn't reachable. 99% of the time this is **Docker not running** (Postgres + Weaviate live in containers). | Start Docker Desktop, wait for the whale icon to be steady, then `/dejavu-doctor` to re-check. |
| `MCP error -32000` immediately at session start | Either Docker just hasn't finished starting, or the install never completed. | Wait 10s and re-check; if persistent, run `claude-dejavu doctor`. |
| `doctor reports: postgres → fail` | PG container is down or wrong port. | Check `docker ps` for `claude-dejavu-postgres`. Start it: `docker start claude-dejavu-postgres`. If the container doesn't exist, re-run `python scripts/install.py --auto`. |
| `doctor reports: weaviate → fail` | Weaviate container is down. | `docker start claude-dejavu-weaviate claude-dejavu-transformers`. If they don't exist, re-run install. |
| `doctor reports: schema → fail` | PG is up but the dejavu schema isn't applied. | `psql -h localhost -p <PG_PORT> -U postgres -d claude_dejavu_$USER -f code/schema.sql`. The doctor's fix hint includes the exact command with your ports filled in. |
| `doctor reports: hooks → warn` | The plugin's hooks file is out of sync. | `claude plugin update claude-dejavu` or re-install via the marketplace. |
| `doctor reports: error_log → warn` | Past errors recorded — not a broken state, just an FYI. | `claude-dejavu logs --with-traceback` to see what failed. |
| `Postgres didn't come up within 30s` (during install) | Container started but isn't accepting connections in time. | Usually a slow disk or low-memory machine. Re-run install — the second pass usually works. If persistent, check `docker logs claude-dejavu-postgres`. |
| `Weaviate transformers-inference timeout` | The vectorizer container is downloading a model on first start (~500 MB). | Wait. First-start can take 2-5 min on slow links. `docker logs claude-dejavu-transformers -f` shows progress. |
| `port 5432 already in use` | You have another Postgres on the host. | Install picks an alternative port automatically (check `~/.local/share/claude-dejavu/config.env` for the actual `PG_PORT`). If you want to force-bind 5432, stop the other Postgres first. |
| `port 8080 already in use` | Same, but Weaviate. Install uses the next free port. | Check `WV_URL` in `~/.local/share/claude-dejavu/config.env`. |
| `command not found: claude-dejavu` (after install) | The CLI shim wasn't placed on `$PATH`, or it's in `~/.local/bin/` and that isn't in your `$PATH`. | Add `~/.local/bin` to `$PATH`, or invoke via `python ~/.local/share/claude-dejavu/venv/bin/claude-dejavu` directly. |
| `claude-dejavu` works but Claude Code doesn't show `/dejavu-*` slash commands | The plugin's installed cache is stale (different version than the marketplace). | `/plugin update claude-dejavu` from inside Claude Code, then restart the session. |

---

## Install troubleshooting

### "I started the install, it failed at <step X>, how do I resume?"

The installer is **idempotent** — re-running it picks up where it
left off. Each step checks whether its prerequisite is already
satisfied and skips if so.

```bash
# After fixing the underlying issue (e.g. starting Docker):
python ~/.local/share/claude-dejavu/scripts/install.py --auto

# Or if you didn't get that far, re-clone and re-run:
git clone https://github.com/HthSolid/claude-dejavu
cd claude-dejavu
python scripts/install.py
```

The installer's typical run:
1. Create a Python venv
2. Install Python dependencies into it
3. Start a Postgres container (waits up to 30s for ready)
4. Start a Weaviate + transformers-inference container pair (first
   start downloads the embedding model — slow)
5. Apply the schema
6. Wire the Claude Code hooks
7. Print "INSTALL COMPLETE"

Any step can fail. Re-running re-attempts only the failed step plus
everything after it.

### "I installed via the marketplace and it says Docker isn't running"

The plugin's setup hook tries to bring up Docker containers
unattended, but it cannot **start Docker itself** — only the
containers. If Docker Desktop isn't running on your machine, the
setup hook can't help.

The fix:
1. Start Docker Desktop. Wait for it to fully load (whale icon
   steady, not animating).
2. From inside Claude Code, run `/dejavu-doctor`.
3. If doctor reports `postgres → fail`, the install hook didn't
   create the containers (Docker wasn't there at install time).
   Run install manually:
   ```bash
   # Linux/macOS:
   python ~/.local/share/claude-dejavu/scripts/install.py --auto

   # Windows (PowerShell):
   python "$env:APPDATA\claude-dejavu\scripts\install.py" --auto
   ```
4. Re-run `/dejavu-doctor`. Should be green.

### "Install hangs at 'Waiting for Postgres'"

Look at `docker logs claude-dejavu-postgres`. Common causes:
- **First start with persistent volume**: PG is initializing the
  data dir. ~10s on SSD, can be 30-60s on slow disks.
- **Permission errors**: the volume mount can't be written. Check
  `~/.local/share/claude-dejavu/pg-data/` permissions on Linux.
- **Out of disk**: PG won't start with <100 MB free.

### "Install hangs at 'Waiting for Weaviate transformers'"

The first start downloads `sentence-transformers/all-MiniLM-L6-v2`
(~80 MB) plus its runtime (~500 MB). Slow links can take 5+ min.
`docker logs claude-dejavu-transformers -f` shows download progress.

If the download stalls, restart the container:
```bash
docker restart claude-dejavu-transformers
```

---

## Runtime troubleshooting

### "Lint isn't catching fabricated names"

The lint requires an indexed project. New projects start empty.

```bash
# In your project's root:
claude-dejavu reindex

# Verify the index has something:
claude-dejavu symbols --limit=5
```

The PreToolUse hook silent-passes on projects with zero indexed
symbols, by design — we don't want to false-positive on a
freshly-cloned repo before it's been indexed.

### "Doctor says everything is OK but commands still fail"

Three possibilities:
1. The CLI shim is older than the venv. Re-install.
2. The MCP server is using a cached process. Restart Claude Code.
3. The `installed_plugins.json` points at an old cache directory.
   Run `/plugin update claude-dejavu` from inside Claude Code.

### "I see `/dejavu-search` but not `/dejavu-doctor`"

Your installed plugin is older than the marketplace. The installed
cache only has the slash commands present at the version that was
installed; newer commands ship with newer versions.

```
# In Claude Code:
/plugin update claude-dejavu

# Then restart the session (close + reopen the editor or run /quit
# and reconnect).
```

### "Sessions aren't being mirrored / restore says 'nothing to restore'"

The off-tree backup writes on the **Stop** event of a session. If
no sessions have ended since install, there's nothing to mirror.
Have at least one full session, then check:

```bash
claude-dejavu restore --list
```

If the list is still empty after sessions have run:
- Check the Stop hook is wired: `claude-dejavu doctor` → `hooks` row
  should report 5+ events.
- Check the off-tree dir: `ls ~/.local/share/claude-dejavu/chat-logs/`
  (Linux) — should contain mirrored `.jsonl` files.

### "Reindex is slow"

Expected for first run on a large project — symbol indexing walks
the whole tree. Subsequent runs use the **fingerprint cache** (v0.4.5)
and complete in seconds when nothing has changed.

Slow indexers:
- **quickwin** (CSS classes + i18n keys + npm scripts + asset paths):
  inserts are per-row; a project with 10 k+ CSS classes takes
  3-4 min on first run. Cache hits are instant.
- **module imports**: parses every package's `.d.ts`. Big
  `node_modules` = slow first run.

Force a full re-walk with `--force`:
```bash
claude-dejavu reindex --force
```

---

## Windows-specific notes

### `python` must be on PATH (the canonical Python command)

The plugin's hooks invoke `python "${CLAUDE_PLUGIN_ROOT}/bin/dejavu-hook.py"`.
On Windows that's the standard install command — the official
Python installer creates `python.exe` and `py.exe`.

If `python --version` works in PowerShell, you're set.

If it doesn't:
1. Install Python 3.10+ from <https://python.org>. **Tick the
   "Add Python to PATH" checkbox** in the installer (it's off by
   default).
2. Or use `py.exe` (always present after Python install) by
   creating an alias: open PowerShell as admin and run
   `New-Alias -Name python -Value py -Scope Global -Force`. Add
   that to your PowerShell profile to persist.

On **Linux** (especially Ubuntu 22.04+), `python` may not exist
even though `python3` does. Install the alias package:
```bash
sudo apt install python-is-python3
```
On macOS modern installers ship `python3` but not always `python`.
Either install `python` via Homebrew (`brew install python`) or
ensure your `python3` binary is symlinked.

### Docker Desktop must be running

Not just installed — **running**. Look for the whale icon in your
system tray. If it's animating, wait until it stops; if it's not
there, launch Docker Desktop from the Start menu.

WSL2 backend is recommended. The native (Hyper-V) backend has had
intermittent volume-mount issues with the dejavu install on some
systems.

### Path separator quirks

The plugin uses Pure Python pathlib, so Windows paths work
transparently. You may see `\` in some logs and `/` in others —
both are valid.

### `claude-dejavu` CLI on Windows

The shim lives at `%APPDATA%\claude-dejavu\bin\claude-dejavu.exe`.
If `claude-dejavu` isn't found at the command line, add that
directory to your `Path` env var, or invoke via:

```powershell
python "$env:APPDATA\claude-dejavu\venv\Scripts\python.exe" `
    "$env:APPDATA\claude-dejavu\code\recall.py" doctor
```

### Antivirus blocking the install

Some Windows AV products quarantine Python venv binaries. If
install fails with "permission denied" on `venv\Scripts\python.exe`,
add `%APPDATA%\claude-dejavu\` to your AV exclusions and re-run.

---

## How to file a useful bug report

We need three things to debug effectively:

1. **What you ran** — the exact command or slash-command + the args
2. **`claude-dejavu doctor` output** — paste the full thing
3. **Recent error log** — `claude-dejavu logs --with-traceback --limit=20`

The error log is at:
- Linux: `~/.local/share/claude-dejavu/errors.log`
- macOS: `~/Library/Application Support/claude-dejavu/errors.log`
- Windows: `%APPDATA%\claude-dejavu\errors.log`

It's append-only JSON Lines, auto-rotated at 5 MB. You can also
attach the file directly to a GitHub issue if it's small enough.

```bash
# Pull the last 20 errors with full tracebacks
claude-dejavu logs --with-traceback --limit=20 > /tmp/dejavu-errors.txt

# Or just print the path so you can attach:
claude-dejavu logs --path
```

Issues: <https://github.com/HthSolid/claude-dejavu/issues>

---

## Diagnostic command cheat sheet

```bash
# Health
claude-dejavu doctor                                # full check
claude-dejavu doctor --json                          # for scripting

# Logs
claude-dejavu logs                                   # last 30 errors
claude-dejavu logs --with-traceback                  # include Python tracebacks
claude-dejavu logs --component=indexer.env           # filter
claude-dejavu logs --path                            # just print log path

# Config
claude-dejavu config list                            # all settings
claude-dejavu config get pg.host                     # specific
claude-dejavu config set lint.mode autofix           # change

# Indexing
claude-dejavu reindex                                # incremental (uses cache)
claude-dejavu reindex --force                        # full re-walk
claude-dejavu symbols --limit=5                      # verify index has data

# Docker (raw)
docker ps | grep claude-dejavu                        # which containers are up
docker logs claude-dejavu-postgres -f                 # live PG logs
docker logs claude-dejavu-weaviate -f                 # live Weaviate logs
docker restart claude-dejavu-postgres                 # last-resort PG kick
```

---

## Still stuck?

1. **Check the error log** — `claude-dejavu logs --with-traceback`.
   The structured records often point directly at the failing
   subsystem (`indexer.env`, `mcp.server`, `rewriter.cascade`, etc.).
2. **Re-run `claude-dejavu doctor`** after every fix — it's the
   fastest way to confirm a step worked.
3. **If install is corrupt**, the nuclear option:
   ```bash
   docker rm -f claude-dejavu-postgres claude-dejavu-weaviate \
       claude-dejavu-transformers
   rm -rf ~/.local/share/claude-dejavu  # Linux/macOS
   # Then re-install via the marketplace or manually.
   ```
   This deletes ALL local dejavu data: the database, the off-tree
   chat-log mirror, the venv, and the error log. Sessions tracked
   so far are unrecoverable after this. Only do it if all else
   fails.
4. **File an issue** with doctor + log output as described above.
