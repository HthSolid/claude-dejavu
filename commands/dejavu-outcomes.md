---
description: Inspect correction_outcomes — what corrections did dejavu suggest, and what did the user accept
argument-hint: [--project|--global] [--language=LANG] [--mode=MODE] [--limit=N]
---

Run the `claude-dejavu outcomes` CLI to inspect the lint/lookup outcome
log. This is the data the v0.3.3 adaptive-threshold learner will consume,
and the v0.3.4 federated-improvement opt-in will (anonymously) share.

Args: $ARGUMENTS

```bash
claude-dejavu outcomes $ARGUMENTS
```

Show the user a compact table of recent outcomes:
  ts | mode | proposed_name → resolved_name | edit_distance | trigram_sim | outcome | pattern_tags

The `mode` column tells which path produced the row:
  - `lookup` — user / Claude called dejavu_resolve_symbol or the CLI
  - `lint`   — dejavu_lint_proposed_edit checked an edit
  - `warn` / `suggest` / `autofix` — PreToolUse intervention modes (v0.3.2+)

Useful for: "did my last lint round actually catch anything?",
"what naming patterns am I drifting on?", or just curiosity.
