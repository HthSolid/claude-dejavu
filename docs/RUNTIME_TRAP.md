# Runtime env trap

A small generated module that wraps env access and throws on
undeclared keys. Complements the gen-time rewriter — anything that
escaped the cascade still gets caught at first execution with a
clear, dejavu-branded error.

## Why bother

Without the trap:

```ts
const stripeKey = process.env.STRIPE_SECRT_KEY;  // typo
// stripeKey is undefined; later:
const stripe = new Stripe(stripeKey);            // crashes deep in
                                                  // Stripe SDK with
                                                  // a confusing
                                                  // "Invalid API key"
```

With the trap:

```ts
import { env } from "./src/lib/env";

const stripeKey = env.STRIPE_SECRT_KEY;
// throws immediately:
// [claude-dejavu] env.STRIPE_SECRT_KEY is not declared in this project.
// Declared keys: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, ...
// If this is a real SDK convention, add it via
// `claude-dejavu env propose STRIPE_SECRT_KEY --apply`
// then regenerate this file.
```

## Generate

```bash
# TypeScript (default — for Node / Next / Vite projects)
claude-dejavu generate runtime-trap

# Python
claude-dejavu generate runtime-trap --language=python

# Custom output path
claude-dejavu generate runtime-trap --output=src/config/env.ts
```

The generator reads declared keys from BOTH:
- the project's `.env.example` (or whatever you pass via `--from-file`)
- the indexed `env_vars` table (fed by reindex's env-var indexer
  which walks `.github/workflows/`, `docker-compose.*`, `next.config.*`,
  source AST, etc.)

Anything either source declares is allowed at runtime.

## TypeScript form

`src/lib/env.ts` (default location):

```ts
import { z } from "zod";

const declaredKeys = ["DATABASE_URL", "STRIPE_SECRET_KEY", ...] as const;
export type DeclaredEnvKey = "DATABASE_URL" | "STRIPE_SECRET_KEY" | ...;

const envSchema = z.object({
  DATABASE_URL: z.string().min(1).optional(),
  STRIPE_SECRET_KEY: z.string().min(1).optional(),
  // ...
});
const parsed = envSchema.safeParse(process.env);
// fails loudly at boot if schema validation catches a wrong type

export const env = new Proxy(parsed.data, {
  get(target, prop) {
    if (!declaredKeys.includes(prop as any)) {
      throw new Error(`[claude-dejavu] env.${prop} not declared...`);
    }
    return target[prop];
  },
});
```

Two layers of defense:

1. **Compile time**: the `DeclaredEnvKey` union type means tsc flags
   `env.UNDECLARED_KEY` in your IDE before runtime, IF you type the
   accessor argument. Useful if you're writing `function getEnv(k:
   DeclaredEnvKey)`.

2. **Runtime**: the Proxy throws on every undeclared access path,
   regardless of how it was reached (dynamic property access,
   typed-around code paths, etc.).

Requires `zod` as a dependency. The generated file imports
`from "zod"` — install with `npm install zod` or `pnpm add zod`.

## Python form

`src/lib/dejavu_env.py` (default location):

```py
_DECLARED_ENV_KEYS = frozenset({"DATABASE_URL", "STRIPE_SECRET_KEY", ...})

class DejavuUndeclaredEnvError(KeyError):
    """Raised when code tries to read an undeclared env var."""

class _EnvProxy:
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def __getitem__(self, name):
        if name not in _DECLARED_ENV_KEYS:
            raise DejavuUndeclaredEnvError(...)
        return os.environ.get(name)

    def __contains__(self, name):
        return name in _DECLARED_ENV_KEYS

env = _EnvProxy()
```

Use it like:

```py
from dejavu_env import env, DejavuUndeclaredEnvError

url = env.DATABASE_URL          # returns the string or None
url2 = env["DATABASE_URL"]      # equivalent

if "STRIPE_SECRET_KEY" in env:  # declared-set check
    ...

# If a name is undeclared, both styles raise:
env.MADE_UP_KEY                 # raises DejavuUndeclaredEnvError
env["FAKE_KEY"]                 # raises DejavuUndeclaredEnvError
```

`DejavuUndeclaredEnvError` is a subclass of `KeyError`, so
`except KeyError:` catches it for code paths that already handle
missing-key errors gracefully. Catch the dejavu-specific class
when you want to surface the branded error message to the user.

## Refreshing after adding a key

Whenever you add a new declared key — via `claude-dejavu env propose`
or by hand-editing `.env.example` — regenerate the trap:

```bash
claude-dejavu env propose NEW_API_KEY --package=somepkg --apply
claude-dejavu generate runtime-trap --language=typescript
```

The generated file is auto-overwritten each run. Don't hand-edit
it; the file's header explicitly says so.

## Adoption checklist

Adopting the trap incrementally:

1. **Generate the file** in a non-blocking location:
   `claude-dejavu generate runtime-trap --output=src/lib/env.ts`

2. **Migrate one boundary at a time** — replace `process.env.X`
   with `env.X` in a single module, run tests, ship.

3. **Catch the error explicitly** in your boot path so users get
   the dejavu message instead of a stack trace from an
   uninitialized SDK:

   ```ts
   try {
     await bootApp();
   } catch (e) {
     if (e.message?.startsWith("[claude-dejavu]")) {
       console.error(e.message);
       process.exit(1);
     }
     throw e;
   }
   ```

4. **CI gate**: add a smoke test that imports your `env` module
   and verifies it loads. Catches `.env.example` drift before it
   reaches production.

## What it doesn't do

- It does NOT validate values. A `STRIPE_SECRET_KEY` set to
  `"placeholder"` still loads. Pair it with the existing Zod
  schema for type/format checks.
- It does NOT load `.env` files at runtime. Use `dotenv` /
  `process.loadEnvFile()` upstream as you would normally.
- It does NOT replace your secrets manager. The trap constrains
  KEYS, not VALUES.
