---
description: Check or list environment variable declarations — catches fabricated names + typos in process.env / os.getenv reads
argument-hint: <var-name>  |  --list  [--source=NAME] [--secrets-only]
---

Use the claude-dejavu plugin to either:

1. **Check before reading an env var** — `dejavu_check_env_var(name)`.
   USE THIS BEFORE writing `process.env.X` / `os.getenv("X")` /
   `Deno.env.get("X")` / `import.meta.env.X`. Returns whether the name
   is declared canonically (`.env.example`, zod/envalid schema,
   docker-compose, GitHub Actions) or only read elsewhere in source.

   - Exact match in a canonical source → safe to use.
   - Exact match only via source-read tier → undeclared but used
     elsewhere; add to `.env.example` or check it's not a typo.
   - Fuzzy match → likely typo; rename to the canonical candidate.
   - No match → fabricated; pick the correct name OR declare it.

2. **Browse the env surface** — `dejavu_env_vars(source?, secrets_only?)`.
   Use to survey what env vars exist before proposing new ones, or to
   find the canonical name when the user describes a var by intent.

3. **Propose a new env var declaration** —
   `dejavu_propose_env_addition(name, package?, source_file?, apply?)`.
   Use when an SDK convention is needed but not yet declared in
   `.env.example` (e.g. `LAUNCHDARKLY_SDK_KEY` because the project
   depends on `@launchdarkly/...`). Returns a unified-diff preview by
   default; pass `apply=true` to write the change. Adds an inline
   comment naming the SDK package for self-documenting history.

Args: $ARGUMENTS

This is the env-grounding companion to `/dejavu-routes` (HTTP API
endpoints) and `/dejavu-pages` (frontend route files). Together they
ensure Claude doesn't fabricate identifiers that the project doesn't
actually expose.
