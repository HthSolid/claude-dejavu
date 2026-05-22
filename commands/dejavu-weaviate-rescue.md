---
description: Weaviate corruption rescue toolkit — scan, quarantine-segment, redo-class. Codifies the 2026-05-14 LSM-segment rescue playbook.
argument-hint: <scan|quarantine-segment|redo-class> [flags]
---

The user is asking you to diagnose or repair Weaviate corruption.
This toolkit complements the v0.8.2 `rescue-shard` flow by adding
diagnostics + the surgical "redo class at a new vector dimension"
path.

Use the bash command:

  claude-dejavu weaviate-rescue $ARGUMENTS

Subcommands:

- `scan [--class <C>] [--json]` — probes `/v1/nodes` for unhealthy
  shards, compares the configured cloud embed endpoint's dimension
  against each class's stored vector dim, reports missing canonical
  classes.
- `quarantine-segment <segment-id> --class <C> --shard <S> [--yes]`
  — moves the 5 files of an LSM segment (`.db`, `.bloom`, `.cna`,
  `.secondary.0.bloom`, `.secondary.1.bloom`) into
  `$DATA_ROOT/weaviate-quarantine/<unix-ts>/`. Stops + restarts
  Weaviate around the move. Used as the first step BEFORE
  `rescue-shard` when you've already identified the torn segment.
- `redo-class <class> [--dim <N>] [--vectorizer none|text2vec-transformers]
  [--reset-pg-flags] [--yes]` — DELETE + recreate the class with
  `vectorizer:none` (or your chosen vectorizer). If
  `--reset-pg-flags` and the class is `ClaudeDejavuTurn`, also
  resets `turns.vectorized=FALSE, weaviate_id=NULL` so a subsequent
  `ingester.py --bootstrap` re-embeds at the new dim.

Recommended workflow when the dim mismatch hits (e.g. you switched
from local 384-dim to cloud 1024-dim):

1. `claude-dejavu session-copy take --tag pre-rescue --include all`
2. `claude-dejavu weaviate-rescue scan --json` — confirm the mismatch.
3. `claude-dejavu weaviate-rescue redo-class ClaudeDejavuTurn
    --reset-pg-flags --yes`
4. Run the ingester bootstrap to re-embed at the new dim.

Recommended workflow when a single LSM segment is torn:

1. `claude-dejavu weaviate-rescue quarantine-segment <id>
    --class ClaudeDejavuTurn --shard <shard> --yes`
2. `claude-dejavu rescue-shard --class ClaudeDejavuTurn
    --include-quarantined $DATA_ROOT/weaviate-quarantine/<ts>/<shard>/segment-<id>.db`

Default mode is dry-run; destructive operations require `--yes`.

Exit codes: 0 ok, 1 generic failure, 2 invalid args, 130 user abort.
