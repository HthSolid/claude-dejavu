# Changelog

All notable changes to claude-dejavu. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely;
versions track the plugin manifest in `.claude-plugin/plugin.json`.

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
