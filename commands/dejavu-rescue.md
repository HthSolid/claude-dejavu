---
description: Rescue a corrupted Weaviate shard by walking its LSM segments, recreating the class, restoring records, and gap-reindexing from Postgres canonical
argument-hint: --class <C> [--shard <id>] [--dry-run] [--yes] [--include-quarantined <path>]
---

The user is asking you to rescue a Weaviate class whose on-disk shard
has a torn / truncated LSM segment that prevents the class from loading.

Use the bash command:

  claude-dejavu rescue-shard $ARGUMENTS

Required:
- `--class <C>` — class name (e.g. `ClaudeDejavuTurn`)

Common:
- `--dry-run` — walk + report what WOULD happen. No DELETE, no batch
  insert, no gap-reindex. Safe to run anytime.
- `--shard <id>` — restrict to one shard id (default: all shards under
  the class).
- `--yes` — skip the typed-confirmation prompt before the destructive
  phase. The default flow requires typing `yes` interactively.
- `--include-quarantined <path>` — extra `.db` segment to walk alongside
  the live shard (e.g. a file you preserved before Weaviate stopped
  loading it).

Phases (destructive on apply, in this order):
1. **Walk** — Go binary `dejavu-rescue scan-shard` extracts records
   from every segment, with truncation tolerance for torn writes.
2. **Reset class** — `DELETE /v1/schema/<C>` then `POST /v1/schema` from
   the canonical class definition in `code/weaviate-class.json`.
3. **Restore** — batch-insert recovered records (with their original
   vectors) via `/v1/batch/objects`.
4. **Gap-reindex** — diff recovered UUIDs against the Postgres
   `turns` canonical set; re-embed any missing entries.

Recommended workflow:
1. Run with `--dry-run` first to confirm record counts look sane.
2. Make a `claude-dejavu session-copy take --tag pre-rescue` snapshot
   for rollback safety.
3. Run for real (omit `--dry-run`, add `--yes` when scripting).

Exit codes: 0 ok, 1 generic failure, 2 invalid args, 3 binary missing,
130 user abort.
