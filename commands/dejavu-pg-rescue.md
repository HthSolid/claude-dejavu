---
description: Postgres corruption rescue toolkit — scan, patch-toast, reset-flags, rebuild-table, reindex. Codifies the 2026-05-14 disk-full rescue playbook.
argument-hint: <scan|patch-toast|reset-flags|rebuild-table|reindex> [flags]
---

The user is asking you to diagnose or repair Postgres corruption in
the dejavu database. This toolkit codifies the five rescue scripts
written during the 2026-05-14 → 2026-05-16 lightning + disk-full
incident.

Use the bash command:

  claude-dejavu pg-rescue $ARGUMENTS

Subcommands:

- `scan [--db <name>] [--json]` — walk every row of every dejavu
  table, probe content via `length()` + `substring()`, run index
  validity check (`pg_index.indisvalid`). Reports `corrupt_rows`,
  `corrupt_indexes`, `corrupt_pages`.
- `patch-toast [--db <name>] [--yes] [--from-jsonl]` — for every
  corrupt row reported by `scan`, re-derives the canonical content
  from the source JSONL (using `sessions.project_path` +
  `turns.message_uuid`) and `UPDATE`s with a per-row `SAVEPOINT` so
  one bad row doesn't kill the batch. Default: dry-run; pass `--yes`
  to actually patch.
- `reset-flags [--db <name>] [--yes]` — row-by-row UPDATE that sets
  `turns.vectorized = FALSE, weaviate_id = NULL` with per-row
  savepoints. Used when a bulk UPDATE keeps rolling back because of
  corrupt TOAST values blocking the transaction.
- `rebuild-table <table> [--exclude-ids 119271,119272]
  [--replace-from-jsonl] [--yes]` — filtered index-scan rebuild.
  Drops dependents (FKs + views) discovered at runtime via
  `pg_catalog`, copies all rows except `--exclude-ids` via an index
  scan to a `<name>_new` table, optionally INSERTs JSONL-recovered
  replacements, atomic swap, recreates dependents.
- `reindex [--db <name>] [--all] [--yes]` — wraps
  `REINDEX INDEX CONCURRENTLY` for indexes flagged corrupt by `scan`,
  or every public-schema index when `--all` is set.

Recommended workflow when corruption is suspected:

1. `claude-dejavu pg-rescue scan --json` — see exactly what's corrupt.
2. `claude-dejavu pg-rescue patch-toast` (dry-run first; verify which
   rows can be repaired from JSONL).
3. `claude-dejavu pg-rescue patch-toast --yes` — apply.
4. If indexes are also invalid: `claude-dejavu pg-rescue reindex --yes`.
5. If any row has a corrupt heap tuple header (so UPDATE itself
   errors): `claude-dejavu pg-rescue rebuild-table <table>
   --exclude-ids <bad-ids> --replace-from-jsonl --yes`.

Take a `claude-dejavu session-copy take --include all` snapshot before
any destructive step. Default mode is dry-run; destructive operations
require `--yes`.

Exit codes: 0 ok, 1 generic failure, 2 invalid args, 130 user abort.
