---
description: Lightweight memory primitives — record-version (v0.8.4) + backfill-gists (v0.8.6)
argument-hint: <record-version|backfill-gists> [flags]
---

The user is asking you to write into the dejavu-owned versions /
memory directory (`DATA_ROOT/versions/`) or to populate the
`turns.gist` column via the closed-source `dejavu_bridge` wheel.

Use the bash command:

  claude-dejavu memory $ARGUMENTS

## Subcommands

### `record-version [--repo PATH] [--memory-dir PATH]`

Distills the most recent CHANGELOG section in `<repo>/CHANGELOG.md`
into a one-line summary (≤200 chars: first meaningful content line +
bracketed `Added/Fixed/Changed` bucket tally) and appends it to
`<memory-dir>/project_<slug>_versions.md`. Idempotent — re-running
on the same head version is a no-op.

Flags:

- `--repo PATH` — repo root containing `CHANGELOG.md` (default: cwd).
- `--memory-dir PATH` — override the versions output directory.
  Default: `DATA_ROOT/versions/` (the dejavu-owned location, NOT
  `~/.claude/projects/.../memory/`, which is Claude Code's territory).

Wiring it up: drop a `.git/hooks/post-commit` in each repo whose
releases you want to surface so every `chore(release): vX.Y.Z` commit
auto-records:

```bash
#!/bin/sh
git log -1 --pretty=%s | grep -qE '^chore\(release\):' && \
  claude-dejavu memory record-version --repo "$(git rev-parse --show-toplevel)" \
  || true
```

The cross-project SessionStart digest hook (`hooks/session_start_digest.py`)
reads `DATA_ROOT/versions/project_*_versions.md` and emits up to ~30
lines of release one-liners as `systemMessage` (last 3 versions per
project, 20 projects max, hard-capped at 6000 chars / ~1500 tokens).
Honors `INJECT_SESSION_START=false`.

### `backfill-gists [--limit N] [--workers M] [--dry-run] [--force]`

Walks every turn with `gist IS NULL AND content IS NOT NULL` and
writes a compressed load-bearing-anchor-preserving gist via the
closed-source `dejavu_bridge` wheel (or the in-tree stub fallback
when the wheel is unavailable — see STANDARD MODE below). Tag:
`gist_method='claudedj-distill-v1'`.

Flags:

- `--limit N` — cap on rows to process (default: all).
- `--workers M` — parallel workers (default: `4`). Each gets its
  own psycopg2 connection.
- `--dry-run` — compute gists but don't write to DB. Prints the
  `distill / skip / error` counts so you can size the real run.
- `--force` — re-distill rows that already have a `gist`
  (default behavior is idempotent skip).

Uncompressible turns store `gist=NULL` rather than the legacy
first-80-chars fallback; recall falls back to `content[:200]` for
those rows. Skip-types (code blocks, tracebacks, JSON, diffs,
`tool_use` payloads) always return `gist=NULL` by design.

STANDARD MODE: if `dejavu_bridge` isn't installed,
`code/dejavu_bridge_stub.py` activates automatically — naive
deterministic Python that re-exposes the same six primitives so the
suite passes and recall keeps working, but gist quality drops to
"first-paragraph-ish". SessionStart logs which mode is active. Install
the wheel by setting `DEJAVU_BRIDGE_INDEX_URL` to a directory or URL
containing the wheel artifacts and re-running `scripts/install.py`.

## Related

- `claude-dejavu what-shipped` — print the recent CHANGELOG headlines
  without writing anything.
- `claude-dejavu search` — gist-first response shape (v0.8.6); use
  `--full` for the legacy content-dump shape.
- `claude-dejavu expand <turn_id>` — fetch one row's full content
  (gist-aware ergonomic for follow-up).

Exit codes: 0 ok, 1 generic failure, 2 invalid args.
