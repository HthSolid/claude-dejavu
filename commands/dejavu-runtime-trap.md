---
description: Generate a typed env accessor that throws on undeclared keys at runtime — TypeScript or Python
argument-hint: [--language=ts|python] [--output=PATH]
---

Generate a runtime defense layer that wraps `process.env` /
`os.environ` with a Proxy/dataclass throwing `DejavuUndeclaredEnvError`
on undeclared key access. Complements the gen-time rewriter — anything
that escaped the cascade still gets caught at first execution with a
clear, dejavu-branded error.

Two language outputs:

  - **TypeScript** (default for Node / Next / Vite projects)
    Generates `src/lib/env.ts` (configurable via `--output=`).
    Uses Zod for schema validation + Proxy for declared-key checks.
    The `DeclaredEnvKey` literal-type union means tsc itself flags
    `env.UNDECLARED` in your IDE.

  - **Python**
    Generates `src/lib/dejavu_env.py`. Uses a frozenset of declared
    keys + `__getattr__` / `__getitem__` on a wrapper class.
    `DejavuUndeclaredEnvError` is a `KeyError` subclass.

Reads declared keys from BOTH:
  - `.env.example` (or whatever you pass via `--from-file=`)
  - the indexed `env_vars` table (fed by `claude-dejavu reindex`,
    which walks `.github/workflows/`, `docker-compose.*`,
    `next.config.*`, `vercel.json`, plus AST scan of source code)

Sample:

  /dejavu-runtime-trap                       # TS to src/lib/env.ts
  /dejavu-runtime-trap --language=python     # Python to src/lib/dejavu_env.py
  /dejavu-runtime-trap --output=lib/env.ts   # custom path

After adding new env vars (`/dejavu-env propose ...`), regenerate
this file to refresh the declared-key set.

Full usage in `docs/RUNTIME_TRAP.md`.

Args: $ARGUMENTS
