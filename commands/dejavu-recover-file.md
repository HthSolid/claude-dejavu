---
description: Reconstruct a corrupted file from a file-history snapshot + replay of session Edit/Write tool calls
argument-hint: <path> [--session <uuid>] [--any-project] [--from-version <N> | --from-git <ref>] [--to <ts>] [--write [<out>]] [--yes] [--verify-tsc] [--skip-failed-edits] [--force-write]
---

The user is asking you to reconstruct a file that was corrupted (zeroed,
partial-zero, shrunk) or otherwise damaged. The recovery combines:

  1. A **base** file content from one of four sources, in order:
     a. File-history snapshot (`~/.claude/file-history/<session>/<hash>@vN`)
     b. DB-tracked Edit chain + `git show HEAD:<path>` as the base
     c. Git HEAD only (warning: uncommitted edits cannot replay)
     d. Write-only: file was created during the session, no prior base
  2. **Replay** of every Edit / MultiEdit / Write `tool_use` on that
     path in the session JSONL since the snapshot.

Use the bash command:

  claude-dejavu recover-file $ARGUMENTS

**Default mode is dry-run.** It prints:
- The chosen base source + ops collected count
- Diff stat (`+N -M lines`) vs the current on-disk file
- The first 20 lines of the unified diff

Pass `--write` to apply. When writing back to the **original** path you
must also pass `--yes` (confirmation gate). `--write <out-path>` redirects
to a different file (no confirmation required, since the original is
preserved).

Safety:
- A copy of the current file is always backed up to
  `$DATA_ROOT/recovered-files/<session>/<unix-ts>/<path-hash>.original`
  BEFORE the atomic temp-then-replace write.
- The temp file lives **next to the target** (`<target>.dejavu-recover.tmp`)
  to keep `os.replace()` atomic on a single filesystem.
- Editor lockfiles (`.<name>.swp`, `.<name>.swo`, `<name>~`) adjacent to the
  target cause the write to abort with a clear error unless `--force-write`
  is passed.
- Refuses to operate on targets outside `$HOME` without `--allow-anywhere`.

Pinning the base:
- `--from-version <N>` — use a specific file-history `@vN` snapshot.
- `--from-git <ref>`   — use `git show <ref>:<path>` as the base.

Flow control:
- `--to <iso-ts>` truncates ops to those with `ts <= <iso-ts>`.
- `--skip-failed-edits` keeps going past `old_string`-not-found mismatches
  (logged as warnings). Without it, an unmatched Edit aborts the recovery.
- `--verify-tsc` runs `tsc --noEmit` on the candidate (advisory; failure
  does not block the write).

Exit codes: 0 success / dry-run, 4 outside $HOME, 5 base pin failed,
6 apply failed, 7 missing --yes, 8 editor lockfile present,
9 backup failed, 10 write failed.
