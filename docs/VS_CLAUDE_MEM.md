# claude-dejavu vs claude-mem

A capability and benchmark comparison. claude-dejavu is the broader,
faster, and license-friendly choice; claude-mem remains relevant
for SQLite-only deployments.

---

## TL;DR — when to pick which

| Your situation | Pick |
|---|---|
| Corporate codebase that bans AGPL-3.0 (most large companies) | **claude-dejavu** (FSL-1.1-ALv2 — no copyleft contagion, internal use unrestricted) |
| Need to recover sessions Claude Code accidentally deleted | **claude-dejavu** (claude-mem can describe a session, can't restore the file) |
| Need a lossless audit trail (every Bash, every Edit, every error) | **claude-dejavu** |
| Need to find specific technical strings (filenames, error codes, command outputs) | **claude-dejavu** (hybrid BM25 + vector at raw-turn level) |
| **Pre-write code grounding (catch fabricated function/class names BEFORE they hit disk)** | **claude-dejavu** (claude-mem doesn't index code) |
| **PreToolUse linter on Edit/Write/MultiEdit, twelve languages** | **claude-dejavu** (no equivalent elsewhere) |
| **Detect typo'd env vars / DB fields / route URLs / GraphQL fields / feature flags** | **claude-dejavu** (eight grounding domains, deterministic post-generation rewriter) |
| Want minimal infrastructure (just SQLite, no Postgres / Weaviate / Docker) | claude-mem |
| Want both for maximum coverage | **install both** — they coexist cleanly |

For most teams on most workloads, claude-dejavu wins. claude-mem
remains a valid pick if your only concern is SQLite-only ops.

---

## Capability matrix

### Memory and recovery

| Capability | claude-mem | claude-dejavu |
|---|:-:|:-:|
| LLM-distilled session summaries | ✓ | **✓** |
| Typed observations (bugfix / decision / discovery / etc.) | ✓ | **✓** |
| SessionStart context injection | ✓ | **✓** |
| Lossless turn-by-turn audit | ✗ | **✓** |
| **Restore byte-perfect deleted `.jsonl` files** | ✗ | **✓** |
| Hybrid BM25 + vector search | partial (vector only) | **✓** |
| File-touch index ("which sessions touched `services/auth/`") | ✗ | **✓** |
| Tool-error timeline (every Bash failure with input + truncated output) | ✗ | **✓** |
| Workspace scoping (per-repo + per-workspace privacy) | ✗ | **✓** |
| Per-Linux-user data isolation | partial | **✓** |

### Code symbol grounding (claude-mem doesn't have this category)

| Capability | claude-mem | claude-dejavu |
|---|:-:|:-:|
| Tree-sitter symbol index across **twelve** languages (Python, TypeScript, TSX, JavaScript, Go, Rust, Java, Ruby, Bash, PHP, C#, Kotlin) | ✗ | **✓** |
| Mid-conversation symbol resolver MCP tool (`dejavu_resolve_symbol`) — fuzzy via trigram + edit-distance, ~16 ms on 21k symbols | ✗ | **✓** |
| Pre-write linter (PreToolUse hook on Edit/Write/MultiEdit) | ✗ | **✓** |
| Type-aware grounding for `obj.method()` calls | ✗ | **✓** |
| Five lint intervention modes (off / warn / suggest / block / autofix) | ✗ | **✓** |
| Auto-reindex on first session in a project | ✗ | **✓** |

### Identifier grounding across eight domains

claude-dejavu doesn't stop at code symbols. It also catches fabricated:

| Domain | What dejavu validates |
|---|---|
| **Env vars** | `process.env.X` / `os.environ['X']` against `.env.example`, with SDK-convention catalog (LaunchDarkly, AWS, Stripe, Sentry, Redis, Postgres, Next, Datadog, OpenAI, Anthropic, Supabase, …) |
| **DB models + fields** | Prisma / Drizzle calls against the schema |
| **HTTP routes** | `fetch` / `axios` URLs against indexed Express / Next / Fastify routes |
| **GraphQL fields + types** | `gql` template literals against indexed schema |
| **Feature flags** | `isEnabled('flag')` calls against indexed flag config |
| **CSS / Tailwind classes** | against the project's design system |
| **i18n keys** | `t('key')` against indexed translation files |
| **npm module exports** | `import { x } from 'pkg'` against the package's `.d.ts` exports |

claude-mem doesn't have any of these.

### Plugin / install / introspection

| Capability | claude-mem | claude-dejavu |
|---|:-:|:-:|
| Setup hook (auto-install on `/plugin install`) | ✓ | **✓** |
| Three-state Docker probe (absent / down / ok) — catches Docker Desktop installed but not running | ✗ | **✓** |
| Install state surfaced to Claude in chat (works for VS Code / IDE users with no terminal access) | ✗ | **✓** |
| `doctor` self-diagnostic (CLI + MCP + slash command) | ✗ | **✓** |
| Typed config UX (`config get/set/list/reset` with schema validation) | ✗ | **✓** |
| Cross-platform native (Linux, macOS, Windows) | partial | **✓** |
| Update notifications (SessionStart probe with 24 h cache) | ✗ | **✓** |
| Native slash commands | partial | **10** |
| MCP tools | 6 | **15+** |

### License

| | claude-mem | claude-dejavu |
|---|---|---|
| License | AGPL-3.0 — copyleft, contagion | **FSL-1.1-ALv2** — fair-source, no contagion, → Apache 2.0 after 2 years |

AGPL-3.0 creates copyleft contagion: modifying claude-mem and running
it on a network server triggers source-disclosure obligations. This
is why most large enterprises (and many smaller ones) ban AGPL by
policy. claude-dejavu's FSL has no such clause: internal corporate
use is fully permitted, no source-disclosure obligation, no contagion
into your own codebase. The only restriction is on Competing Use
(e.g. hosting it as a managed service for third parties), which a
typical corporate user is not doing. Each version converts to
Apache 2.0 two years after release.

### MCP tool inventory

**claude-mem (6):** `search`, `timeline`, `get_observations`,
`smart_search`, `smart_unfold`, `smart_outline`

**claude-dejavu (15+):** `dejavu_search`, `dejavu_expand`,
`dejavu_resume`, `dejavu_sessions`, `dejavu_errors`, `dejavu_files`,
`dejavu_restore`, `dejavu_summary`, `dejavu_observations`,
`dejavu_handoff`, `dejavu_workspaces`, `dejavu_resolve_symbol`,
`dejavu_symbols`, `dejavu_lint_proposed_edit`, `dejavu_check_name`,
`dejavu_doctor`, `dejavu_config`, `dejavu_about`

---

## Benchmark — parity layer (apples-to-apples)

Both systems compress past Claude Code activity into typed knowledge
records. Same kind of operation, same kind of corpus, same machine.

**Setup**: 8 queries × 25 calls each (5 warmup discarded → n=160
measured per tool), both invoked through their canonical MCP servers
via JSON-RPC over stdio. Strict marker-based recall scoring.

| Tool | p50 latency | Avg chars | Recall |
|---|---:|---:|---:|
| `claude-mem.search` | 8.0 ms | 109 | 1 / 8 |
| `dejavu_observations` | 16.0 ms | 95 | **8 / 8** |
| `dejavu_summary` | 15.8 ms | **2,194** (22 ×) | **8 / 8** |

**Apples-to-apples on useful content per millisecond:**

| Metric | claude-mem | claude-dejavu |
|---|---:|---:|
| Successful retrievals (out of 8) | 1 (12.5 %) | **8 (100 %)** |
| Latency on successful retrievals | 389 – 1,807 ms | **12 – 16 ms** |
| Speedup on real content path | baseline | **30 × – 150 ×** |

When both systems actually retrieve content, dejavu is **30–150 ×
faster**. claude-mem's median is dominated by error responses
(7 of 8 queries returned `Worker API timed out after 3000ms`).
dejavu has no equivalent failure mode.

---

## Hallucination grounding benchmark

claude-dejavu's flagship feature: detecting and correcting fabricated
identifiers before they reach disk. Synthetic project, 20 prompts ×
20 trials = 400 paired trials, claude-sonnet-4-6.

| Mode | Hallucination rate |
|---|---:|
| OFF (no grounding) | 51.5 % |
| ON (claude-dejavu, schema in system prompt) | 5.5 % |
| ON (claude-dejavu tool-use + deterministic rewriter) | **0.5 %** |

**89.3% relative reduction** with grounding context
(n=400 paired trials, McNemar p<1e-26). v0.5.0a's tool-use mode +
deterministic rewriter has measured **up to 99% reduction** in
additional tests (n=200, not yet a stability guarantee — treat
89.3% as the rigorous headline). Full report and raw data in
[`docs/benchmarks/`](benchmarks/).

claude-mem doesn't measure or address hallucination — it operates on
session summaries, not pre-write code grounding.

---

## Benchmark — raw turn search (claude-dejavu's unique territory)

claude-mem can't do this — its corpus is summaries only.

| Query type | Path | dejavu p50 |
|---|---|---:|
| Single-concept lexical | PG-FTS only | **15 ms** |
| Multi-word natural-language | Weaviate hybrid (BM25 + vector) | 200 – 400 ms |
| Mixed (lexical hits + augmentation) | PG + Weaviate | 220 – 260 ms |

`dejavu_search` runs in `mode=auto` by default: PG-FTS first
(sub-25 ms warm), falls through to Weaviate hybrid only when PG
returns fewer than `limit/2` hits. Force a single mode with
`mode='lexical'` or `mode='hybrid'`.

---

## Decision guide — by use case

### "I want my Claude sessions to never be lost again"
**claude-dejavu.** The only plugin in this category that mirrors
the live `.jsonl` off-tree on every Stop event and offers byte-perfect
restoration via `dejavu_restore`.

### "I want Claude to remember context from past conversations"
**Either** works for cross-session recall. claude-dejavu's
`dejavu_summary` + `dejavu_handoff` ship the same UX as claude-mem's
`search` + observations, under FSL — internal use unrestricted.

### "I work in a regulated industry / corporate environment"
**claude-dejavu.** AGPL-3.0 (claude-mem's license) creates copyleft
contagion that most large enterprises ban by policy. dejavu's FSL
has no such clause.

### "I want Claude to stop writing code that references functions / env vars / DB fields that don't exist"
**claude-dejavu.** No competitor does pre-write grounding across
twelve languages and eight identifier domains.

### "I need to find when Claude last edited file X"
**claude-dejavu.** `dejavu_files <path>` queries an indexed
`file_touches` table. claude-mem doesn't track file paths.

### "I need to find every failed Bash command in my history"
**claude-dejavu.** `dejavu_errors --tool=Bash` queries the
`tool_calls` table. claude-mem doesn't track tool failures.

### "I want fast, simple, SQLite-only memory"
**claude-mem.** It's the right tool if your priority is "no PG, no
Weaviate, no Docker." dejavu requires the heavier stack.

### "I want both"
**Install both.** They coexist cleanly — different stores, different
hooks, different MCP namespaces. Common pairing: claude-mem for
distilled wisdom + dejavu for lossless audit + restore + grounding.

---

## Reproducibility

```bash
# Parity-layer comparison
PG_HOST=localhost PG_PORT=5432 \
  python3 tests/bench_summary_layer.py

# Raw-turn search
PG_HOST=localhost PG_PORT=5432 \
  python3 tests/bench_vs_claude_mem.py

# Hallucination-grounding benchmark
ANTHROPIC_API_KEY=sk-ant-… \
  python3 tests/bench_hallucination.py --n=10
```

Each script spawns both MCP servers as subprocesses, sends identical
queries, measures wall-clock latency from request-write to
response-read.

---

## License

claude-dejavu is **[FSL-1.1-ALv2](../LICENSE)** — a fair-source
license. Free for any Permitted Purpose: individual use, internal
corporate use (any company size), non-commercial research,
professional services, OSS projects. No copyleft contagion. Each
version automatically becomes **Apache 2.0** two years after release.
A commercial license is available for use cases outside the Permitted
Purpose (bundling, hosting as a managed service, competing products) —
see [COMMERCIAL.md](../COMMERCIAL.md) or contact
**dejavu@hendrikthurau.enterprises**.

claude-mem is **AGPL-3.0** — read its license carefully if you intend
to use it in any commercial or hosted-service context.
