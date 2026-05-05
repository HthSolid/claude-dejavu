# claude-dejavu schema migrations

`code/schema.sql` is the **base schema** — applied on every install
with `CREATE TABLE IF NOT EXISTS` semantics. New tables added in
later versions get created here automatically when an existing
install upgrades.

This directory is for changes the base schema **can't** express
idempotently:

- Adding a column to an existing table (`ALTER TABLE … ADD COLUMN …`)
- Renaming a column / table
- Changing a type
- Adding a NOT NULL constraint that requires backfill
- Removing a column (rare; usually we just leave it)
- Index changes that affect performance characteristics

## File naming

`NNNN_short_description.sql` where `NNNN` is a zero-padded sequence
number starting at `0001`. Numbers are monotonically increasing and
never reused.

## Each migration MUST be:

- **Idempotent** — running it twice has the same effect as once.
  Use `IF NOT EXISTS` / `IF EXISTS` / `DO $$ … $$ EXCEPTION` blocks
  to absorb redundant applications safely.
- **Forward-only** — no down-migrations. Rollback by writing a new
  forward migration that undoes the prior one. (Down-migrations
  add complexity and are rarely run; we'd rather keep the audit
  trail simple.)
- **Self-contained** — no `\i` includes, no external file paths.

## How they're applied

`scripts/install.py::_apply_schema_migrations(...)` runs after the
base `schema.sql`. It:

1. Creates `_dejavu_schema_versions(name TEXT PRIMARY KEY,
   applied_at TIMESTAMPTZ)` if missing.
2. Lists every `*.sql` in this directory, sorted by filename.
3. For each, checks `_dejavu_schema_versions` — if absent, applies
   the file with `psql … -f migration.sql` and inserts the
   filename into the versions table on success.
4. Logs each application; failures are surfaced via the standard
   `_fail()` path.

## When to add a migration vs edit `schema.sql`

| Change | Where |
|--------|-------|
| New table | `schema.sql` (CREATE TABLE IF NOT EXISTS) — gets auto-applied |
| New column on existing table | Migration file |
| New index | `schema.sql` (CREATE INDEX IF NOT EXISTS) |
| Rename / type change | Migration file |
| Drop column / table | Migration file (rare) |
| Backfill data | Migration file with explicit transaction |

Always update both the base `schema.sql` (for fresh installs) AND
add a migration (for existing installs being upgraded), so a brand
new install doesn't need to replay every migration in history.

## Future: down-migrations / dry-run

Not implemented today. If they become needed, the entry point will
be `claude-dejavu schema {migrate,rollback,dry-run}` in `recall.py`.
