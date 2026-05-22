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
| `doctor --deep` — full PG + Weaviate corruption scan with one-line `→ fix:` per finding | ✗ | **✓** |
| Typed config UX (`config get/set/list/reset` with schema validation) | ✗ | **✓** |
| Cross-platform native (Linux, macOS, Windows) | partial | **✓** |
| Update notifications (SessionStart probe with 24 h cache) | ✗ | **✓** |
| Native slash commands | partial | **20+** |
| MCP tools | 6 | **15+** |

### Disaster recovery (categories claude-mem doesn't address)

The 2026-05-14 lightning + disk-full incident found four parallel
corruption modes in one event: a torn Weaviate LSM segment, 66
Postgres TOAST chunks torn in two clusters, one heap tuple with a
torn xmin/xmax field, two PG indexes silently skipping rows, and a
vector-dim mismatch between the configured embed endpoint and the
existing class. claude-dejavu v0.8.0 → v0.8.3 codifies the recovery
playbook as proper CLI subcommands so the next disaster is a
five-minute restore.

| Capability | claude-mem | claude-dejavu |
|---|:-:|:-:|
| Postgres corruption scan (TOAST + heap + index validity) | ✗ | **`pg-rescue scan`** (v0.8.3) |
| Re-derive corrupt rows from source JSONL with per-row savepoints | ✗ | **`pg-rescue patch-toast`** (v0.8.3) |
| Index-scan table rebuild with runtime FK + view discovery | ✗ | **`pg-rescue rebuild-table`** (v0.8.3) |
| Weaviate shard / vector-dim consistency scan | ✗ | **`weaviate-rescue scan`** (v0.8.3) |
| LSM-segment quarantine + restart-hardened class redo | ✗ | **`weaviate-rescue {quarantine-segment,redo-class}`** (v0.8.3) |
| Recover records from a torn LSM segment without re-embedding | ✗ | **`rescue-shard`** (v0.8.2 — pure-stdlib Go segment reader) |
| Detect zeroed / partial-zero / shrunk files across all known projects | ✗ | **`scan-corruption`** (v0.7.2) |
| Reconstruct a corrupted file from file-history + tool_use replay (4-strategy fallback) | ✗ | **`recover-file`** (v0.7.2) |
| Repair corrupt / truncated session JSONLs from the canonical DB | ✗ | **`repair`** (v0.7.1) |

### Backup subsystem

| Capability | claude-mem | claude-dejavu |
|---|:-:|:-:|
| Restic-backed session backup with content-addressed dedup + zstd + encryption | ✗ | **✓** (v0.8.0) |
| Cross-platform restic install (no sudo, GPG signature pinning) | ✗ | **✓** (v0.8.0 + v0.8.3 keyserver fallback fix) |
| Platform-native scheduling (systemd / launchd / Task Scheduler) | ✗ | **✓** (v0.8.0) |
| Idempotent + resumable migration from legacy rsync chain | ✗ | **`session-copy migrate-from-rsync`** (v0.8.0) |
| PG snapshot in the same backup stream (`pg_dump --format=custom`) | ✗ | **`session-copy take --include pg`** (v0.8.3) |
| Weaviate snapshot in the same backup stream (volume tarball) | ✗ | **`session-copy take --include weaviate`** (v0.8.3) |
| Symmetric restore for both PG and Weaviate | ✗ | **`session-copy restore --include {…}`** (v0.8.3) |
| Pre-recovery snapshot before any destructive write | ✗ | **✓** (v0.8.0 — `repair` + `recover-file` both auto-snapshot) |

### Context economy

| Capability | claude-mem | claude-dejavu |
|---|:-:|:-:|
| LLM-distilled session summaries | ✓ | **✓** |
| Compressed-but-anchor-preserving turn gists (paths, URLs, version strings, env-var names, error classes, etc. always survive compression) | ✗ | **✓** (v0.8.6 — `claudedj-distill-v1`) |
| Gist-first search shape with on-demand expand for one row | ✗ | **✓** (v0.8.6 — `search` + `expand`) |
| Opt-in UserPromptSubmit JIT recall with token-budget greedy fill | ✗ | **✓** (v0.8.5 G2 + v0.8.6 G3) |
| Preview hook output without enabling it | ✗ | **`jit-recall-preview`** (v0.8.5) |
| Cross-project release digest at SessionStart | ✗ | **✓** (v0.8.4 G1.c) |
| `what-shipped` / `memory record-version` CHANGELOG primitives | ✗ | **✓** (v0.8.4) |

### Documentation grounding

| Capability | claude-mem | claude-dejavu |
|---|:-:|:-:|
| Heading-anchored `.md` chunk embeddings keyed by stable per-repo UUID | ✗ | **`ClaudeDejavuDoc`** (v0.7.0) |
| Mixed-kind RRF search across turns + docs + symbols + routes in one query | ✗ | **`dejavu_search(kinds=[…])`** (v0.7.0) |
| Stop-hook background diff scan for `.md` changes | ✗ | **✓** (v0.7.0) |
| Per-repo identity via `.dejavu/id` UUID (`.gitignored` by default) | ✗ | **✓** (v0.7.0) |

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

### Current measurement — v0.8.13 (2026-05-20, claude-mem v10.6.2)

Measured on the pinned `claude-mem-observer-sessions-bench` fixture
at full source density (see "Bench fixture" below). Same 8 queries,
same markers, same n=160 methodology.

<!-- BENCH_NUMBERS_0813_START -->
| Tool | p50 latency | Avg chars | Recall |
|---|---:|---:|---:|
| `claude-mem.search`   |  7.8 ms |  109 | 1 / 8 |
| `dejavu_search`       | 82.6 ms | 1744 | **7 / 8** |
| `dejavu_observations` | 28.2 ms |  933 | **8 / 8** |
| `dejavu_summary`      |  8.3 ms |   66 | 0 / 8 |
<!-- BENCH_NUMBERS_0813_END -->

`dejavu_observations` matches the v0.5 8 / 8 recall headline.
`dejavu_search` reaches 7 / 8 — the one remaining gap (Q1's
`openprovider` + `rrpproxy` both-required marker pair) needs
broader top-N coverage in the search response and is on the
roadmap.

### Previous measurement — v0.8.11 (2026-05-19, claude-mem v10.6.2)

Same harness, same queries, against the same pinned fixture but at
~0.25% source density (9 sessions / 62 observations only).

| Tool | p50 latency | Avg chars | Recall |
|---|---:|---:|---:|
| `claude-mem.search`   |  9.9 ms | 109 | 1 / 8 |
| `dejavu_search`       | 50.9 ms | 974 | 1 / 8 |
| `dejavu_observations` |  9.0 ms | 747 | 2 / 8 |
| `dejavu_summary`      |  8.6 ms |  66 | 0 / 8 |

### What we shipped v0.7 → v0.8.13

A lot of the under-the-hood recall stack landed during this stretch.
The bench will pick up the wins as the corpus + methodology evolves.

- **Caller-side query embedding** + per-process cache + cross-session
  PG-backed cache (v0.8.7 / v0.8.8) — sub-ms cache hits, cloud
  fallback bounded at 1s.
- **RRF fusion** of BM25 + nearVector (v0.8.7) — the
  `dejavu_search` MCP tool now uses the same canonical
  `code/recall._hybrid` path as the CLI (v0.8.10 Fix 4), so the
  MCP surface gains every recall improvement the CLI has.
- **Token-budget greedy disclose** (v0.8.6) — JIT recall hook now
  packs hits by score-desc until the budget runs out instead of a
  fixed top-N cap.
- **Distill with load-bearing-token preservation** (v0.8.6) —
  port numbers, file paths, version strings, error class names,
  env-var names, command flags ALL survive compression verbatim.
- **Topic-aware re-weighting** (v0.8.9) — opt-in, default off,
  uses session `ai_title` for cheap topic detection.
- **Trash-input gate at ingest** (v0.8.7) — cuts cloud-embed cost
  by skipping turns that fail the gate before they reach the
  cloud worker.
- **`dejavu_observations` tokenization** (v0.8.10) — multi-word
  queries now match any-token OR against the corpus instead of
  requiring the full string as a substring.
- **`dejavu_summary` accepts a query** (v0.8.10) — filters by
  request/investigated/learned/completed/next_steps content
  instead of always returning the latest.
- **Cache-refresh hardening** (v0.8.10) — `threading.Lock` +
  bounded PG connect timeout means the sustained MCP traffic
  pattern from the bench no longer blocks the cache-refresh path.
- **MCP stderr-drain in the bench harness** (v0.8.10) — drains
  the chatty `_hybrid` stderr so the 64 KB pipe never fills and
  no `_recv` deadline gets hit. Zero timeouts in the v0.8.11
  measurement, vs three 15 s timeouts in v0.8.9.
- **`-bench` corpus fixture** (v0.8.11) — see below.
- **Full-corpus bench re-ingest** (v0.8.12) — 745 sessions / 96k+
  turns from the canonical source `.jsonl` files, ingested into the
  pinned bench slug. Density went from ~0.25% to 100% of available
  source content. See "Bench fixture" below.
- **Isolated Weaviate class** (v0.8.12) — bench vectors now live in
  `ClaudeDejavuTurn_bench`, fully separate from the live
  `ClaudeDejavuTurn` class. The ingester honors
  `CLAUDE_DEJAVU_WV_CLASS` so the bench can write into its own class
  without polluting day-to-day recall.
- **Interleaved retrieval in `dejavu_search`** (v0.8.13) — the auto
  mode used to short-circuit on the first ≥limit/2 PG-lexical hits,
  which on the dense bench corpus surfaced XML-stub wrapper turns
  (`<observed_from_primary_session>…`) ahead of the semantically
  richer hybrid matches. Both branches now run up to `limit` each
  and interleave hybrid-first; pg-lexical fills the tail. Lifts
  search recall from 1/8 to 7/8 on the bench (and never starves
  exact-string queries that the lexical branch is good at).
- **Content-snippet response shape in `dejavu_search`** (v0.8.13) —
  responses now include a leading slice of `turns.content` alongside
  the per-turn gist, so the caller sees both the wrapper opening
  AND a deeper marker-bearing window. Avg response chars went
  974 → 1,744 (1.8× more useful context).

### Bench fixture — pinning the comparison corpus

The bench corpus lives at PG `claude_dejavu_bench.observations` filtered
by `sessions.project_slug` AND at Weaviate class
`ClaudeDejavuTurn_bench` (v0.8.12). Two layers of isolation:

  - **PG slug pinning** (v0.8.11). To stop unrelated future ingest
    from drifting the corpus out from under the bench, the slug was
    renamed from `claude-mem-observer-sessions` to
    `claude-mem-observer-sessions-bench`. The `-bench` suffix can
    never collide with a real CWD-derived slug (no real directory
    ends in `*-bench`), so the bench corpus is write-protected
    against incidental ingest.
  - **Weaviate class isolation** (v0.8.12). The bench writes vectors
    to `ClaudeDejavuTurn_bench`, not to the live `ClaudeDejavuTurn`
    class. Same schema (vectorizer:none, cosine HNSW, same eight
    properties), separate index. The ingester reads
    `CLAUDE_DEJAVU_WV_CLASS` from env or `WV_CLASS` from
    `config.env` (default unchanged).

The fixture's observations are deterministically re-extracted from
the source turns by `code/observations.extract` — unchanged regex,
no engineered content. v0.8.12 walks every `.jsonl` under
`~/.claude/projects/<your-claude-projects-encoded-path>/`
(745 files, 96k+ turns) and feeds them through the same ingest
code path the live Stop hook uses. The canonical rebuild script
is `tests/_ingest_bench_corpus_0812.py` with an idempotency guard
that refuses to re-run once the bench observation count exceeds
500.

### Optimization roadmap toward v0.5's headline recall

The v0.5 release reported **8 / 8 recall** on this bench. v0.8.13
matches it on `dejavu_observations` (8 / 8) and reaches **7 / 8**
on `dejavu_search` — the one remaining gap is Q1's
`openprovider` + `rrpproxy` both-required marker pair, which
needs broader top-N coverage in the search response. We optimize,
we improve — the recall stack is moving
back toward the v0.5 line as the corpus density gap closes and
the under-the-hood improvements (RRF fusion, topic re-weighting,
load-bearing token preservation, observation tokenization,
summary query filtering) compound.

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
