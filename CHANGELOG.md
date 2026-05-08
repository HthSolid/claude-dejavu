# Changelog

All notable changes to claude-dejavu. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely;
versions track the plugin manifest in `.claude-plugin/plugin.json`.

## [0.6.0] — 2026-05-06

**Cloud embedding tier shipped.** New optional path that routes session
embeddings through a managed cloud embedder instead of
the local CPU container, eliminating multi-thousand-percent CPU spikes
on slow machines during reingest. Same model
(`intfloat/multilingual-e5-large-instruct`), so existing local vectors
are byte-for-byte interchangeable — no re-embedding required when
opting in.

### Added

- **`code/embedding_provider.py`** — pluggable embedding interface with
  three modes selected via `DEJAVU_EMBED_MODE`:
    - `local` (default) — current behavior, calls the t2v-transformers container
    - `cloud` — pure cloud, fails fast if the worker is unreachable
    - `cascade` — cloud primary with optional local fallback (only
      added if local probes reachable, so we never silently corrupt the
      index when the local container is intentionally stopped)
- **`code/cloud_cli.py`** — three new CLI subcommands powering the
  Pro tier onboarding flow:
    - `claude-dejavu login`         — OAuth-style browser flow
    - `claude-dejavu login --paste` — manual paste fallback
    - `claude-dejavu logout`        — server-side revoke + local erase
    - `claude-dejavu status`        — mode + worker + adaptive batch + quota
- **`--retry-vectorize` flag** on `ingester.py` — re-process turns
  stuck with `vectorized=FALSE`. Bypasses cursor dedup; honors
  `DEJAVU_EMBED_MODE` so cloud users push through the worker.
- **`code/migrate_to_cloud.py`** — one-shot Weaviate class migration to
  `vectorizer:none`, preserving 1024-dim vectors byte-for-byte. --dry-run
  by default; --apply to execute.
- **doctor `cloud_embed` check** — surfaces skip / warn / fail / ok with
  worker version, dim, cache hit ratio, quota remaining.

### Fixed

- `_check_weaviate` UnboundLocalError when the `health` import fails.
  ImportError now produces a `skip` status (not a misleading `fail`
  saying "class missing").
- The cascade provider's error chain now surfaces the cloud-side
  error too, not just the local fallback's "container not running"
  message — much faster debugging when the cloud worker is the actual
  failure point.

### Fixed — Windows install reliability

- **`probe_pg` falls back to `docker exec claude-dejavu-postgres
  pg_isready`** when host `pg_isready`/`psql` aren't on PATH. Most
  Windows users don't have the Postgres client installed locally;
  pre-fix the install would silently 60s-timeout in the readiness
  loop reporting
  `no_pg_isready_or_psql_available_for_verification` even though the
  container was actually up. Now the probe asks the container
  directly via `docker exec`, which works on every platform.
- **`_reclaim_stale_named_containers`** runs before `docker compose
  up` to free our canonical container names if a previous (broken)
  install left them owned by a different Compose project. Without
  this, `compose up` errored with
  `Conflict. The container name "claude-dejavu-postgres" is already
  in use by container "abc…"` on every retry. Removes the stale
  container wrapper only — volumes (where the data lives) are
  preserved, so a subsequent `compose up` re-attaches with no data
  loss.
- **Existing-Weaviate detection logs the URL, not the raw meta
  dict** (was printing `{grpcMaxMessageSize: …, hostname: …, …}`).
  Also now verifies the existing Weaviate has the
  `text2vec-transformers` module — if it's a vanilla Weaviate
  without the vectorizer (common when users have an unrelated
  Weaviate from another project), the bundled stack is brought up
  instead so the `ClaudeDejavuTurn` class can actually be created.
- **`claude-dejavu doctor` now works when the venv is missing.**
  Previously the wrapper hard-exited with "venv missing — run
  install.py", giving the user no diagnostic. The wrapper (POSIX
  + Windows .bat) now falls back to system `python` / `py` for
  `doctor` specifically — `code/doctor.py` is stdlib-only at
  module top, and dep-requiring checks already produce `skip`
  rows. The user gets a real layered report telling them exactly
  what's broken (venv = fail, all downstream = skip) instead of
  a one-line "venv missing" stub. POSIX wrapper also dumps the
  `INSTALL_STATUS` marker contents on hard-fail so the user sees
  what the last install attempt failed on.

### Privacy

Cloud mode routes embedding requests through a managed gateway to a
third-party inference provider. The provider does not log content by
default and never trains on customer data; their TOS reserves the
right to briefly log content for debugging. The plugin shows the
provider name + this disclosure on first cloud activation so users
can decide whether to opt in.

For users staying on local mode: nothing changes. The CPU container
path is the default and remains fully supported.

## [0.5.0e] — 2026-05-05

Reliability + observability overhaul. **Closes the silent-ingest-failure
class of bugs** that caused entire sessions to be missing from the
search index without warning.

### Fixed

- **Ingest no longer aborts on NUL-byte rows.** Pre-fix, a single turn
  whose text contained `\x00` (common when tool outputs captured
  binary or truncated-terminal data) raised `ValueError: A string
  literal cannot contain NUL` from `psycopg2.cur.execute()`, aborted
  the whole session ingest, and left the cursor un-advanced so every
  subsequent SessionEnd re-failed at the same byte. Sessions with
  even one NUL byte were silently lost. Fix: `_strip_nul()` recursive
  helper applied at every text boundary (turn content, tool_input,
  gist, file_path) before any PG insert.
- **Per-line try/except in ingest loop.** One bad turn no longer
  aborts the whole session. The loop catches the exception, rolls
  back the aborted PG transaction (otherwise psycopg2 enters
  `InFailedSqlTransaction` state and breaks every subsequent
  statement), advances the cursor past the bad line, and continues.
- **Stop hook on Linux/macOS now uses `start_new_session=True`.**
  Pre-fix only Windows got `DETACHED_PROCESS`; on POSIX the ingester
  child stayed in the parent's process group and got SIGHUP'd when
  Claude Code closed. Long ingests of multi-MB sessions used to die
  at Claude-Code-quit.
- **Cascade reorder for slow machines.** `summarize.py` reads
  `PERF_CLASS=slow` from `config.env` and dynamically reorders the
  provider cascade to put cloud (Anthropic / Gemini / OpenRouter)
  first when local Ollama is too slow. Saves a 30s × N providers
  walk on weak machines.
- **`fh.tell()` replaced with byte-accumulator** for cursor offsets.
  Python's text-mode iterator (`for line in fh`) uses a readahead
  buffer that disables `tell()`. Cursor's `last_byte` now tracks
  `sum(len(line.encode(...)))` so partial-progress saves reflect the
  actual byte offset.
- **Silent `make_gist()` Ollama call removed from ingest hot path.**
  `make_gist` ignored `prefer_llm=False` for assistant text ≥1000
  chars, calling Ollama with a 30s urllib timeout per turn — a
  32 MB session blocked for 45 minutes pre-fix. Ingester now imports
  `rule_gist` directly, never reaches the LLM cascade.

### Added

- **Per-session ingest lock.** `$DATA_ROOT/locks/ingest-<sid>.lock`
  with `O_CREAT|O_EXCL` atomic create + 30-min stale-reaping.
  Prevents two stop-hooks (e.g. from concurrent Claude Code sessions)
  from racing on the same session's PG row locks. Released via outer
  try/finally — guaranteed cleanup on any exit path.
- **Periodic durable progress.** Cursor commits every 500 successful
  turns instead of only at end-of-file. A 32 MB session that crashes
  at line 18,000 now keeps ~17,500 lines of forward progress on the
  next run instead of redoing from scratch.
- **Progress heartbeat.** Stderr line every 250 turns:
  `[ingest] aabbccdd line=12345 bytes=1.5M/10M (15%) +250t 7t/s 32s`.
  Visible during interactive `reingest` and in `ingest.log` for
  detached stop-hook runs. Final completion line always emitted.
- **`claude-dejavu reingest` CLI** for crash recovery. Three modes:
  `--reingest --session SID` (clear cursor + re-ingest one),
  `--reingest --missing-only` (re-ingest only sessions whose .jsonl
  exists on disk but is absent from the index — gap recovery without
  disturbing complete sessions), `--reingest` (full re-scan).
- **`--workers N` parallel reingest.** Each worker has its own PG
  connection. Recovering 10 missing sessions drops from sequential
  ~8 min to parallel ~2 min with `--workers 4`.
- **`claude-dejavu bench`** — measures local Ollama gist latency on
  a 1 KB sample, classifies the machine as `fast` or `slow`,
  persists `PERF_CLASS=...` to `config.env` with `--apply`.
- **`doctor` `_check_perf_class`** — surfaces slow-machine warning
  with fix message pointing at cloud API key setup or Pro tier.
- **`doctor` `_check_ingest_coverage`** — walks
  `~/.claude/projects/`, checks each .jsonl against the
  `ingest_cursors` table, flags sessions present on disk but missing
  from the index. Surfaces `warn` with `claude-dejavu reingest
  --missing-only` as the fix.
- **`error_log` integration in ingester.** Top-level uncaught
  exceptions go to the structured error log via
  `error_log.log_error()`. Combined with stop.py's schedule-failure
  capture, the silent-failure surface is closed — ingester crashes
  show up in `dejavu doctor` output instead of vanishing into
  `ingest.log`.
- **Adversarial test suite** (`tests/test_ingester_adversarial.py`)
  — 23 PG-dependent assertions: NUL bytes mid-stream, malformed JSON
  line, latin-1 / non-UTF-8 bytes, single 200 KB turn, no-network
  mode (OLLAMA_URL unreachable), cursor invariants, real-PG
  concurrency (two ingesters, one defers via lock), observability
  (deliberate failure → error_log record), fault injection (bad
  UUID at a specific line, surrounding good turns must survive).
- **`tests/_deadline.py`** — cross-platform `with deadline(seconds)`
  context manager (SIGALRM on POSIX, watchdog on Windows). Catches
  the entire "blocking call in hot path" regression class.

### Changed

- `code/summarize.py` cascade reordered local-first by default:
  `ollama → claude-cli → anthropic → gemini → openrouter`. On
  `perf_class=slow` machines, runtime flips it to cloud-first.
- `summarize.py` per-provider timeout 60s → 20s. New
  `CASCADE_TIMEOUT_S=60s` total-walk circuit breaker.
- Test count: 168 → **293 infra-free + 23 PG-dependent** (316 total).

### Documentation

- `docs/TROUBLESHOOTING.md` — new sections "Sessions show as missing
  from the index" (with `reingest --missing-only` recipe) and
  "I edited the plugin source but my changes don't take effect"
  (with the `~/.claude/plugins/cache/` refresh recipe).

### Parallel install / health work (also in this release)

- **`scripts/install.py`** — major overhaul (per-service Docker
  probe, streaming compose output, install responsiveness fixes).
- **`code/health.py` + `tests/test_health.py`** — schema +
  Weaviate-class integrity checks with auto-recovery (re-apply
  `schema.sql`, re-create Weaviate class, re-ingest from off-tree
  backup if data loss is detected).
- **`code/migrations/`** — schema-evolution directory + README for
  changes that `CREATE TABLE IF NOT EXISTS` can't express.
- **`bin/dejavu-hook.py`** + **`hooks/session_start.py`** —
  cross-platform hook dispatcher hardening.
- **`docker/docker-compose.minimal.yml`** — minor compose tweaks.

## [0.5.0d] — 2026-05-05

Cross-platform install fix. **Critical for Windows users.**

### Fixed

- Hook commands in `hooks/hooks.json` previously called `python3
  SCRIPT.py` directly. On Windows this fails — the canonical
  command is `python` or `py.exe` and `python3` typically isn't on
  PATH. As a result, every Windows install of v0.5.0c and earlier
  silently failed at the **Setup** hook (which calls `install.py`),
  leaving the plugin "enabled" with no venv, no Postgres / Weaviate
  provisioning, no MCP server, and no `dejavu_*` tools registered.
  All subsequent hooks (SessionStart, Stop, SessionEnd, PreToolUse)
  failed for the same reason.

  Symptom: `/claude-dejavu:dejavu-restore` returns "MCP tool not in
  registry," doctor reports nothing because the MCP server never
  started, and `bin/claude-dejavu-mcp` errors with "bundled venv
  not found." The plugin appears installed but does literally
  nothing.

### Added

- `bin/dejavu-hook.py` — single cross-platform hook dispatcher.
  Takes a hook name (`setup` / `session-start` / `stop` /
  `session-end` / `pre-tool-use`) and re-execs the corresponding
  handler using `sys.executable` so subsequent calls don't depend
  on PATH lookup.

### Changed

- `hooks/hooks.json` — every command now routes through
  `python "${CLAUDE_PLUGIN_ROOT}/bin/dejavu-hook.py" <hook-name>`.
  The canonical `python` (not `python3`) is the only PATH
  requirement, matching the existing `.mcp.json` convention.

### Documentation

- `docs/TROUBLESHOOTING.md` — added a Windows-specific section
  noting that `python` must be on PATH before the Setup hook can
  fire, with the apt-install one-liner for Ubuntu users
  (`apt install python-is-python3`) and the Windows installer
  default behavior.

### Migration for existing Windows installs

If you have an existing v0.5.0c-or-earlier install on Windows that
never completed Setup, refresh the marketplace clone to v0.5.0d
and run `install.py` manually:

```powershell
cd "$env:APPDATA\claude\plugins\marketplaces\claude-dejavu"
git fetch origin main
git reset --hard origin/main

python "$env:APPDATA\claude\plugins\marketplaces\claude-dejavu\scripts\install.py"
```

Then fully restart Claude Code. Future installs will Setup
correctly without manual intervention.

## [0.5.0c] — 2026-05-04

Troubleshooting documentation release. Pure docs — no code changes.

### Added

- `docs/TROUBLESHOOTING.md` — comprehensive install + runtime
  troubleshooting guide. Maps every observed error message to its
  fix command. Covers Docker-not-running flow, install resume,
  PG / Weaviate startup hangs, port conflicts, Windows-specific
  notes (Docker Desktop must be running, WSL2 backend, antivirus
  exclusions), runtime issues (lint not catching fabrications,
  missing slash commands, sessions not mirrored, slow reindex),
  bug-report template, diagnostic command cheat sheet, and a
  nuclear-option full re-install for when all else fails.

- README install section now links to the new troubleshooting
  guide so users find it before they get stuck.

## [0.5.0b] — 2026-05-04

Governance release. Pure docs / project-meta — no code changes.

### Added

- `CONTRIBUTING.md` — development setup, PR conventions, lightweight
  in-project FSL contribution clause.
- `SECURITY.md` — coordinated-disclosure policy with severity-tiered
  response targets. Contact: `dejavu@hendrikthurau.enterprises`.
- `.github/ISSUE_TEMPLATE/bug_report.md` and `feature_request.md` —
  structured templates surfaced when opening a new issue.
- `.github/ISSUE_TEMPLATE/config.yml` — disables blank issues;
  redirects questions to Discussions, security to email, commercial
  licensing to `COMMERCIAL.md`.
- `.github/PULL_REQUEST_TEMPLATE.md` — checklist incl. tests,
  CHANGELOG entry, no telemetry, FSL contribution agreement.
- `.github/FUNDING.yml` — adds the GitHub Sponsors button.

### Changed

- `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`
  descriptions re-anchored to lead with the rigorous **89.3%** number
  (n=400, McNemar p<1e-26) instead of the v0.5.0a-only **99%** number.
  The 99% appears as a hedged "up to" mention with the n=200 caveat.

## [0.5.0a] — 2026-05-04

The hallucination-reduction release. The rigorous core claim
remains the v0.3.12 result: **89.3% relative reduction
(51.5% → 5.5%, n=400 paired trials, McNemar exact p=9.6e-27,
claude-sonnet-4-6).** v0.5.0a adds a 14-strategy deterministic
rewriter cascade; in additional tests (n=200, same prompts and
scoring as v0.3.12) we've measured up to 99% reduction
(51.5% → 0.5%) — that's the upper bound seen so far, not a
stability guarantee. Treat 89.3% as the headline.

### Added

- **`dejavu_check_name(name, kind?, parent?)` MCP tool** — single-
  entry verifier across 13 identifier kinds (env, db-field, db-model,
  route, gql-field, gql-type, flag, css-class, i18n-key,
  module-export, symbol, name, auto). Replaces the in-prompt schema
  dump; scales to projects of any size.
- **14-strategy deterministic rewriter** (`code/rewriter.py`) —
  cascade per scorer-flagged name with per-domain stacks:
  `exact → comment_context → import_gating → sdk_catalog →
  cross_domain → acronym → case_normalize → levenshtein_word →
  trigram → token_set → embedding → project_history → suffix_pattern`.
  Plus a blacklist short-circuit driven by the per-project
  fabrication memory.
- **`dejavu_rewrite_proposed_edit` MCP tool** — runs the cascade on
  an edit before write. Auto-corrects typos via word-boundary
  substitution; accepts SDK-convention names (gated by `package.json`
  deps + import-source check); flags genuine fabrications as
  RESIDUAL with the closest candidates.
- **SDK convention catalog** (`code/sdk_env_catalog.json`) — curated
  map of ~30 packages → canonical env vars (LaunchDarkly / AWS /
  Stripe / Sentry / Redis / Postgres / Next / Datadog / OpenAI /
  Anthropic / Supabase / Twilio / SendGrid / Resend / Vercel / Node).
  Plus an abbreviation map (LD→LAUNCHDARKLY, DD→DATADOG, PG→POSTGRES).
- **SDK catalog auto-scraper** (`scripts/scrape_sdk_envs.py`) —
  fetches READMEs + `.d.ts` files via npm registry / unpkg, or the
  package's GitHub source tree (with `--source=github`). Outputs
  proposed catalog updates as JSON; never auto-merges.
- **Cross-source env index** — `extend_manifest_from_sources()`
  walks `.github/workflows/*.y(a)ml`, `docker-compose.*`,
  `Dockerfile`, `next.config.*`, `vercel.json`, plus AST-light
  scan of `.py / .ts / .tsx / .js` source for `process.env.X` /
  `os.environ['X']` / `os.getenv('X')`. Reduces RESIDUAL rates on
  real projects that declare envs in CI/Compose but not
  `.env.example`.
- **Project history walker** — `project_history_names()` scans the
  last 200 commits of `.env.example` / `flags.json` / etc. for names
  that ever existed. The `strategy_project_history` strategy flags
  re-introduced removed names as regressions.
- **Per-project fabrication memory** — new `fabrication_history`
  table and the `claude-dejavu fabrications` CLI (+ slash command
  `/dejavu-fabrications`). Records every RESIDUAL/REPLACE outcome
  per `(project_slug, name, domain)`. Three triage states: `open` /
  `declared` / `blacklisted`. Blacklisted names short-circuit the
  cascade.
- **`.env.example` synthesis** — `dejavu_propose_env_addition` MCP
  tool + `claude-dejavu env propose <NAME>` CLI. Dry-run preview
  by default; `--apply` writes the new line + an inline comment
  naming the SDK convention.
- **Runtime env-trap generator** — `claude-dejavu generate
  runtime-trap [--language=ts|python]`. Emits a typed `env`
  accessor (TS: Zod + Proxy + `DeclaredEnvKey` literal-union;
  Python: `_EnvProxy` + frozenset + `DejavuUndeclaredEnvError <:
  KeyError`) that throws on undeclared key access at runtime. See
  [`docs/RUNTIME_TRAP.md`](docs/RUNTIME_TRAP.md).
- **Centralized error log** — `code/error_log.py`. Append-only JSON
  Lines at the platform's data dir (honors
  `CLAUDE_DEJAVU_DATA_ROOT` for sandbox isolation). Auto-rotated
  at 5 MB. New `claude-dejavu logs` CLI + `dejavu_logs` MCP tool +
  `/dejavu-logs` slash command. Uncaught-exception hook installed
  at MCP server entry.
- **Comprehensive test suite** — `tests/run_all.py` aggregates
  `test_strategies.py` (46) + `test_helpers.py` (59) +
  `test_error_log.py` (14) + `test_runtime_trap.py` (26) +
  `test_scraper.py` (10) + `test_v05_followups.py` (13) =
  **168 infra-free assertions** covering every strategy, helper,
  and code path. Plus `tests/smoke.py` step 28 covering the new
  MCP surfaces (smoke total: **175/175** with PG + Weaviate up).
- **Documentation** — [`docs/REWRITER.md`](docs/REWRITER.md)
  (architecture + all 14 strategies + per-domain stacks) and
  [`docs/RUNTIME_TRAP.md`](docs/RUNTIME_TRAP.md) (TS + Python
  generators, two-layer defense, refresh workflow).

### Changed

- `dejavu doctor` now reports the error-log status (path, level
  breakdown, last-24h count). Status is `ok` when no errors are
  present, `warn` when recent errors exist; never `fail` on
  diagnostic-log activity.
- Indexer error sites in `recall.py` (env / db / route / graphql /
  flag / page / quickwin / module / name_registry / semantic) now
  emit BOTH stderr (existing UX) AND a structured `error_log`
  record with full traceback. Real failures are now persistable
  and forwardable.

### Fixed

- `error_log` honors `CLAUDE_DEJAVU_DATA_ROOT` so sandbox-isolated
  test runs no longer pollute the user's real
  `~/.local/share/claude-dejavu/errors.log`.
- `project_history_names`: regex previously matched a stripped diff
  line against a pattern expecting `+/-` prefix; the strategy never
  fired on real history. Now correctly extracts removed names from
  `git log -p` output.

### Performance

- Same as v0.4.5 — fingerprint cache from prior release continues
  to make incremental reindexes near-instant.
- Rewriter cascade overhead per name: <1 ms typical (in-process
  pure Python; no DB roundtrip unless fabrication_memory is wired).

### Languages

12 languages indexed for symbol grounding: Python, TypeScript, TSX,
JavaScript, Go, Rust, Java, Ruby, Bash, PHP, C#, Kotlin. Swift is
deferred until upstream `tree-sitter-languages` ships a working
parser.

---

## [0.4.5] — 2026-04-30

Performance release. Fingerprint-based skip cache for non-symbol
indexers — incremental reindexes that previously walked the full
project tree now skip when nothing has changed.

### Added

- `indexer_fingerprints` table: per-project per-indexer
  `(file_count, max_mtime_ns, sum_size_bytes)` cache.
- `--force` flag on reindex bypasses the cache.
- Smoke step 27 covers cache hits, CSS-edit invalidation, and
  `--force` bypass.

---

## [0.4.2] — 2026-04-22

Performance pass — share one `IgnoreFilter` across all 11 indexers
instead of constructing 11 separately. Saves ~150 s per `--force`
reindex on large repos.

---

## [0.4.1] — 2026-04-14

Empirical hallucination benchmark — Claude hallucinations cut
89.3% (n=400, p<1e-26). The v0.3.12 measurement that anchors
v0.5.0a's apples-to-apples claim. Full report:
[`docs/benchmarks/hallucination-rate-v0.3.12.md`](docs/benchmarks/hallucination-rate-v0.3.12.md).

---

## [0.4.0] — 2026-04-10

Language expansion + module imports + prose name registry. Adds
PHP / C# / Kotlin tree-sitter visitors (12 languages total),
`module_exports` indexing for npm `import { … } from "pkg"`
grounding, and a name-registry strategy for prose projects
(novels, screenplays, technical docs).

---

## Earlier versions

See git history for v0.3.x and earlier. Public release line begins
at v0.3.0 (initial publish).
