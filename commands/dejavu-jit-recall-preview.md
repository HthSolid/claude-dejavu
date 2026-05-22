---
description: Preview what the UserPromptSubmit JIT recall hook would inject for a given prompt (v0.8.5)
argument-hint: "<prompt>"
---

The user wants to see what the opt-in UserPromptSubmit JIT recall hook
would inject as additional context for a specific prompt, without
flipping `JIT_RECALL_ENABLED` on globally.

Use the bash command:

  claude-dejavu jit-recall-preview $ARGUMENTS

The single positional argument is the prompt to simulate (quote it if
it contains spaces). Runs the hook's recall logic against the live
Weaviate + Postgres without Claude Code in the loop, prints the block
that would be injected to stdout.

## What the hook does

When `JIT_RECALL_ENABLED=true`, every user prompt fans out to a hybrid
Weaviate search (BM25 keyword path — `ClaudeDejavuTurn` ships with
`vectorizer:none`, so caller-side embeds drive hybrid only when the
cloud embed is configured). The top hits' gists are printed to
stdout, where Claude Code picks them up as additional context for the
next turn.

Defensive by design: every failure mode (PG down, Weaviate down, weak
hits, short prompt, unhandled exception, timeout) exits 0 with empty
stdout. A broken hook would break every prompt; that's unacceptable.

In v0.8.6 the hook calls `dejavu_bridge.disclose(hits,
token_budget=JIT_RECALL_TOKEN_CAP)` to greedy-fill hits up to the
budget instead of fixed top-N truncation. Falls back to v0.8.5's
top-N path silently if the bridge call raises.

## Enabling JIT recall

JIT recall is **off** after upgrade. To turn it on, append to
`<DATA_ROOT>/config.env`:

```
JIT_RECALL_ENABLED=true
```

Tuning knobs (all readonly — must be edited in `config.env`
directly):

| Setting | Default | Purpose |
|---|---|---|
| `JIT_RECALL_ENABLED` | `false` | Master switch. |
| `JIT_RECALL_TOP_N` | `3` | Hits to inject (legacy v0.8.5 path). |
| `JIT_RECALL_TOKEN_CAP` | `500` | chars/4 budget (v0.8.6 greedy fill). |
| `JIT_RECALL_SCORE_THRESHOLD` | `2.0` | Silence floor on BM25 (range 1-20+, NOT 0-1 cosine). |
| `JIT_RECALL_MIN_PROMPT_CHARS` | `40` | Skip short prompts. |

Re-tune `JIT_RECALL_SCORE_THRESHOLD` if you see no hits — the v0.8.5
ship value of `0.6` silently rejected every real BM25 hit; `2.0` is
the retuned default (fix 0208d8d).

## Disabling

Remove the `JIT_RECALL_ENABLED` line from `config.env` or set it to
`false`. No restart needed — the hook reads the config on every
prompt.

## Related

- `claude-dejavu search "<query>"` — same backend, manual driver.
- `claude-dejavu memory backfill-gists` — populate `turns.gist` so the
  injected block is the compressed gist, not `content[:200]`.

Exit codes: 0 ok (including silent-zero on every failure mode),
1 reserved for backend failure (hook itself is defensive and never
exits non-zero in production), 2 invalid args.
