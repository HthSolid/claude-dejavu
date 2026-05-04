# claude-dejavu

> **Stop Claude from fabricating names. Restore deleted sessions. Search every past conversation.**
>
> Built by [HTE Switzerland](https://hendrikthurau.enterprises) — a digital solutions partner shipping production AI infrastructure, full-stack development, and cloud-grade engineering.

[![CI](https://github.com/HthSolid/claude-dejavu/actions/workflows/ci.yml/badge.svg)](https://github.com/HthSolid/claude-dejavu/actions/workflows/ci.yml)
[![Hallucinations cut 89.3%](https://img.shields.io/badge/Hallucinations-cut%2089.3%25%20%28n%3D400%29-brightgreen.svg)](docs/benchmarks/hallucination-rate-v0.3.12.md)
[![License: FSL-1.1-ALv2](https://img.shields.io/badge/License-FSL--1.1--ALv2-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org)
[![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey.svg)](#install)

---

## What claude-dejavu does

Every Claude API call costs tokens. Every fabricated name (a made-up env var, a wrong field on a Prisma model, a typo of a route, a fictional GraphQL field) ends in a rewrite — and a second API call to fix the first one. claude-dejavu catches those fabrications BEFORE they hit disk.

**Twelve languages, eight grounding domains:** symbols (Python, TypeScript, TSX, JavaScript, Go, Rust, Java, Ruby, Bash, PHP, C#, Kotlin), HTTP routes, frontend pages, env vars (with an SDK convention catalog covering LaunchDarkly / AWS / Stripe / Sentry / Redis / Postgres / Next / Datadog / OpenAI / Anthropic / Supabase / …), DB schema (Prisma / Drizzle / TypeORM / SQL migrations), GraphQL (SDL + inline `gql`), feature flags (LaunchDarkly / Unleash / GrowthBook / flags.json), plus an LSP wire-protocol passthrough for type-check ground truth.

**89.3% measured reduction in hallucinated identifiers.** On a 400-trial controlled benchmark against `claude-sonnet-4-6` (51.5% → 5.5%, McNemar exact p=9.6e-27, 95% CI 82.7–95.1%), claude-dejavu's grounding cuts fabricated names by 89.3%. In additional tests with v0.5.0a's 14-strategy deterministic rewriter cascade (n=200, same prompts and scoring), we've measured **up to 99% reduction** (51.5% → 0.5%) — that's the upper bound seen so far, not a stability guarantee. Treat 89.3% as the rigorous headline. See [docs/REWRITER.md](docs/REWRITER.md) for the rewriter architecture and [docs/benchmarks/](docs/benchmarks/) for both reports.

**Plus session recovery.** Claude Code stores every conversation in `~/.claude/projects/<encoded>/<uuid>.jsonl`. Those files **can be deleted, truncated, or otherwise lost** — by Claude, by a stray `git clean`, by manual cleanup. claude-dejavu mirrors them so deletion never costs you the conversation. Semantic search across every past chat means _"have we discussed X before?"_ now has an answer.

Both ship as a Claude Code plugin AND a standalone CLI:

### 1. Recover deleted sessions

Every Claude Code Stop event mirrors the live `.jsonl` to an off-tree location. When a session disappears from `~/.claude/projects/`, the next time Claude Code runs you see this:

> ⚠ claude-dejavu: 3 session(s) missing from `~/.claude/projects/`. Run `claude-dejavu restore --list` to see all.

Then one command brings them back, byte-perfect, with a runnable `claude --resume` command:

```bash
$ claude-dejavu restore --list
Restorable sessions (3):

  deleted    aaaaaaaa-1111-0000-0000-000000000001  myproject  2026-04-15  124.7 MB
  deleted    aaaaaaaa-2222-0000-0000-000000000002  myproject  2026-04-08   25.3 MB
  deleted    aaaaaaaa-3333-0000-0000-000000000003  other-app  2026-04-02    0.9 MB

$ claude-dejavu restore --all
  ✓ aaaaaaaa-1111-…  (124.7 MB)  → ~/.claude/projects/-home-…/aaaaaaaa-1111-….jsonl
     resume: cd ~/projects/myproject && claude --resume aaaaaaaa-1111-…
  ✓ aaaaaaaa-2222-…  ( 25.3 MB)
  ✓ aaaaaaaa-3333-…  (  0.9 MB)
Restored 3/3 session(s).
```

### 2. Search across every past conversation

Hybrid BM25 + vector search across **every turn** of every past session. By default returns ~80-token gists; expand specific turns on demand to keep token cost low.

```bash
$ claude-dejavu search "auth refresh token rotation"
scope=ws:my-stack  hits=2
  [1] turn=1842 score=0.84 myproject/aaaaaaaa-1111 2026-04-15
       Refresh-token rotation: store hash, single-use, 5min grace window for clock skew…
  [2] turn=2104 score=0.61 backend/bbbbbbbb-2222 2026-04-12
       JWT exp 15m + refresh 30d, rotation on every use, revocation list in Redis…
```

Claude can call this automatically through the bundled MCP server, so when you ask *"have we discussed auth?"* mid-session, it grounds itself in your actual past conversations instead of inventing context.

### 3. Auditable history

```bash
$ claude-dejavu errors --tool=Bash --limit=5    # all Bash failures across sessions
$ claude-dejavu files src/auth/                  # which sessions touched this dir
$ claude-dejavu sessions --scope=global          # everything you've ever worked on
```

Postgres schema captures `sessions`, `turns`, `tool_calls`, `file_touches`. Trigram + full-text indexes for instant "what did Claude run last week?" queries.

### 4. Session summaries + typed observations *(v0.2)*

When a Claude Code session ends, claude-dejavu auto-summarizes it into structured
fields (`request / investigated / learned / completed / next_steps`) using `claude -p`
(no API key needed — inherits your existing Claude Code auth) and extracts typed
observations (bugfix / decision / discovery / feature / change / refactor / question).

When you start a fresh conversation in the same project, claude-dejavu injects a
token-disciplined preamble:

```
[claude-dejavu — context from prior sessions on this project]
  last request: refactor the auth refresh-token rotation
  completed:    JWT exp 15m + refresh 30d, rotation on every use, revocation list in Redis
  next steps:   wire the revocation list to the gateway middleware
  recent observations:
    [decision] adopt 5-min grace window for clock skew
    [bugfix]   refresh token leaked in error logs (sanitizer added)
```

LLM provider cascade: `claude -p` → Anthropic SDK → Gemini → OpenRouter → Ollama →
rule-based fallback. Whichever you have configured wins; degrades gracefully.

---

## How it differs from other Claude Code memory plugins

| Plugin | Storage | Granularity | Search | Session summaries | Restore deleted? | License |
|---|---|---|---|:-:|:-:|---|
| `claude-mem` | SQLite | LLM summaries | vector (chroma) | ✓ | ✗ | AGPL-3.0 |
| `claude-recall-plugin` | files | mid-session context | n/a | ✗ | ✗ | MIT |
| `claude-historian` | files | session metadata | basic | ✗ | ✗ | varies |
| `claude-archivist` | sqlite | session metadata | search | partial | partial | varies |
| **`claude-dejavu`** | **PG + Weaviate** | **raw turns + summaries** | **hybrid BM25+vector + PG-FTS** | **✓ (v0.2)** | **✓** | **FSL-1.1-ALv2** |

claude-dejavu now ships **claude-mem's three killer features under a
fair-source license** — LLM session summaries, typed observations,
SessionStart context injection (v0.2) — plus its own unique
deletion-recovery + lossless-audit features.

For corporate teams whose legal policy forbids AGPL-3.0's copyleft
contagion, dejavu's FSL is the clean answer: internal use is unrestricted,
no source-disclosure obligation, and each version converts to Apache 2.0
two years after release. Commercial license available for use cases that
fall outside FSL's Permitted Purpose — see [COMMERCIAL.md](COMMERCIAL.md).

See [docs/VS_CLAUDE_MEM.md](docs/VS_CLAUDE_MEM.md) for measured latency,
recall, and response richness numbers from a head-to-head benchmark.

---

## Install

### One prerequisite: Docker

claude-dejavu auto-provisions Postgres + Weaviate via Docker. Install it
first, then everything below is automatic.

| OS | Install command |
|---|---|
| **macOS** | `brew install --cask docker` then launch Docker Desktop |
| **Windows** | `winget install Docker.DockerDesktop` then launch Docker Desktop |
| **Linux** | `curl -fsSL https://get.docker.com \| sudo sh && sudo usermod -aG docker $USER` |

(If you already have your own Postgres + Weaviate running, the plugin will
detect them on common ports — Docker is only required for the bundled stack.)

### Recommended: Claude Code marketplace

```
/plugin marketplace add HthSolid/claude-dejavu
/plugin install claude-dejavu
```

That's it. The plugin's `Setup` hook fires automatically on install and
runs the installer end-to-end — auto-provisions Postgres + Weaviate via
Docker, applies the schema, creates a per-user database, bootstraps a
Python venv with all dependencies, and wires the MCP server.

**First-install takes 1-5 minutes** while Docker pulls the Weaviate +
transformers images. Subsequent sessions are instant.

If anything goes wrong (Docker not running, port conflict, etc.), the
SessionStart hook reads the install marker and tells Claude **in chat**
exactly what to fix — no terminal output to dig through. Plus you can
always run:

```
/dejavu-doctor          # diagnose every layer + see one-shot fix instructions
/dejavu-config list     # see all settings + current values
```

Updates flow through the marketplace — the SessionStart hook checks
`git ls-remote` for new tags and prints a one-line notice when a newer
version is available; `claude plugin update claude-dejavu` applies it
(Setup hook re-runs to pick up any new dependencies).

### Alternative: standalone (no Claude Code plugin)

For CI environments, dev forks, or distros without the `claude` CLI:

```bash
git clone https://github.com/HthSolid/claude-dejavu
cd claude-dejavu
python3 scripts/install.py --register-mcp
```

`--register-mcp` writes a manual `claude-dejavu` entry to
`~/.claude/settings.json` so Claude Code can find the MCP server without
the plugin path. **Don't use `--register-mcp` if you've already installed
via the marketplace** — it'll create a duplicate registration. The
standalone installer also detects existing marketplace installs and skips
its own MCP registration in that case.

### I have nothing — guide me through it

See **[docs/INSTALL.md](docs/INSTALL.md)** for the complete platform-by-platform
guide. Covers macOS, Linux (Ubuntu / Fedora / Arch), Windows native, and
Windows + WSL2. Assumes zero prior Docker / Postgres knowledge.

### What the Setup hook actually does

1. **Verifies Docker is installed AND running** — three-state probe
   (absent / daemon-down / ok). If Docker Desktop is installed but not
   started, you'll see "Launch Docker Desktop" in the Claude chat,
   not a buried terminal error.
2. **Detects** Postgres + Weaviate on common ports, with shape verification
   (a SearXNG on port 8889 won't pose as Weaviate). If nothing is found,
   **auto-provisions** them via the bundled `docker-compose.minimal.yml`.
3. Creates a per-Linux-user database `claude_dejavu_$USER` (use `--shared` for
   trusted teams).
4. Bootstraps a Python venv and installs `psycopg2-binary`, `mcp`,
   `tree-sitter==0.21.3`, and `tree-sitter-languages==1.10.2`.
5. Normalizes `.mcp.json` + `hooks/hooks.json` to the OS-correct python
   command (`python3` on Linux/macOS, `python` on Windows).
6. Symlinks the `claude-dejavu` CLI into `~/.local/bin/` (or
   `%LOCALAPPDATA%\Microsoft\WindowsApps\claude-dejavu.bat` on Windows).
7. **No systemd, no launchd, no Task Scheduler** — snapshots are Stop-hook-driven.

The installer is **cross-platform**: Linux, macOS, Windows (native + WSL).
All hooks, the MCP server, and the CLI are pure Python.

### Slash commands available after install

| Command | What it does |
|---|---|
| `/dejavu-search <query>` | Hybrid (BM25 + vector) search across past sessions |
| `/dejavu-resume <hint>` | Find the best matching past session + ready-to-run resume command |
| `/dejavu-sessions` | List recent sessions in current scope |
| `/dejavu-restore` | List/restore deleted sessions from off-tree backup |
| `/dejavu-symbols <name>` | Resolve a code symbol against the project's index |
| `/dejavu-lint <file>` | Lint a proposed edit, flag fabricated names + typos |
| `/dejavu-reindex` | (Re)build the symbol index for the current project |
| `/dejavu-outcomes` | Inspect the correction_outcomes log |
| `/dejavu-doctor` | Diagnose install + runtime health |
| `/dejavu-config` | Read/change settings (lint mode, scope defaults, …) |

---

## How it works

```
                   ┌─────────────────────────────┐
   Claude Code ─►  │  Stop hook (pure Python)    │
   .jsonl write    │                             │
                   │  • mirror to off-tree dir   │  ← always (free safety net)
                   │  • incremental ingest       │  ← async (PG + Weaviate)
                   │  • throttled snapshot 5min  │  ← rsync --link-dest
                   │  • deletion-detection       │  ← warn user via systemMessage
                   └────────────┬────────────────┘
                                │
        ┌───────────────────────┴────────────────────────┐
        │                                                │
   ┌────▼──────────┐    Postgres                  Weaviate
   │ sessions      │    • per-user isolation     • text2vec-transformers
   │ turns         │    • trigram + BM25 + FTS   • multi-tenant per Linux user
   │ tool_calls    │    • workspaces table       • hybrid BM25 + vector
   │ file_touches  │
   └───────────────┘
                                │
                                ▼
                ┌──────────────────────────────────┐
                │  CLI + MCP server                │
                │  hybrid query, two-pass design   │
                │  scope = workspace ⊇ project     │
                └──────────────────────────────────┘
```

### Storage layout

```
$XDG_DATA_HOME/claude-dejavu/         # Linux:    ~/.local/share/claude-dejavu/
                                       # macOS:    ~/Library/Application Support/claude-dejavu/
                                       # Windows:  %LOCALAPPDATA%\claude-dejavu\
├── chat-logs/<encoded-project>/<uuid>.jsonl  # off-tree mirror, 1 file per session
├── snapshots/<timestamp>/                     # 5-min hardlink snapshots (rsync --link-dest)
│   └── latest -> 20260427-230945
├── config.env
├── ingest.log
└── venv/
```

Postgres lives in your existing instance (or the bundled Docker compose if you don't have one).

---

## Privacy + scoping

Designed for **privacy by default** without you having to think about it:

- **Multi-user**: separate Postgres database + Weaviate tenant per Linux/macOS/Windows user. No cross-user reads. Override with `--shared` only for trusted teams.
- **Cross-repo**: default scope is the **workspace** containing the current repo (or just the repo if no workspace defined). A contractor on multiple unrelated client projects never sees data leak between them by accident.
- **Token discipline**: search returns ~80-token gists by default. Claude expands specific turns on demand. Saves 10–20× tokens vs. dumping full content.
- **Off-tree by default**: nothing critical lives inside your project repo. `git clean -xfd` cannot delete it.

---

## Hardware tiers (embedding model)

Pick a model based on your machine. Real CPU benchmarks on a 4,548-turn corpus:

| Tier | Default model | Short p50 | Recall@5 | MRR |
|---|---|---:|---:|---:|
| **light** (<8 GB RAM) | MiniLM-L6-v2 (384-dim) | **43 ms** | 0.56 | 0.52 |
| **normal** English | bge-small-en-v1.5 (384-dim) | 138 ms | 0.78 | 0.60 |
| **multilingual light** | multilingual-e5-small (384-dim) | 72 ms | 0.67 | 0.53 |
| **best-recall (CPU)** | nomic-embed-text-v1.5 (768-dim) | 300 ms | **0.89** | **0.67** |
| **strong (GPU)** | multilingual-e5-large-instruct (1024-dim) | varies | — | — |

`scripts/install.py` detects RAM/GPU/Docker and picks a sensible default. Override with `--model=<choice>`. See [docs/MODEL_BENCHMARK.md](docs/MODEL_BENCHMARK.md) for methodology.

---

## CLI quick reference

```bash
# Recovery (the headline feature)
claude-dejavu restore --list                    # show what's restorable
claude-dejavu restore <uuid> [...]              # restore specific session(s)
claude-dejavu restore --all                     # restore everything missing
claude-dejavu restore --since 7d                # only recent

# Search & navigate
claude-dejavu search "<query>" [--scope=...]    # hybrid search
claude-dejavu expand <turn_id> [...]            # full content of specific turns
claude-dejavu resume "<hint>"                   # find best session + resume command
claude-dejavu sessions                          # list recent sessions
claude-dejavu errors [--tool=X]                 # tool failures
claude-dejavu files <path-substring>            # which sessions touched these

# Workspaces (named groups for shared recall across tightly-coupled repos)
claude-dejavu workspace list
claude-dejavu workspace create my-stack --projects=repo1,repo2,repo3
claude-dejavu workspace add my-stack repo4

# Hallucination grounding (v0.5.0a — see docs/REWRITER.md)
claude-dejavu env propose <NAME> [--package=<sdk>] [--apply]
                                                # declare a new env var (after dejavu sees an SDK convention
                                                # the project hasn't declared yet)
claude-dejavu fabrications [--status=open]      # list names the rewriter has flagged; mark each
                                                # as declared / blacklisted after triage
claude-dejavu fabrications --mark NAME DOMAIN STATUS
claude-dejavu generate runtime-trap [--language=ts|python]
                                                # emit a typed env accessor that throws on undeclared keys

# Diagnostics
claude-dejavu doctor                            # checks PG/Weaviate/index/error-log status
claude-dejavu logs [--component=...] [--with-traceback]
                                                # plugin-internal error log (rotates at 5 MB)
```

---

## Belt-and-suspenders: kernel-level append-only protection

For users who want extra defense beyond mirror-based recovery:

| OS | Command |
|---|---|
| Linux ext4 | `sudo chattr +a ~/.claude/projects/<encoded>/*.jsonl` |
| macOS APFS | `sudo chflags uappnd ~/.claude/projects/<encoded>/*.jsonl` |
| Windows NTFS | `icacls <path> /deny "*S-1-1-0:(D)"` |

Append-only allows Claude to keep writing new turns but rejects truncation/deletion at the kernel level. claude-dejavu documents this — it doesn't apply it by default (needs admin privileges).

---

## Contributing

Issues and PRs welcome. Run the smoke test before opening a PR:

```bash
PG_HOST=localhost PG_PORT=5432 WV_URL=http://localhost:8080 \
  python3 tests/smoke.py
```

CI runs the same suite on Linux × Python 3.10/3.11/3.12 plus best-effort macOS / Windows jobs.

---

## Attribution

claude-dejavu builds on excellent open-source work:

- **PostgreSQL** ([PostgreSQL License](https://www.postgresql.org/about/licence/)) — the structured audit store
- **Weaviate** ([BSD-3-Clause](https://github.com/weaviate/weaviate/blob/main/LICENSE)) — the vector store with hybrid search
- **Anthropic MCP SDK** ([MIT](https://github.com/modelcontextprotocol/python-sdk)) — the in-session protocol
- **psycopg2-binary** (LGPL with linking exception) — Postgres client
- **sentence-transformers** ([Apache-2.0](https://github.com/UKPLab/sentence-transformers)) — used in the benchmark harness
- **HuggingFace embedding models** — see [docs/MODEL_BENCHMARK.md](docs/MODEL_BENCHMARK.md) for the list and their respective licenses

"Claude Code" and "Claude" are trademarks of Anthropic, PBC. claude-dejavu is an independent third-party plugin and is not affiliated with or endorsed by Anthropic.

---

## Why open-source

In 2014, Tesla took down the wall of patents in their Palo Alto lobby. Elon Musk wrote: *"They have been removed, in the spirit of the open source movement, for the advancement of electric vehicle technology."*

claude-dejavu is in that spirit — for the advancement of AI that doesn't hallucinate. Knowledge that took years to gather, I'm releasing to the world. The next decade of software depends on this problem getting solved, and locking the answer just slows everyone down.

I have plenty more projects like this in the drawer. With a little funding, they'll all see daylight too.

---

## Who built this

claude-dejavu is built by **[HTE Switzerland](https://hendrikthurau.enterprises)**.

We're not an outsourcing shop — we're the engineering team founders call when something has to *actually work*.
If you have a startup, no CTO, and a roadmap that needs serious engineering, that's our wheelhouse. Reach out at **info@hendrikthurau.enterprises** or [hendrikthurau.enterprises](https://hendrikthurau.enterprises). The plugin you're reading is what that quality looks like.

### Want to support the work?

claude-dejavu is fair-source under FSL-1.1-ALv2 — fully functional, free for any Permitted Purpose, forever. If you want to help fund the next ten projects:

- **[GitHub Sponsors](https://github.com/sponsors/HthSolid)** — one-off or monthly, native to where the code is
- Or just **star the repo + tell a founder who needs this**

A future **Pro tier** (editor extensions, CI integration, hallucination dashboard, custom grounding domains for your DSL) will fund the next phases. The core stays free under FSL, fully functional, with each version automatically converting to Apache 2.0 two years after release.

---

## License & commercial use

claude-dejavu is licensed under the **Functional Source License, Version 1.1, ALv2 Future License** ([FSL-1.1-ALv2](LICENSE)).

**Free, no questions asked, for:**
- Individual developers, freelancers, students
- Internal corporate use (any company size — on your codebase, in your CI, on your developer machines)
- Open-source projects
- Non-commercial research and teaching
- Professional services delivered to a licensee using claude-dejavu under the same terms

Each version automatically becomes Apache 2.0 two years after release.

**A commercial license is required if you want to:**
- Bundle claude-dejavu into a commercial product or service you sell
- Host it as a managed service for third parties
- Build a product that competes with our commercial offering

If any of that applies — or if your legal team simply prefers a traditional commercial license — we're easy to work with. We don't expect Fortune 500s to use our work for free while we get nothing, and we don't expect indie devs to pay. Drop a line: **dejavu@hendrikthurau.enterprises**. See [COMMERCIAL.md](COMMERCIAL.md) for details.
