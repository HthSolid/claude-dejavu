# claude-dejavu rewriter — architecture and usage

The rewriter is the v0.5.0a feature that takes the bench's
hallucination rate from 7.5% (raw tool-use mode) to **0.5%** without
re-prompting the LLM. It runs after the model emits a proposed code
edit, identifies every name that doesn't match the project's
manifest, and tries to resolve each via a deterministic 14-strategy
cascade.

## Pipeline

```
Model emits proposed code (raw)
       │
       ▼
Scorer parses every grounded reference
   - process.env.X
   - Prisma model.field calls
   - fetch / axios URLs
   - gql template literals
   - feature-flag isEnabled() calls
       │
       ▼
For each name not exactly in the manifest:
   Run domain-specific strategy stack (see below)
   Each strategy returns (verdict, confidence, replacement?)
   Highest-priority verdict wins (ties broken by confidence)
       │
       ▼
Apply outcomes:
   - REPLACE     → word-boundary string substitution applied
   - ACCEPT_SDK  → manifest extended; not counted as halluc
   - RESIDUAL    → reported back; never auto-modified

Optional final stage (opt-in):
   Batched LLM clarification turn for residuals (Haiku tier,
   cost-capped at one call per response)
```

The cascade is the source of truth — the LLM is never re-prompted to
"please rewrite the code." Per project rule: deterministic + tooling
beats prose instruction every time.

## The 14 strategies

Listed in the order the env stack runs them. Per-domain stacks
include a subset (see `code/rewriter.py::STRATEGY_STACKS`).

| # | Strategy | What it catches |
|---|----------|-----------------|
| 1 | `exact` | Name is in the manifest verbatim |
| 2 | `comment_context` | Name appears inside or adjacent to a TODO/FIXME/XXX/HACK/NOTE comment — intentional scaffolding, not fabrication |
| 3 | `import_gating` | Source text imports `<pkg>` AND name is the SDK's standard env (e.g. `LD_SDK_KEY` after `import * from "@launchdarkly/..."` ) |
| 4 | `sdk_catalog` | Name matches a known third-party convention AND the project's `package.json` declares the package |
| 5 | `cross_domain` | Name typed as one kind but exists as another (e.g. flag-treated-as-env) |
| 6 | `acronym` | `LD_SDK_KEY` → `LAUNCHDARKLY_SDK_KEY`, `DD_API_KEY` → `DATADOG_API_KEY`, etc. via the curated abbreviation map in `sdk_env_catalog.json` |
| 7 | `case_normalize` | `databaseUrl` ↔ `DATABASE_URL` ↔ `database-url` (camel/snake/kebab confusion) |
| 8 | `levenshtein_word` | `STRIPE_KEY` → `STRIPE_SECRET_KEY` (one-token edit distance) |
| 9 | `trigram` | `DATABSE_URL` → `DATABASE_URL` (per-domain threshold: env 0.7, db 0.75) |
| 10 | `token_set` | `USER_API_TOKEN` ↔ `API_USER_TOKEN` (same token bag, different order) |
| 11 | `embedding` | Semantic similarity via Weaviate (catches `_KEY` vs `_SECRET` semantic pairs that fuzzy misses). Gracefully no-ops when Weaviate offline. |
| 12 | `project_history` | Name was declared in the project's git history but has been removed — flagged as regression |
| 13 | `suffix_pattern` | `STRIPE_PUBLIC_KEY` near declared `STRIPE_SECRET_KEY` — paired-purpose hint, never auto-replaced |
| 14 | `blacklist` (short-circuit) | User has marked this name as `blacklisted` via `claude-dejavu fabrications --mark`. Skips the cascade entirely. |

### Per-domain stacks

```
env:     exact → comment_context → import_gating → sdk_catalog →
         cross_domain → acronym → case_normalize → levenshtein_word →
         trigram(0.7) → token_set → embedding(0.85) →
         project_history → suffix_pattern
db:      exact → comment_context → cross_domain → trigram(0.75) →
         token_set → case_normalize → embedding(0.82) →
         project_history
route:   exact → comment_context → trigram(0.7) →
         embedding(0.82) → project_history
graphql: exact → comment_context → cross_domain → trigram(0.75) →
         token_set → case_normalize → embedding(0.82) →
         project_history
flag:    exact → comment_context → cross_domain → trigram(0.7) →
         case_normalize → embedding(0.85) → project_history
```

## How to use it

### From inside a Claude Code session (MCP)

```
> Use dejavu_rewrite_proposed_edit to validate this change before I
> write it.
```

The tool takes the full proposed file content + the target path,
runs the cascade, returns the rewritten content + a per-name
verdict report.

### From the CLI

The CLI surfaces every grounding-related operation:

```bash
# Lint without auto-correcting (just flag fabrications)
claude-dejavu lint <file>

# Triage repeat fabrications across sessions
claude-dejavu fabrications
claude-dejavu fabrications --status=open --min-count=3
claude-dejavu fabrications --mark FAKE_KEY env blacklisted

# Declare an SDK convention so future runs find it via the manifest
claude-dejavu env propose LAUNCHDARKLY_SDK_KEY \
    --package=@launchdarkly/node-server-sdk --apply

# Generate a runtime defense layer
claude-dejavu generate runtime-trap --language=typescript
```

### From the PreToolUse hook (automatic)

The plugin's PreToolUse hook on Edit/Write/MultiEdit invokes the
linter automatically. With `lint.mode=block` or `autofix`, the hook
refuses to write fabricated names; with `suggest`, it surfaces the
candidates inline. Configure via:

```bash
claude-dejavu config set lint.mode autofix
```

## SDK convention catalog

`code/sdk_env_catalog.json` ships with curated entries for ~30
common packages. Each package maps to the env vars its README +
`.d.ts` files document. Examples:

```json
{
  "env": {
    "@launchdarkly/node-server-sdk": [
      "LAUNCHDARKLY_SDK_KEY", "LD_SDK_KEY"
    ],
    "aws-sdk": [
      "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
      "AWS_SESSION_TOKEN", "AWS_REGION"
    ],
    "stripe": [
      "STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY",
      "STRIPE_WEBHOOK_SECRET"
    ]
  },
  "abbreviations": {
    "LD": "LAUNCHDARKLY", "DD": "DATADOG", "PG": "POSTGRES"
  }
}
```

Auto-extend the catalog with the bundled scraper:

```bash
# Conservative: scrape READMEs + .d.ts files via npm registry
python scripts/scrape_sdk_envs.py

# Heavier: scrape the package's actual GitHub source tree (requires
# GITHUB_TOKEN for non-trivial yields)
GITHUB_TOKEN=ghp_... python scripts/scrape_sdk_envs.py \
    --source=github @launchdarkly/node-server-sdk

# Apply the proposed catalog updates
python scripts/scrape_sdk_envs.py --apply
```

The scraper is designed for periodic maintenance, not in-session
use. It's never run automatically — every catalog change ships
through review.

## Per-project fabrication memory

Every name the rewriter classifies as RESIDUAL or REPLACE is
recorded in the `fabrication_history` table. Future encounters of
the same `(project_slug, name, domain)` triple bump the count, so
you can see at a glance which names the model keeps inventing in
your codebase:

```bash
$ claude-dejavu fabrications
project: myapp
summary: open=4  declared=12  blacklisted=1  total_occurrences=37

  name                             domain   status       count  last_seen
  -------------------------------- -------- ------------ -----  ----------
  LAUNCHDARKLY_SDK_KEY             env      open             4  2026-05-04 14:23:11
  GoogleAnalytics_TRACKING_ID      env      open             3  2026-05-04 13:55:02
  Post.confirmed                   db       blacklisted      2  2026-05-04 12:08:44
  ...
```

Triage:
- **declared** → user added it to `.env.example` / schema (use `env propose`)
- **blacklisted** → don't keep proposing this; the rewriter
  short-circuits the cascade entirely on next encounter

## Runtime defense layer

The gen-time rewriter catches the vast majority of hallucinations
before disk — in v0.5.0a benchmark tests we measured up to 99%
reduction (n=200, not a stability guarantee; the rigorous headline
remains the v0.3.12 89.3% reduction at n=400). The runtime trap
closes the remaining edge cases — a fabricated name that reaches
production code throws a dejavu-branded error at first execution
instead of silently returning `undefined` and crashing two layers
down with a confusing message.

```bash
# Generate the trap
claude-dejavu generate runtime-trap --language=typescript

# Use it in your code:
import { env } from "./src/lib/env";

const url = env.DATABASE_URL;       // OK if declared
const fake = env.MADE_UP_KEY;       // throws DejavuUndeclaredEnvError
```

The TypeScript form uses Zod + Proxy. The DeclaredEnvKey union
type means tsc itself flags the mistake in your IDE — runtime is
the second line of defense.

The Python form (`--language=python`) generates a `dejavu_env`
module with `_EnvProxy` + `DejavuUndeclaredEnvError`.

See `code/runtime_trap.py` for the templates.

## What if the rewriter can't fix something?

Anything that lands as RESIDUAL — no fuzzy match, not in the SDK
catalog, no historical record, no semantic neighbor — is reported
verbatim with the closest candidates listed. From there:

1. **`env propose` it** if it's a real env var the project should
   declare — one CLI command, no manual editing.
2. **`fabrications --mark ... blacklisted`** if the model keeps
   inventing this name and you want it banned.
3. **Use the optional `--use-residual-llm` mode** in the bench /
   replay — sends the residual to a single batched Haiku call for
   clarification (TYPO_OF_X / ADD_NEW / OMIT). Off by default to
   keep the deterministic headline number clean.

## Validation

Three regression suites exercise the rewriter:

```bash
# Infra-free: cascade verdicts, error log, generators (no PG/Weaviate)
python tests/test_v05_followups.py

# Full smoke (needs PG + Weaviate)
python tests/smoke.py

# Replay against the v0.5.0a 200-trial corpus (offline, no API calls)
python tests/replay_with_rewriter.py
```

The replay is the cheapest regression check: it scores the existing
raw model outputs against the current rewriter and reports the
post-rewrite hallucination rate. Should always be 0.5% on the
v0.5.0a corpus; any regression there means a strategy started
emitting false positives.
