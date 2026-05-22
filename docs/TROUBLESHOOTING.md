# Troubleshooting

If something went wrong during install or a tool isn't working, this
is the page. Most issues are one of: Docker not running, port
conflict, stale install path, or a backgrounded service that didn't
come up cleanly. **Always start with `/dejavu-doctor` —** it
auto-diagnoses everything below and tells you exactly what to fix.

```bash
# Inside Claude Code:
/claude-dejavu:dejavu-doctor

# Or from a terminal:
claude-dejavu doctor
```

The output is self-explanatory: each check reports `ok` / `warn` /
`fail`, and failed checks include a `→ fix:` line you can copy-paste.

---

Make sure to run the installation script: 
```bash
python C:\Users\<your_user>\.claude\plugins\cache\claude-dejavu\claude-dejavu\<latest_version>\scripts\install.py --auto"
```

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

### "Sessions show as missing from the index"

Symptom: `dejavu_search` doesn't return any hits from a recent session, or
`claude-dejavu doctor` shows `ingest_coverage: warn — N never ingested`.

This used to happen silently when an ingester crashed mid-session. As of
v0.5.0+ ingest is durably-resumable and crash-resilient (per-session
lock, NUL-byte sanitization, periodic cursor commits), but historical
gaps from earlier versions still need a one-shot recovery.

```bash
# Recover all sessions that exist on disk but aren't in the index.
# Runs in parallel — pick `--workers` to match your CPU/IO budget.
claude-dejavu reingest --missing-only --workers 4

# Or re-ingest one specific session (clears its cursor first):
claude-dejavu reingest --session <session-uuid>

# Full re-scan (clears ALL cursors, walks every jsonl from scratch):
claude-dejavu reingest
```

Each run prints a per-session progress heartbeat to stderr every ~250
turns so you can see forward motion without polling Postgres.

### "I edited the plugin source but my changes don't take effect"

Claude Code doesn't run hooks from your working tree — it runs them
from `~/.claude/plugins/cache/claude-dejavu/<version>/`. After editing
the plugin source:

```bash
# 1. Update the marketplace clone (Linux/macOS):
cd ~/.claude/plugins/marketplaces/claude-dejavu
git fetch origin main && git reset --hard origin/main

# 2. Re-run install.py from the updated clone:
python ~/.claude/plugins/marketplaces/claude-dejavu/scripts/install.py

# 3. Restart Claude Code so it re-reads the plugin manifest.
```

Or for development against a *local* working tree (not the marketplace
clone), set `CLAUDE_PLUGIN_ROOT_OVERRIDE=<your-path>` in your shell
before launching Claude Code, and the hooks resolve from there.

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

### "`pg-rescue scan` reports `corrupt_rows`"

`claude-dejavu pg-rescue scan` walks every row of `turns`,
`tool_calls`, and `observations`, probing content via `length()` +
`substring()`. Any row whose probe raises a TOAST decompression error
or returns NULL-where-NOT-NULL is reported as `corrupt_rows`.

The standard remediation is `patch-toast`, which re-derives the
canonical content from the source JSONL (using `sessions.project_path`
+ `turns.message_uuid`) and `UPDATE`s with a per-row `SAVEPOINT` so one
bad row doesn't kill the batch.

```bash
# Take a snapshot first — destructive operations require it.
claude-dejavu session-copy take --tag pre-pg-rescue --include all

# See exactly what's broken
claude-dejavu pg-rescue scan --json

# Dry-run patch (default)
claude-dejavu pg-rescue patch-toast

# Apply
claude-dejavu pg-rescue patch-toast --yes

# If indexes are also invalid
claude-dejavu pg-rescue reindex --yes
```

If `UPDATE` itself errors on a row (corrupt heap tuple header — torn
xmin/xmax), use `rebuild-table` instead:

```bash
claude-dejavu pg-rescue rebuild-table turns \
    --exclude-ids 119271,119272 \
    --replace-from-jsonl --yes
```

This does a filtered index-scan rebuild: drops dependents (FKs + views
discovered at runtime via `pg_catalog`), copies all rows except the
excluded ids, optionally INSERTs JSONL-recovered replacements, atomic
swap, recreates dependents.

### "`pg-rescue scan` reports `schema_mismatches`"

This isn't corruption — it's drift detection. The scanner validates
every probe column against `information_schema.columns` before
probing. When a column the scanner expects (e.g. `tool_calls.input`)
doesn't match the live schema (the column is actually `tool_input`,
JSONB), the mismatch is surfaced under `schema_mismatches` instead of
being counted as a corrupt row.

If you see this, your dejavu schema has drifted from what
`code/pg_rescue.py` expects. Likely causes:

- You're on a forked schema (patched the .sql by hand).
- A migration ran but the rescue toolkit hasn't been updated yet.

If you're on `main` and seeing mismatches, that's a bug — file an
issue with the `--json` output. The v0.8.3 release of `pg-rescue`
fixed three drift cases (`tool_calls.input` → `tool_input`,
`tool_calls.result` → `tool_output`, `observations.observation` →
`title`/`subtitle`), so a stale plugin cache pre-0.8.3 is the most
likely cause.

### "`weaviate-rescue scan` reports shard issues"

`claude-dejavu weaviate-rescue scan` probes `/v1/nodes`, `/v1/schema`,
and vector-dimension consistency between the configured cloud
embedding endpoint and each class's stored vectors. Common findings:

| Finding | Remediation |
|---|---|
| Shard not in `READY` / `INDEXING` state | `claude-dejavu rescue-shard --class <C>` (v0.8.2 — Go segment reader + class reset + gap-reindex). Take a `session-copy take --include all` first. |
| Vector-dim mismatch (e.g. configured 1024-dim cloud embed, existing class is 384-dim) | `claude-dejavu weaviate-rescue redo-class ClaudeDejavuTurn --reset-pg-flags --yes` then bootstrap the ingester to re-embed at the new dim. |
| LSM segment file corrupted | `claude-dejavu weaviate-rescue quarantine-segment <id> --class <C> --shard <S> --yes` moves the 5 files (`.db`, `.bloom`, `.cna`, `.secondary.0.bloom`, `.secondary.1.bloom`) into `$DATA_ROOT/weaviate-quarantine/<ts>/`. Stops + restarts Weaviate around the move. Follow with `rescue-shard` to recover records from the quarantined segment without re-embedding. |
| Missing canonical class | re-run `scripts/install.py` to recreate `ClaudeDejavuTurn` + `ClaudeDejavuDoc`. |

### "`doctor --deep` reports WARN / FAIL"

`claude-dejavu doctor --deep` (v0.8.3) extends the standard health
check with `pg-rescue scan` + `weaviate-rescue scan`. It's opt-in
because both scans are slow on large DBs (full row-by-row probe).

Each surfaced issue includes the **exact remediation command** to
paste. Read the `→ fix:` line — that's the single-line fix path.

`WARN` is "you can keep using dejavu but should fix this soon";
`FAIL` is "you have data corruption / unreachable backend, do the fix
before depending on results". Both are non-blocking — `doctor` itself
exits 0 either way; check the body, not the exit code.

### "`dejavu_bridge unavailable; running in standard mode`"

The closed-source `dejavu_bridge` Python wheel isn't installed. This is
expected when `DEJAVU_BRIDGE_INDEX_URL` wasn't set at install time.
You get an in-tree Python stub that re-exposes the same six retrieval
primitives with naive deterministic semantics — the full test suite
passes against the stub, and recall keeps working.

What you lose in STANDARD MODE:

- Gist quality drops from anchor-preserving compression to
  first-paragraph-ish.
- `claude-dejavu search` falls back to `content[:200]` for any row
  where `gist IS NULL` — fine, but uses more tokens.
- JIT recall (`hooks/user_prompt_submit.py`) falls back from greedy
  token-budget fill to fixed top-N truncation.

To install the wheel:

```bash
# Point at a directory / URL with the wheel artifact
export DEJAVU_BRIDGE_INDEX_URL=file:///path/to/wheels/

# Re-run the installer (idempotent — only step [4/9] actually does work)
python3 scripts/install.py --auto
```

Confirm with `claude-dejavu doctor` (the bridge mode shows up under
the venv check) and the SessionStart stderr line on next launch.

### "JIT recall returns nothing"

If `claude-dejavu jit-recall-preview "<prompt>"` prints empty output:

1. **Check the prompt is long enough.** `JIT_RECALL_MIN_PROMPT_CHARS`
   (default `40`) silently skips short prompts.
2. **Check the BM25 score threshold.** `JIT_RECALL_SCORE_THRESHOLD`
   defaults to `2.0` on BM25's 1-20+ range (NOT 0-1 cosine). If you
   manually set it to `0.6` (cosine-flavored), every real hit is
   silently rejected — that's exactly the bug the v0.8.5 retune
   fixed. Reset to `2.0` (or remove the line).
3. **Check past sessions have data on the topic.** JIT recall searches
   the live `ClaudeDejavuTurn` corpus via BM25 keyword extraction. If
   the topic genuinely doesn't appear in past sessions, no hits is
   correct.
4. **Check the backend.** `claude-dejavu doctor` must report PG + WV
   green; the hook is defensive and silent-zeros on backend failure.

### "`migrate-from-rsync` crashed mid-flight"

The migration is **idempotent + resumable** via a phase marker at
`$DATA_ROOT/migration-state.json`. Re-run `scripts/install.py` (or
the underlying `claude-dejavu session-copy migrate-from-rsync`
command) and it picks up at the last completed phase.

The 10 phases: `stop legacy → inventory → preserve → init repo →
initial snapshot → verify → install scheduler disabled → atomic
rename → enable scheduler → done`. Per-step rollback contracts mean
a crash in any phase leaves the system in a known state.

`hooks/session_start.py` surfaces a migration-pending notice when the
state file exists with `phase != 'done'`, so you'll see a warning in
chat next time you launch Claude Code.

### "I have a 513 GB `.deleteme` tree eating my disk"

That's the `migrate-from-rsync` safety net — the old
`~/.claude-backups/snapshots/` chain renamed to
`~/.claude-backups/snapshots.deleteme.<ts>/` so you can roll back if
the new restic repo turns out wrong.

Once you've verified the restic repo (`claude-dejavu session-copy
list`, `… diff`, `… restore` on a known snapshot to a temp dir):

```bash
# Preview (size + path)
claude-dejavu session-copy reclaim-legacy --dry-run

# Apply (TTY-guarded + typed confirmation phrase; --yes skips the typing)
claude-dejavu session-copy reclaim-legacy --yes
```

The 2026-05-15 incident that motivated v0.8.0 had 5,057 unbounded rsync
snapshots × ~100 MB each = 508 GB on a single root partition.
`reclaim-legacy` brings that back to the ~5-10 GB the new policy
targets (24 hourly + 7 daily + 13 weekly retention, content-addressed
dedup, zstd compression).

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
claude-dejavu doctor --deep                          # + pg-rescue scan + weaviate-rescue scan (slow)
claude-dejavu doctor --json                          # for scripting

# Rescue toolkits (v0.8.2 + v0.8.3)
claude-dejavu pg-rescue scan --json                  # PG corruption diagnostic
claude-dejavu pg-rescue patch-toast --yes            # repair from JSONL
claude-dejavu pg-rescue rebuild-table <t> --yes      # heap rebuild
claude-dejavu weaviate-rescue scan --json            # WV shard + dim diagnostic
claude-dejavu rescue-shard --class ClaudeDejavuTurn  # recover from torn LSM segments

# Session-copy (v0.8.0 — restic-backed)
claude-dejavu session-copy take --tag manual         # ad-hoc snapshot
claude-dejavu session-copy list --last 20            # what's restorable
claude-dejavu session-copy status                    # size + last-snapshot age
claude-dejavu session-copy reclaim-legacy --yes      # delete legacy .deleteme tree

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
