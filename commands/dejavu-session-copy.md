---
description: Manage the restic-backed session-copy subsystem (v0.8.0)
---

# /dejavu-session-copy

Runs `claude-dejavu session-copy $ARGUMENTS`.

The v0.8.0 session-copy subsystem replaces the legacy rsync chain with a
deduplicated, encrypted, compressed restic repo.

## Usage

```bash
# One-time setup (generates passphrase, inits repo, installs scheduler)
/dejavu-session-copy init

# Take an ad-hoc snapshot
/dejavu-session-copy take --tag manual --message "before refactor"

# List snapshots
/dejavu-session-copy list --last 20
/dejavu-session-copy list --tag stop-hook

# Diff two snapshots
/dejavu-session-copy diff <id-a> <id-b>

# Restore a snapshot to a target dir
/dejavu-session-copy restore <id> --target /tmp/restored

# Mount the repo as FUSE (Linux native; macOS needs macFUSE; Windows needs WinFsp)
/dejavu-session-copy mount /tmp/snaps

# Apply retention policy
/dejavu-session-copy prune --dry-run
/dejavu-session-copy prune

# Health: size, snapshot count, last-snapshot age
/dejavu-session-copy status

# Audit (24h-cached metadata check by default; `--read-data` for full audit)
/dejavu-session-copy check

# Migrate from legacy rsync chain (auto-invoked by install.py [7/9])
/dejavu-session-copy migrate-from-rsync --mode=dry-run
/dejavu-session-copy migrate-from-rsync --mode=cold

# Reclaim the legacy .deleteme tree once the new repo is verified (v0.8.3)
/dejavu-session-copy reclaim-legacy --dry-run
/dejavu-session-copy reclaim-legacy --yes
```

## `reclaim-legacy` (v0.8.3)

After `migrate-from-rsync` finishes it leaves the old
`~/.claude-backups/snapshots/` tree renamed to
`~/.claude-backups/snapshots.deleteme.<unix-ts>/` as a safety net.
Once you've verified the new restic repo has everything you need
(`session-copy list`, `session-copy diff`, `session-copy restore` on
a known snapshot), `reclaim-legacy` deletes the `.deleteme.*` tree
to reclaim disk.

Flags:

- `--dry-run` — print the plan (path + size) and exit without
  deleting.
- `--yes` — skip the TTY-guarded typed-confirmation phrase.

The 2026-05-15 incident left a 513 GB legacy tree on one host; running
`reclaim-legacy` after verifying the restic repo brought that back to
the ~5-10 GB the new policy targets.

## Includes (v0.8.3)

`session-copy take` accepts `--include sessions,sqlite,pg,weaviate`
(or `--include all`, the default). `pg` runs `pg_dump --format=custom`
(via docker-exec preferred, host fallback); `weaviate` snapshots the
volume as a tarball via a throwaway alpine container, with a Weaviate
restart in a `finally` block.

`session-copy restore <id> --include {…} [--force]` is the symmetric
restore. `pg` runs `pg_restore --clean --if-exists`; `weaviate`
extracts the tarball over the volume. Both refuse to clobber a
populated target without `--force`.

See spec §3.3 in `2026-05-15-snapshot-subsystem-design.md` for the full
command surface and per-flag semantics.
