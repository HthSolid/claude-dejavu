---
description: Check or browse database schema (Prisma / Drizzle / TypeORM / SQL) — catches typos and fabricated table/column references
argument-hint: <model.field>  |  <model>  |  --models  |  --fields=MODEL
---

Use the claude-dejavu plugin to either:

1. **Check before referencing a model.field** — `dejavu_check_db_field(target)`.
   USE THIS BEFORE writing `prisma.<model>.<op>({ where|select|data: { … } })`,
   `db.select().from(<schema>)`, or raw SQL.

   Match kinds returned:
   - ✓ exact — model + field both exist; safe.
   - ≈ cross-model — field exists but on a DIFFERENT model than asked.
     Either query the actual model, or add the field to the queried one.
   - ~ fuzzy — close match (typo); rename to the candidate.
   - no match — fabricated; pick the right one OR declare it.

2. **Browse the model surface** — `dejavu_db_models(source?)`.
   Survey what's declared across Prisma schema, Drizzle table consts,
   TypeORM @Entity classes, and SQL CREATE TABLE migrations.

3. **Browse fields on one model** — `dejavu_db_fields(model)`.
   Check exactly what keys a `where` / `select` / `data` block can use.

Args: $ARGUMENTS

This is the DB-grounding companion to `/dejavu-routes` (HTTP API),
`/dejavu-pages` (frontend routes), and `/dejavu-env` (env vars). The four
together ensure Claude doesn't fabricate names that the project doesn't
actually expose.
