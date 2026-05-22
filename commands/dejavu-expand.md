---
description: Fetch the full content of one or more turns by id — gist-aware counterpart to search (v0.8.6 default shape)
argument-hint: <turn_id> [<turn_id> ...]
---

The user wants to pull the full content of specific turn rows. As of
v0.8.6, `claude-dejavu search` returns the compressed gist by default
(or `content[:200]` for rows without a gist); `expand` is the
canonical way to ask for one row's complete content when the gist
isn't enough.

Use the bash command:

  claude-dejavu expand $ARGUMENTS

`$ARGUMENTS` is a whitespace-separated list of one or more
`turn_id`s (positive integers). Output is one full-content block per
id, separated by a header line.

## Why expand exists

`search` ships a token-cheap shape:

```
{session_id, turn_id, gist (or content[:200]), anchors[], score, ts}
```

The leading `[anchors: …]` block is stripped from `gist` for display
and the anchor list surfaces on its own line. This keeps the search
result tight enough to inject into context without burning budget.

When you actually need the full content of one of those hits — to
read the file diff verbatim, replay a Bash failure, etc. — call
`expand <turn_id>` for just that row. The whole-corpus content lives
in Postgres; expansion is a single indexed lookup.

The alternative, `claude-dejavu search "…" --full` (or `--mode full`),
returns the legacy content-dump shape for every hit, which is wasteful
when you only need one of them.

## Examples

```bash
# Expand a single turn
claude-dejavu expand 12345

# Expand multiple turns in one shot (useful when search returned a
# couple of related hits)
claude-dejavu expand 12345 12346 12399
```

## Related

- `claude-dejavu search "<query>" [--full]` — gist-first by default;
  `--full` switches to the legacy whole-content shape per hit.
- `claude-dejavu memory backfill-gists` — populate `turns.gist` so the
  search shape is the compressed gist instead of `content[:200]`.

Exit codes: 0 ok, 1 turn not found / DB error, 2 invalid args.
