# claude-dejavu Roadmap

Last updated: 2026-05-18 — after `v0.8.6` shipped.

This document is the single source of truth for what comes next. The
historical phase plans (v0.3 → v0.4.x) are preserved below for context;
the live "Shipped" and "Next" sections at the top are the canonical
status.

---

## Shipped 2026-04-30 → 2026-05-18 (v0.5 → v0.8.6)

The plugin grew along five axes in this window. Distilled headlines —
full per-version notes live in `CHANGELOG.md`.

### Recovery + resilience (root-cause response to the 2026-05-14 lightning + disk-full incident)

- **v0.7.1** — `claude-dejavu repair` reconstructs corrupt / truncated
  session JSONLs from the `turns` table. Auto-detect at SessionStart.
- **v0.7.2** — `claude-dejavu recover-file <path>` reconstructs a
  corrupted file by combining a file-history snapshot with replay of
  every Edit / MultiEdit / Write `tool_use` in the session JSONL.
  Four-strategy fallback stack. `claude-dejavu scan-corruption`
  surfaces zeroed / partial-zero / shrunk files across all known
  projects.
- **v0.8.2** — `rescue-shard` plus a pure-stdlib Go binary
  (`code/rescue/cmd/dejavu-rescue/`) that walks Weaviate v1.27.x LSM
  segments without any Weaviate imports. Recovers records from torn
  segments and re-embeds only the genuinely-missing UUIDs (no
  double-billing the embedding cost).
- **v0.8.3** — full rescue toolkits as proper CLI: `pg-rescue
  {scan,patch-toast,reset-flags,rebuild-table,reindex}` and
  `weaviate-rescue {scan,quarantine-segment,redo-class}`. Codifies the
  five ad-hoc shell scripts the incident produced.
- **v0.8.3** — `claude-dejavu doctor --deep` runs both rescue scans
  and surfaces every issue with the exact remediation command.

### Backup subsystem (replacing the unbounded rsync chain)

- **v0.8.0** — `claude-dejavu session-copy` end-to-end:
  `init/take/list/diff/restore/mount/prune/status/check/migrate-from-rsync`.
  Restic-backed (content-addressed dedup, encryption, zstd
  compression). Default retention 24 hourly + 7 daily + 13 weekly
  (~90 days in ≤44 snapshots).
- **v0.8.0** — cross-platform `restic` install via
  `code/install_restic.py` (no sudo). GPG signature pinning with
  fingerprint constant.
- **v0.8.0** — `migrate-from-rsync` 10-step idempotent + resumable
  migration auto-invoked by `scripts/install.py [7/9]`. Per-step
  rollback contracts.
- **v0.8.0** — VACUUM INTO consistent SQLite staging avoids "database
  disk image is malformed" from torn WAL snapshots.
- **v0.8.3** — `session-copy` gains `--include sessions,sqlite,pg,weaviate`.
  `pg` uses `pg_dump --format=custom`; `weaviate` snapshots the volume
  as a tarball. Symmetric `restore` runs `pg_restore --clean
  --if-exists` and tar-extract. Both refuse to clobber populated
  targets without `--force`.
- **v0.8.3** — `session-copy reclaim-legacy` deletes the
  `~/.claude-backups/snapshots.deleteme.<ts>` tree the migration left
  behind, with TTY guard + typed confirmation.
- **v0.8.3** — restic install GPG-keyserver fallback chain
  (`keys.openpgp.org` → `keyserver.ubuntu.com` → `pgp.mit.edu`); fixes
  the broken v0.8.0 first-install path.

### Documentation + repo identity

- **v0.7.0** — `ClaudeDejavuDoc` Weaviate class for heading-anchored
  `.md` chunks. Stable per-repo identity via UUID in
  `<repo_root>/.dejavu/id`. Workspace migration from slug strings to
  UUIDs.
- **v0.7.0** — `dejavu_search(kinds=[…])` extension: `turn` / `doc` /
  `symbol` / `route` with reciprocal-rank-fusion across kinds.
  `dejavu_expand` prefix dispatch.
- **v0.7.0** — `claude-dejavu reindex --docs`, `search-docs`, `repo
  {show,rebind,prune}`, `workspace-migrate`, stop-hook background diff
  scan (`code/doc_diff_scan.py`).

### Context economy (G1, G2, G3 — three-phase Phase G initiative)

- **v0.8.4** (G1) — `claude-dejavu what-shipped` summarizes the last
  N CHANGELOG entries. `claude-dejavu memory record-version` distills
  one CHANGELOG section into `DATA_ROOT/versions/`. New
  `hooks/session_start_digest.py` emits a cross-project release digest
  (last 3 versions × 20 projects, hard-capped at 6000 chars).
  `changelog_distill.py` is the dependency-free heuristic distiller
  behind both.
- **v0.8.4** (G4) — `ingester.cmd_retry_vectorize` gains parallel
  sharded retry via `--workers N`. Default 1 keeps the existing path
  byte-identical.
- **v0.8.5** (G2) — opt-in UserPromptSubmit JIT recall hook
  (`hooks/user_prompt_submit.py`). Fans out to a BM25 search and
  injects top-N gists. Default-disabled (`JIT_RECALL_ENABLED=false`);
  preview without enabling via `claude-dejavu jit-recall-preview
  "<prompt>"`. Defensive-by-design: every failure mode exits 0 with
  empty stdout.
- **v0.8.5** — `JIT_RECALL_SCORE_THRESHOLD` retuned 0.6 → 2.0 (BM25
  range is 1-20+, not 0-1 cosine — 0.6 silently rejected every real
  hit).
- **v0.8.6** (G3) — `code/distill_pipeline.py` wraps the closed-source
  `dejavu_bridge.distill` primitive. Pre-extracts load-bearing tokens
  (paths, URLs, ports, versions, command flags, env-var names, error
  classes, backticked identifiers, numbers-with-units), compresses the
  body, splices anchors back as `[anchors: …]` prefix, round-trips
  every anchor against the compressed body. Skip-types and
  anchor-loss both return `(None, None)` → store `gist=NULL` and fall
  back to `content[:200]` on read.
- **v0.8.6** — `claude-dejavu memory backfill-gists` walks every
  `gist IS NULL` row and writes a compressed gist. Sharded via
  `--workers M`, idempotent unless `--force`.
- **v0.8.6** — `claude-dejavu search` returns a gist-first shape by
  default (`{session_id, turn_id, gist, anchors[], score, ts}`).
  `--full` reverts to the legacy content-dump shape. `expand
  <turn_id>` remains the canonical way to pull one row's full content.
- **v0.8.6** — `hooks/user_prompt_submit.py` calls
  `dejavu_bridge.disclose(hits, token_budget=JIT_RECALL_TOKEN_CAP)`
  to greedy-fill up to the budget instead of fixed top-N truncation.
- **v0.8.6** — `code/dejavu_bridge_stub.py` activates automatically
  when the closed-source wheel is absent. The full test suite passes
  against the stub; SessionStart logs which mode is active.

### Suite growth

| Version | Tests passing | Delta |
|---|---|---|
| v0.8.6 | 926 / 0 | +68 |
| v0.8.5 | 858 / 0 | +50 |
| v0.8.4 | 808 / 0 | +94 |
| v0.8.3 | 714 / 0 | +(included full rescue + session-copy tests) |

---

## Next

Pending after v0.8.6 ships. Order is rough — the first three are
near-term ergonomic wins; the others depend on data we don't have yet.

1. **Multi-platform wheel CI for `dejavu_bridge`** — today the wheel
   is built locally for one platform at a time. Need a release matrix
   (Linux x86_64 / aarch64, macOS x86_64 / aarch64, Windows x86_64) so
   the closed-source dep can be `pip install`-ed without a
   per-platform build dance.
2. **Real reranker provider for JIT recall** — v0.8.6's
   `dejavu_bridge.disclose` is greedy-fill on BM25 score; the next
   step is a small cross-encoder rerank pass (caller-side embed →
   `nearVector:` for the top-K and a second-stage rerank). The wheel
   already exposes the surface; we need the integration + tuning.
3. **Gist-first quality tuning** — measure `claudedj-distill-v1`
   compression ratios + anchor preservation on the live corpus; tune
   the anchor-extraction regexes for cases we're currently
   under-anchoring (assembly, SQL DDL, raw JSON dumps).
4. **Sem-cache HNSW** — `dejavu_bridge.sem_cache_lookup` runs a flat
   scan today. An HNSW index over the cache would unlock larger
   working sets for the JIT recall hook without blowing the 2 s hook
   timeout.
5. **Reranker auto-eval harness** — extend `tests/run_all.py` with a
   recall@k benchmark on a fixed query set so reranker tuning has a
   ground-truth target.

---

## Historical context (v0.3 → v0.4.x phase plans, preserved)

## Decision log: Phase 7 dilution risk

**Question:** Will adding `markdown_grounding` + a generalist
`name_registry` (Phase 7 / v0.4.0) dilute the code-grounding features?

**Answer: No. The architecture was built for this.**

Reasoning:

1. **Strategy registry is purely additive.** Adding `markdown_grounding`
   to `lint_strategies/` registers one more strategy alongside the
   existing eight. None of them are touched.
2. **Per-language filtering already exists.** Every strategy declares
   `languages: set[str]`. `markdown_grounding` will declare
   `{"markdown", "text", "rst"}`. It only fires for those file
   extensions. Code files trigger zero overhead.
3. **Code projects are unaffected.** A TS/Python/Go project with no
   `name_registry.json` and no `*.md` content under lint sees
   `markdown_grounding.applies_to(...)` return `False` and skip.
4. **Independent buckets in lint output.** New `prose_resolved /
   prose_low_confidence / prose_unknown` buckets in the lint result
   shape. Existing buckets (symbols/routes/pages/env/db/gql/flags)
   keep their semantics.
5. **Reindex pass adds one indexer.** `index_project_prose()` runs
   alongside the seven existing indexers, with `--skip-prose` opt-out
   and identical lifecycle.
6. **Branding stays consistent.** "claude-dejavu — grounding for
   anything you write" expands the audience without rebranding the
   dev offering.

**Conclusion:** Phase 7 is purely additive. Dev users see no change.
Writers gain a use case. Marketing surface 10×s.

---

## Decision log: Phase 8 reworked — lean monetization, no heavy infra

**Original plan:** hosted cloud platform with managed Postgres + Weaviate
+ web dashboard + Stripe billing.

**Why we're not doing that yet:** capital cost. Running cloud Postgres,
cloud Weaviate, an app server, and customer support for $19-49/seat
needs ~50+ paying seats just to break even on infrastructure, before
any HTE engineering hours.

**New plan: monetize WITHOUT building infrastructure first.** Three
revenue streams, in order of capital efficiency:

### 1. Paid plugin license keys (zero infra)

The OSS plugin stays free + fully-functional forever. A paid `Pro`
license unlocks **local-only** features that are valuable to teams:

| Feature | Why teams pay for it |
|---------|----------------------|
| **Editor integrations** (VS Code, Cursor extension) showing real-time grounding inline | Daily-use ergonomics; transforms the plugin from terminal-only to in-editor |
| **CI integration**: GitHub Action / GitLab pipeline that runs `claude-dejavu lint` on every PR + posts comments on hallucinations | Blocks bad code from merging; enterprise CTO sign-off material |
| **Custom grounding domains** — bring your DSL / internal framework / proprietary schema; we provide the indexer template + integration | Enterprise lock-in value |
| **Hallucination dashboard** (local-only HTML report) — week-over-week trend, top fabricated names, ROI ("you saved N rewrites this sprint") | Makes the case to leadership for AI-tool budget |
| **Audit log** of every flagged hallucination + decision | SOC 2 / compliance contexts |
| **Multi-LLM support** (Cursor, GitHub Copilot, generic LSP) | Expands beyond Claude users |

License model: **Stripe Checkout → license key in email → plugin
verifies offline (signed JWT)**. Zero ongoing infra for us. Customer
runs everything locally as before; license unlocks the gated features.

Pricing (anchor, refine after data):
- **Free**: everything we ship today + Phase 1-7 grounding domains
- **Pro**: $9 / dev / month — editor integrations, CI integration,
  dashboard, audit log
- **Team**: $29 / dev / month — Pro + custom grounding domain support
  (we help wire it), priority email support
- **Enterprise**: contact us — Team + custom DSL grounding, security
  review, on-prem support contract

### 2. HTE services (highest-margin, fits HTE's existing business)

claude-dejavu is the showcase. The funnel goes:

> See the plugin → "this is sharp engineering" → check who built it →
> "HTE Switzerland — full-stack development, AI infrastructure, technical
> leadership" → contact for project work.

HTE's existing services already cover this:
- Custom software development (the natural upsell)
- Cloud development (deploying client AI infrastructure)
- Web/app development (sites + apps with AI built in)
- Outsourcing / coaching / Digital Business Angel

The plugin doesn't need to "sell" services — it just needs to make HTE
findable when someone needs Swiss-quality engineering. **README hero
+ /dejavu-about slash command + footer credit. That's it.** No email
lead capture, no upsell prompts.

### 3. Optional cloud later (only after Pro license proves demand)

If we hit ~30 paid Pro seats, THEN the cloud version makes sense:
- Hosted index for users who don't want local Postgres
- Cross-team federated learning
- Web dashboard for org-wide rollups

By then we have:
- Revenue to fund infra (~$300/mo from 30 seats covers managed
  Postgres + Weaviate + small app server)
- Validated demand (people are paying for the local Pro tier)
- Real telemetry on what teams actually use

Until then, **cloud stays a deferred sketch**.

---

## Decision log: lead capture (we're NOT doing it)

The user's instinct is right: nobody wants more spam. Plugins that
nag for emails get uninstalled. We won't.

Replaced model:

| Original idea | What we ship instead |
|---------------|---------------------|
| `claude-dejavu register --email=…` opt-in | Removed. Plugin never asks for an email. |
| Drip email campaign | Removed. |
| Newsletter signup in install flow | Removed. |
| Anonymous usage telemetry | Replaced with a one-line, opt-IN, **no-email** stat ping (`config set TELEMETRY=on`) that sends only an anonymized usage count once per week. Default: off. Useful for our roadmap (knowing which grounding domains people actually use) but never tied to a person. |

Discoverability of HTE happens through:
- README hero badge linking to `hendrikthurau.enterprises/claude-dejavu`
- `/dejavu-about` slash command (opt-in user action, never autorun)
- Subtle footer credit on `claude-dejavu --version`
- Public benchmark report at `hendrikthurau.enterprises/claude-dejavu/benchmark`
- Blog posts on hendrikthurau.enterprises (the website is the funnel)

---

## Decision log: user-POV value (the actually-payable wedge)

**Reality check:** Claude API tokens are expensive. Some companies are
shifting back to human developers because cost-per-feature is rising.
This is exactly the market we should serve.

The pitch that works:

> **claude-dejavu cuts your Claude bill by reducing rewrite loops.**
>
> Every hallucination = one rewrite. Every rewrite = more tokens.
> claude-dejavu prevents the rewrite by catching the hallucination
> *before* the agent commits it.
>
> Phase 2's empirical benchmark will quantify this in concrete dollars.

Paid features that match this pitch:

| Feature | The "I'd pay for this" moment |
|---------|------------------------------|
| Hallucination dashboard | "We saved $4,200 in rewrites this month" → expensable |
| CI integration | "Bad PRs no longer reach review → 8 hrs/week reclaimed per reviewer" |
| Editor inline grounding | "I caught the typo before sending the prompt → 1 fewer Claude call" |
| Custom domains | "Our internal DSL was 40% of our hallucination rate; now it's grounded" |

This is the language for the marketing site. **Pure dev productivity
ROI, in dollars.** Not "AI fabrication detection" (jargon, no buyer).

---

## HTE positioning (research-calibrated)

Pulled from the HTE website:

- Tagline: **"Building Digital Business with IT Solutions"**
- Sub-line: "the digital age requires the best digital solutions –
  the best businesses rely on having the best technology"
- Self-description: **"Digital Business Angel"**
- Four pillars: **Satisfaction · Quality · Cutting-edge technology
  · Expert team**
- Services span: marketing, web/mobile/cloud development, UX/logo
  design, custom software, hosting/infrastructure, business support

So the README pitch is NOT "hire me, I'm the best CTO" (boastful,
narrow). It IS:

> **claude-dejavu** is built by [HTE Switzerland][hte] — a digital
> solutions partner that ships production AI infrastructure, full-stack
> development, and cloud-grade engineering for growth-focused teams.
> If you liked how this is built, that's the kind of work we do.
>
> [hte]: https://hendrikthurau.enterprises

Tone: confident, factual, low-pressure. The plugin IS the credential.

---

## The phases

### v0.3.11 — `.dejavuignore` + per-folder scopes  *[1 day]*

Stop indexing what you don't want indexed. Prerequisite for clean
benchmark in v0.3.12.

- Walk-up `.dejavuignore` discovery; gitignore syntax incl. `!` re-include
- Optional `respect_gitignore: true` config flag
- Shared `should_skip(path, project_root) -> bool` filter consumed by
  every indexer (symbols / routes / pages / env / db / gql / flags)
- New CLI: `claude-dejavu ignore status <path>` (explain) and
  `ignore list` (show all loaded rules)
- Smoke fixture exercises nested ignores + `!` re-include
- Real-world: drop `.dejavuignore` in a-large-monorepo to exclude
  `.claude-backups/`

**Acceptance:** a-large-monorepo reindex no longer counts ~58k
symbols from claude-dejavu's own dev tree.

### v0.3.12 — Empirical hallucination benchmark  *[2 days]*

The marketing weapon. Public, reproducible, statistically rigorous.

- Synthetic project (8 Prisma models, 12 env vars, 6 routes, 4 GraphQL
  types, 5 flags)
- 20 curated prompts spanning all 8 grounding domains
- N=10 trials × 2 conditions (dejavu OFF / ON) × 20 prompts = 400 trials
- Deterministic post-hoc scoring: parse Claude's output, look up names
  against schema
- Stats: bootstrap 95% CI + McNemar's test for paired difference
- API cost: ~$4 (400 calls × ~$0.01)
- Output: `tests/bench_hallucination.py`,
  `docs/benchmarks/hallucination-rate-v0.3.12.md`

**Acceptance:** report shows ≥40% reduction with statistical
significance. Reproducible from the script with `ANTHROPIC_API_KEY`.

### v0.3.13 — Branding + lead-gen funnel (calibrated to HTE)  *[1 day]*

- README hero rewritten for HTE positioning (research-calibrated copy)
- `/dejavu-about` slash + `dejavu_about` MCP tool: shows credit,
  links HTE website, NEVER captures email
- Footer on `claude-dejavu --version` linking
  `hendrikthurau.enterprises/claude-dejavu`
- One tasteful end-of-session systemMessage when N≥5 hallucinations
  caught: "claude-dejavu prevented N rewrites this session. Built by
  HTE Switzerland — see hendrikthurau.enterprises/claude-dejavu"
- HTE subpage skeleton: `/claude-dejavu` landing,
  `/claude-dejavu/benchmark`, `/claude-dejavu/docs`,
  `/claude-dejavu/contact` (the page is built by you on
  hendrikthurau.enterprises)
- `config set TELEMETRY=on` opt-in: anonymous weekly usage count, no email

**Acceptance:** README + slash + MCP work. HTE landing page links
back from plugin. Zero email capture in plugin.

### Public launch checkpoint

After v0.3.13 ships and the HTE landing page is live, submit to
HN/Reddit with the benchmark as the headline. Single highest-impact
moment. Subsequent phases ship in front of an audience.

### v0.3.14 — Quick-win grounding domains  *[2 days]*

Four small-but-frequent wins:

1. **Tailwind / CSS classes** — index `tailwind.config.*` extensions +
   built-in classes; lint `className="…"`
2. **i18n keys** — index `*.json` / `*.po` / `*.ftl` translation files;
   lint `t('key')` calls
3. **package.json scripts** — read `scripts` map; lint
   `npm run X` / `pnpm X` / `yarn X`
4. **Asset paths** — verify `import x from './path.svg'` resolves
   on disk

### v0.3.15 — Language expansion: PHP, C#, Kotlin, Swift  *[2 days]*

Tree-sitter parsers for four high-volume languages:

- PHP: classes, methods, functions, traits (Laravel/WordPress audience)
- C#: classes, methods, properties, interfaces (.NET enterprise)
- Kotlin: classes, fun, val/var (Android + JVM)
- Swift: classes, structs, protocols, func (iOS)

Each tested against a real project of that stack.

### v0.3.16 — Module import grounding  *[2 days]*

Catch `import { fooBar } from 'lodash'` where lodash doesn't export
`fooBar`.

- Walk `node_modules/<pkg>/package.json` `main`/`exports`
- Parse `*.d.ts` for TypeScript exports
- Python: site-packages walk + `__all__` / `__init__.py`
- Schema: `module_exports` table
- Strategy: `import_grounding`
- Edge cases: default-only, namespace imports, wildcard re-exports

### v0.4.0 — Generalist pivot: markdown grounding + name_registry  *[3-4 days]*

The market-expansion bet.

- Schema: `name_registry` table
- Source: `name_registry.json` / `*.bib` / auto-extracted from chapter 1
- Strategy: `markdown_grounding` walks `.md` / `.txt` / `.docx` for
  proper nouns + defined terms; flags non-registry hits, fuzzy-matches typos
- Lifecycle: first-run intake; `claude-dejavu register-name "Aria,
  the Veilweaver"`
- Marketing: NovelCrafter / Sudowrite community, /r/writing, AcademicTwitter
- Reposition site copy: "Type checker for prose, code, and everything
  in between"

**This is the 10× audience expansion.**

### v0.4.1 — Pro license + editor extensions  *[1 week]*

After v0.4.0 ships, kick off the paid wedge:

- Stripe Checkout integration (HTE side)
- License key generation (signed JWT, offline-verifiable)
- Plugin reads `~/.claude-dejavu/license.key`; gates Pro features
- VS Code extension scaffold (real-time grounding inline) — Pro-only
- GitHub Action template (CI integration) — Pro-only

### v0.4.2 — Hallucination dashboard (local HTML report)  *[3 days]*

Pro-only. Generates a static HTML page with:

- Week-over-week hallucination rate trend
- Top-50 fabricated names (per project, per domain)
- ROI estimate ($ saved in Claude tokens × N rewrites prevented)
- Per-strategy breakdown

### v0.4.2 → v0.5.0a — Performance + quality pass  *[shipping in sequence]*

After the v0.4.1 real-world reindex on `a-large-monorepo` exposed
that a `--force` rebuild took ~10 minutes (vs the ~3 min advertised
earlier), we identified concrete perf + quality wins. Reordered after
v0.4.2 ship to prioritize highest user-impact-per-effort:

  ✅ v0.4.2 — Share one `IgnoreFilter` across all indexers (saved ~150s
              on `--force` reindex, was: rebuilt 11× per reindex)

  📍 v0.4.5 — **mtime cache for non-symbol indexers** (route/page/env/
              db/gql/flag/quickwin) so incrementals touch only changed
              files. Daily UX win: reindex goes from minutes to seconds.

  📍 v0.5.0a — **Tool-use mode** (the largest single hallucination-
              rate quality lift): replace the schema-dump in system
              prompt with a `dejavu_check_name(name, kind?, model?)`
              MCP tool the model is FORCED to call before using a name.
              Token-cheaper, model accuracy higher (forced lookup loop).
              Expected: hallucination rate 5.5% → <1%.

  📍 v0.4.4 — Parallel indexer execution (`multiprocessing.Pool`,
              most are I/O-bound). Cuts full-rebuild wall-clock ~50%.

  📍 v0.5.0 — Full context-aware grounding (see section below).

#### Retired / parked

  ❌ v0.4.3 — Single shared `os.walk` per project, slice per indexer.
              Originally planned to save ~30s of redundant filesystem
              traversal on `--force` reindex. **Retired** because
              v0.4.5 (mtime cache) makes full walks rare in daily use,
              and v0.4.4 (parallel indexers) naturally accepts per-
              worker walks because IPC for shared walk results adds
              more complexity than the ~30s saved. Re-evaluate post-
              v0.4.5 if telemetry shows `--force` is a hot path.

  📦 v0.4.6 — `psycopg2.extras.execute_values` bulk inserts.
              Parked until we measure whether quickwin's ~9k inserts
              actually dominate any perceptible reindex stage post-
              v0.4.5. May get folded into v0.4.4's parallel work.

Aggregate target after v0.4.5 + v0.4.4 ship: incremental reindex
becomes near-instant; `--force` reindex on a 8k-file repo goes from
10 min → ~3 min.

### v0.5.0 — Context-aware grounding (the proactive turn)  *[major release]*

Right now the lint pipeline is reactive: Claude writes everything,
we lint after, surface violations. The v0.5.0 leap is **proactive
context-aware suggestions** — Claude is *given* the right names
*before* generating, scoped to where in the file it's writing.

  - New `code/context_extractor.py`: tree-sitter walker that, given
    a cursor position in a file, classifies the local context:
      inside JSX `className=…` → CSS context
      inside `gql\`…\``         → GraphQL context
      inside `import { … } from 'pkg'` → module-export context for pkg
      inside Prisma `where: { … }`     → DB-field context for that model
      after `process.env.`            → env-var context

  - Framework awareness: detect React vs Angular vs Vue vs Svelte
    once per project (presence of `react`, `@angular/core`, `vue`,
    `svelte` in package.json). Each strategy filters its
    suggestions by framework — Angular projects surface
    `@Component` / `@Injectable` / pipes; React projects surface
    hooks + components.

  - Tool-use mode (replaces context-dump in system prompt):
    instead of pasting the whole schema as preamble, expose a
    `dejavu_check_name(name, kind?, model?)` MCP tool the model
    must call before using a name. Token-cheaper, model accuracy
    higher (forced lookup loop).

Hallucination-rate-reduction techniques landing here:
  A. Stricter "USE EXACT STRINGS — do not paraphrase" instruction
     (fixes p14 regression in the v0.3.12 benchmark)
  B. Few-shot anti-examples in schema digest
  C. Tool-use mode (the big one)
  D. Two-pass refinement: lint Claude's output, send violations
     back as a follow-up message
  E. Embedding-based pre-suggestion (Weaviate finds top-K relevant
     names per prompt; only those go in context — solves the
     "schema digest is too long" issue)

Re-run the v0.3.12 benchmark after v0.5.0 ships. Target headline:
**>95% reduction with tool-use mode**.

### v1.0.0 — `grounding-core` library extraction  *[the open standard play]*

The market-pivot move. Today claude-dejavu is "a Claude Code
plugin." v1.0.0 makes it the *reference implementation of an open
standard* that any LLM coding tool can adopt: Cursor, GitHub
Copilot, Cody, Continue, Aider, JetBrains AI, internal corporate
AI tools.

Architecture:

```
grounding-core/        ← new PyPI package
  ├ indexers/          ← all 13 grounding domains
  ├ strategies/        ← all the lint strategies
  ├ scorer/            ← deterministic hallucination measurement
  ├ context_extractor/ ← v0.5.0's positional context classifier
  └ protocol/          ← JSON-RPC interface — the "Grounding Protocol" (GP)

claude-dejavu/         ← Claude Code wrapper, depends on grounding-core
nexus-grounded/        ← Nexus integration, depends on grounding-core
cursor-grounded/       ← (future, community)
copilot-grounded/      ← (future, community — likely LSP-based)
```

The **Grounding Protocol (GP)** becomes the LSP-of-name-grounding.
Open spec (CC-BY or similar), reference implementation under
FSL-1.1-ALv2 (with each version converting to Apache 2.0 after
two years), brand-agnostic. Companies wanting GP integrated into
their proprietary AI tools hire HTE to implement — fair source +
paid commercial license + paid integration, mirrors how LSP /
Language Server Protocol commercializes.

This is the Tesla-patents-wall move applied to AI grounding. See
the README "Why open-source" section for the philosophy framing.

### v1.x — Hosted cloud (only if Pro hits ~30 paying seats)

Deferred until demand is proven and revenue covers infra.

---

## Future feature ideas (parking lot — not yet sprint-planned)

### Split-session support

When a Claude Code conversation gets too long / heavy / context-bloated,
let the user split it cleanly: claude-dejavu observes the session,
detects the split signal (token count, context depth, manual
`/dejavu-split`), creates two new sessions:

  - "main" continuing the active task
  - "archive" snapshotting everything before the split point

Both sessions are first-class restorable via `claude-dejavu restore`.
The current session's recall index is updated to mark the split point
so future searches surface the right side.

User-noted while running this very session — claude-dejavu's own
build session got large enough that we manually split into a
"technical" thread and a "marketing" thread (see docs/MARKETING.md).
The plugin itself should automate this.

Likely lands in v0.6.x — depends on better session-state introspection
APIs from Anthropic (currently we only see Stop events).

### Other parking-lot ideas

- **Live diff grounding** — when user reviews a diff in `gh pr view`
  or VS Code diff editor, surface "this line introduces 3 hallucinated
  names" inline.
- **Real-time index-as-you-type** — VS Code extension that
  incrementally updates the symbol index as files save, so lint
  results are <100ms.
- **Multi-LLM bench** — extend `bench_hallucination.py` to compare
  Claude vs GPT-5 vs Gemini vs Llama. Position dejavu as
  provider-agnostic.
- **Confidence calibration UI** — show users which names dejavu is
  confident about vs uncertain about, so they trust the suggestions.

---

## Marketing track (parallel to phases 3+)

| Item | When | Owner |
|------|------|-------|
| `hendrikthurau.enterprises/claude-dejavu` landing | Phase 13 | you |
| Public benchmark report | Phase 12 → published in 13 | me draft → you publish |
| HN/Reddit launch post | After Phase 13 | you, with my prep |
| Blog: "Building 8 grounding domains in 14 days" | Phase 13 | me draft → you publish |
| Blog: "We measured Claude's hallucination rate" | Phase 13 | me draft → you publish |
| GitHub stars campaign | continuous | both |
| Conference pitches (PyCon, JSConf, AI Eng World's Fair) | after 1k stars | both |

---

## Cumulative timeline (focused execution)

| Phase | Cumulative days |
|-------|-----------------|
| v0.3.11 | 1 |
| v0.3.12 | 3 |
| v0.3.13 | 4 |
| **public launch** | ↑ |
| v0.3.14 | 6 |
| v0.3.15 | 8 |
| v0.3.16 | 10 |
| v0.4.0 | 14 |

So `v0.4.0` (generalist pivot) ships in ~14 working days.
v0.4.1+ (paid Pro wedge + editor) follows.

---

## What we're NOT doing (yet)

- Heavy cloud infrastructure (Phase 8 deferred until paid demand exists)
- Email lead capture (rejected: spammy, low-conversion)
- Auto-update telemetry without consent (only with explicit opt-in)
- A "Pro upsell" inside the OSS plugin's hot path (subtle credit only)
- Multi-LLM support before benchmark validates Claude case (one fight at a time)

This list is as important as what we ARE doing.
