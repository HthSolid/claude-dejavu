-- =============================================================================
-- claude_dejavu — structured audit log for Claude Code sessions
-- =============================================================================
-- Run as: psql -h <host> -p <port> -U <user> -d claude_dejavu_<linux-user> -f schema.sql
--
-- Multi-user isolation: each Linux user gets a dedicated database
-- (claude_dejavu_$USER) so cross-user reads are impossible by default.
-- Override with --shared at install time for trusted teams.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ─── Workspaces ──────────────────────────────────────────────────────────────
-- Named groups of project slugs for shared recall (default scope unit).
CREATE TABLE IF NOT EXISTS workspaces (
  name        TEXT PRIMARY KEY,
  description TEXT,
  projects    TEXT[] NOT NULL DEFAULT '{}',
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_workspaces_projects ON workspaces USING gin (projects);

-- One row per Claude Code session (parent .jsonl).
CREATE TABLE IF NOT EXISTS sessions (
  id              UUID PRIMARY KEY,                 -- session UUID = jsonl filename
  project_path    TEXT NOT NULL,                    -- decoded absolute path or "_orphan"
  project_slug    TEXT,                             -- short name (last segment of project path)
  os_user         TEXT,                             -- Linux $USER who owns this session
  started_at      TIMESTAMPTZ,
  ended_at        TIMESTAMPTZ,
  ai_title        TEXT,
  total_turns     INT DEFAULT 0,
  total_tokens_in BIGINT DEFAULT 0,
  total_tokens_out BIGINT DEFAULT 0,
  ingested_at     TIMESTAMPTZ DEFAULT now(),
  jsonl_path      TEXT,
  jsonl_size      BIGINT,
  jsonl_sha256    TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_os_user ON sessions(os_user);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_slug);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);

-- One row per turn (user msg, assistant msg, tool_use, tool_result).
CREATE TABLE IF NOT EXISTS turns (
  id              BIGSERIAL PRIMARY KEY,
  session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  idx             INT  NOT NULL,                    -- order within session
  parent_uuid     TEXT,                             -- jsonl parentUuid for threading
  message_uuid    TEXT,                             -- jsonl message uuid
  role            TEXT NOT NULL,                    -- 'user' | 'assistant' | 'system' | 'tool_result'
  ts              TIMESTAMPTZ,
  content         TEXT,                             -- main text payload (for user/assistant text blocks)
  content_type    TEXT,                             -- 'text' | 'tool_use' | 'tool_result' | 'thinking' | 'image'
  tokens_in       INT,
  tokens_out      INT,
  model           TEXT,
  vectorized      BOOLEAN DEFAULT FALSE,            -- true if pushed to Weaviate
  weaviate_id     UUID,                             -- corresponding Weaviate object id
  gist            TEXT,                             -- compressed ~80-char summary for cheap MCP returns
  gist_method     TEXT,                             -- 'rule' | 'llm' | NULL (not yet generated)
  UNIQUE(session_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_turns_session_idx ON turns(session_id, idx);
CREATE INDEX IF NOT EXISTS idx_turns_role ON turns(role);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts DESC);
CREATE INDEX IF NOT EXISTS idx_turns_content_trgm ON turns USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_turns_content_fts ON turns USING gin (to_tsvector('english', coalesce(content,'')));

-- One row per tool invocation by the assistant.
CREATE TABLE IF NOT EXISTS tool_calls (
  id              BIGSERIAL PRIMARY KEY,
  turn_id         BIGINT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
  session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  tool_use_id     TEXT,                             -- Claude's tool_use_id
  tool_name       TEXT NOT NULL,
  tool_input      JSONB,
  tool_output     TEXT,                             -- truncated for huge outputs
  output_size     INT,                              -- full size before truncation
  is_error        BOOLEAN DEFAULT FALSE,
  duration_ms     INT,
  ts              TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tool_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_name ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_error ON tool_calls(is_error) WHERE is_error;
CREATE INDEX IF NOT EXISTS idx_tool_input_gin ON tool_calls USING gin (tool_input);

-- One row per file touched by Edit/Write/MultiEdit (extracted from tool_input).
CREATE TABLE IF NOT EXISTS file_touches (
  id              BIGSERIAL PRIMARY KEY,
  tool_call_id    BIGINT NOT NULL REFERENCES tool_calls(id) ON DELETE CASCADE,
  session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  file_path       TEXT NOT NULL,
  op              TEXT NOT NULL,                    -- 'write' | 'edit' | 'multi_edit'
  ts              TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_files_path ON file_touches(file_path);
CREATE INDEX IF NOT EXISTS idx_files_session ON file_touches(session_id);

-- ─── v0.2: LLM-distilled session summaries (claude-mem parity) ───────────────
-- Generated at SessionEnd by hooks/session_end.py via a local LLM (Ollama qwen).
-- Cheap, dense, queryable. Multiple summaries per session allowed (one per
-- major checkpoint or compact event).
CREATE TABLE IF NOT EXISTS session_summaries (
  id              BIGSERIAL PRIMARY KEY,
  session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  prompt_number   INT,                                -- which user-prompt boundary triggered this summary
  request         TEXT,                               -- what the user was trying to do
  investigated    TEXT,                               -- what claude looked at / explored
  learned         TEXT,                               -- non-obvious findings / facts surfaced
  completed       TEXT,                               -- what got done in this slice
  next_steps      TEXT,                               -- what's queued / pending
  notes           TEXT,                               -- free-form leftovers
  model           TEXT,                               -- LLM that generated it (e.g. qwen2.5-coder:3b)
  generated_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON session_summaries(session_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_summaries_request_fts ON session_summaries USING gin (to_tsvector('english', coalesce(request,'') || ' ' || coalesce(learned,'')));

-- ─── v0.2: typed observations (claude-mem parity) ────────────────────────────
-- One row per noteworthy moment in a session. Extracted from <observation>
-- tags + "Decision:" / "Learned:" / "Discovery:" markers + LLM classification.
DO $$ BEGIN
  CREATE TYPE observation_type AS ENUM ('bugfix','feature','decision','discovery','change','refactor','question');
EXCEPTION
  WHEN duplicate_object THEN NULL;
END $$;
CREATE TABLE IF NOT EXISTS observations (
  id              BIGSERIAL PRIMARY KEY,
  session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  turn_id         BIGINT REFERENCES turns(id) ON DELETE SET NULL,
  type            observation_type NOT NULL,
  title           TEXT NOT NULL,
  subtitle        TEXT,
  facts           TEXT[],                             -- discrete facts (JSON array of strings)
  files_touched   TEXT[],                             -- files mentioned / referenced
  ts              TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_observations_session ON observations(session_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_observations_type ON observations(type);
CREATE INDEX IF NOT EXISTS idx_observations_title_trgm ON observations USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_observations_files ON observations USING gin (files_touched);

-- Cursor table: tracks ingestion progress per session for incremental updates.
CREATE TABLE IF NOT EXISTS ingest_cursors (
  session_id      UUID PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  last_line       BIGINT NOT NULL DEFAULT 0,
  last_byte       BIGINT NOT NULL DEFAULT 0,
  last_run_at     TIMESTAMPTZ DEFAULT now()
);

-- Convenience view: prompts only.
CREATE OR REPLACE VIEW user_prompts AS
SELECT t.id, t.session_id, s.project_slug, t.idx, t.ts, t.content
FROM turns t
JOIN sessions s ON s.id = t.session_id
WHERE t.role = 'user' AND t.content_type = 'text'
ORDER BY t.ts DESC NULLS LAST;

-- Convenience view: errors with context.
CREATE OR REPLACE VIEW errors_with_context AS
SELECT
  tc.id        AS tool_call_id,
  s.project_slug,
  s.id         AS session_id,
  tc.tool_name,
  tc.tool_input,
  left(tc.tool_output, 1000) AS error_preview,
  tc.ts
FROM tool_calls tc
JOIN sessions s ON s.id = tc.session_id
WHERE tc.is_error
ORDER BY tc.ts DESC NULLS LAST;

-- ─── v0.3.0: code symbol grounding ───────────────────────────────────────────
-- Indexed symbols per project. One row per definition (function, class, method,
-- top-level variable, type, interface, enum, const). Methods reference their
-- enclosing class via parent_symbol_id.
CREATE TABLE IF NOT EXISTS symbols (
  id               BIGSERIAL PRIMARY KEY,
  project_slug     TEXT NOT NULL,
  file_path        TEXT NOT NULL,                       -- absolute path on disk
  language         TEXT NOT NULL,                       -- 'python','typescript','javascript','go','rust','java','c','cpp','ruby','bash'
  kind             TEXT NOT NULL,                       -- 'function','class','method','variable','type','interface','enum','const'
  name             TEXT NOT NULL,                       -- e.g. 'parseUserInput'
  qualified_name   TEXT,                                -- e.g. 'utils.parseUserInput' or 'UserService.parseInput'
  signature        TEXT,                                -- e.g. 'def parseUserInput(s: str) -> User'
  start_line       INT,
  end_line         INT,
  parent_symbol_id BIGINT REFERENCES symbols(id) ON DELETE CASCADE,
  doc              TEXT,                                -- leading docstring/comment if extractable
  indexed_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_symbols_project_name ON symbols(project_slug, name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_lang ON symbols(language);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_name_trgm ON symbols USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified_trgm ON symbols USING gin (coalesce(qualified_name,'') gin_trgm_ops);

-- Per-file index state. Used for incremental re-index: skip files whose
-- mtime + size haven't changed since last scan.
CREATE TABLE IF NOT EXISTS symbol_file_state (
  file_path        TEXT PRIMARY KEY,
  project_slug     TEXT NOT NULL,
  language         TEXT NOT NULL,
  mtime_ns         BIGINT,                              -- st_mtime_ns at last scan
  size_bytes       BIGINT,
  sha256           TEXT,                                -- only when content actually parsed (cheap dedupe)
  symbol_count     INT DEFAULT 0,
  parser_error     TEXT,                                -- last parse error, NULL if clean
  indexed_at       TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_symbol_file_project ON symbol_file_state(project_slug);

-- Correction outcomes. Storage-only in v0.3.0; v0.3.3 reads this for adaptive
-- thresholds, v0.3.4 derives anonymized telemetry tuples from it.
-- session_id is a soft reference (no FK) — outcomes can be recorded outside an
-- active ingested session.
CREATE TABLE IF NOT EXISTS correction_outcomes (
  id               BIGSERIAL PRIMARY KEY,
  ts               TIMESTAMPTZ DEFAULT now(),
  session_id       UUID,                                -- nullable, no FK
  project_slug     TEXT,
  language         TEXT,                                -- 'python','typescript',...
  mode             TEXT,                                -- 'lookup'|'lint'|'warn'|'suggest'|'autofix'
  proposed_name    TEXT,                                -- what Claude wrote
  resolved_name    TEXT,                                -- what we suggested (NULL if no candidate)
  edit_distance    INT,                                 -- Levenshtein
  trigram_sim      REAL,                                -- pg_trgm similarity
  vector_sim       REAL,                                -- Weaviate cosine (NULL if not used)
  candidate_count  INT,                                 -- how many candidates considered
  pattern_tags     TEXT[],                              -- ['camel_to_snake','one_char_typo','verb_swap',...]
  outcome          TEXT,                                -- 'accepted'|'rejected'|'unknown'|'no_followup'
  outcome_signal   TEXT,                                -- how outcome was inferred
  build_after      TEXT                                 -- 'green'|'red'|'unknown'
);
CREATE INDEX IF NOT EXISTS idx_outcomes_ts ON correction_outcomes(ts DESC);
CREATE INDEX IF NOT EXISTS idx_outcomes_lang_mode ON correction_outcomes(language, mode);
CREATE INDEX IF NOT EXISTS idx_outcomes_session ON correction_outcomes(session_id);

-- ─── v0.3.2.1: API route grounding ───────────────────────────────────────────
-- One row per HTTP route declared in the project. Sources include both
-- code-side detection (Express / FastAPI / Next.js / NestJS / Flask /
-- Django / Hono / Fastify / Koa) and OpenAPI / Swagger files. OpenAPI
-- takes precedence as the maintainer's declared truth; code-side
-- detection fills in undocumented routes.
CREATE TABLE IF NOT EXISTS api_routes (
  id                 BIGSERIAL PRIMARY KEY,
  project_slug       TEXT NOT NULL,
  framework          TEXT NOT NULL,    -- 'express'|'fastify'|'koa'|'hono'|'nestjs'
                                       -- |'nextjs-pages'|'nextjs-app'|'fastapi'
                                       -- |'flask'|'django'|'openapi'
  source             TEXT NOT NULL,    -- 'openapi'|'code'  (precedence + auditability)
  method             TEXT NOT NULL,    -- 'GET'|'POST'|'PUT'|'PATCH'|'DELETE'|'OPTIONS'|'HEAD'|'*'
  path_pattern       TEXT NOT NULL,    -- normalized form: '/api/users/:id'
  raw_path           TEXT NOT NULL,    -- original literal as written in code/spec
  handler_symbol_id  BIGINT REFERENCES symbols(id) ON DELETE SET NULL,
  source_file        TEXT NOT NULL,    -- file where the route was declared
  start_line         INT,
  end_line           INT,
  description        TEXT,             -- from OpenAPI summary/description, JSDoc, or docstring
  indexed_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_routes_project_method ON api_routes(project_slug, method);
CREATE INDEX IF NOT EXISTS idx_routes_path_trgm ON api_routes USING gin (path_pattern gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_routes_raw_path_trgm ON api_routes USING gin (raw_path gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_routes_source_file ON api_routes(source_file);
CREATE INDEX IF NOT EXISTS idx_routes_framework ON api_routes(framework);

-- Path/query/body/header parameters per route. Lets the lint catch
-- "called /users/:id with body={'foo': ...} but the route expects
-- body={'name': ..., 'email': ...}" mismatches in v0.3.2.x+ work.
CREATE TABLE IF NOT EXISTS api_route_params (
  id                 BIGSERIAL PRIMARY KEY,
  route_id           BIGINT NOT NULL REFERENCES api_routes(id) ON DELETE CASCADE,
  param_name         TEXT NOT NULL,
  located_in         TEXT NOT NULL,   -- 'path'|'query'|'body'|'header'|'cookie'
  type_hint          TEXT,            -- 'string'|'integer'|'array<User>'| ...
  required           BOOLEAN DEFAULT FALSE,
  description        TEXT
);
CREATE INDEX IF NOT EXISTS idx_route_params_route ON api_route_params(route_id);

-- Per-file index state for routes (independent of symbols' state since
-- a single file can declare many routes via different frameworks).
CREATE TABLE IF NOT EXISTS api_route_file_state (
  file_path          TEXT PRIMARY KEY,
  project_slug       TEXT NOT NULL,
  framework          TEXT NOT NULL,
  source             TEXT NOT NULL,    -- 'openapi'|'code'
  mtime_ns           BIGINT,
  size_bytes         BIGINT,
  sha256             TEXT,
  routes_count       INT DEFAULT 0,
  parser_error       TEXT,
  indexed_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_route_file_project ON api_route_file_state(project_slug);

-- ─── v0.3.5: page / file-route duplicate prevention ─────────────────────────
-- One row per declared frontend page or file-system-routed handler. Used
-- by the page_duplicate lint strategy to flag Claude proposing a NEW page
-- when one already exists for the same goal.
--
-- Coverage: Next.js (pages router + app router), React Router (config),
-- Vue Router (config), Remix, SvelteKit, Astro. Future: Solid Start,
-- Qwik, TanStack Router, Hono PageRouter.
CREATE TABLE IF NOT EXISTS page_routes (
  id                 BIGSERIAL PRIMARY KEY,
  project_slug       TEXT NOT NULL,
  framework          TEXT NOT NULL,    -- 'nextjs-pages'|'nextjs-app'|'react-router'
                                       -- |'vue-router'|'remix'|'sveltekit'|'astro'
  path_pattern       TEXT NOT NULL,    -- normalized: '/dashboard/profile/:id'
  raw_path           TEXT NOT NULL,    -- original literal as written
  kind               TEXT NOT NULL,    -- 'page'|'layout'|'api'|'middleware'|'loader'|'error'
  source_file        TEXT NOT NULL,
  start_line         INT,
  description        TEXT,             -- best-effort: page title / leading comment
  indexed_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_page_routes_project ON page_routes(project_slug);
CREATE INDEX IF NOT EXISTS idx_page_routes_pattern_trgm ON page_routes USING gin (path_pattern gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_page_routes_framework ON page_routes(framework);
CREATE INDEX IF NOT EXISTS idx_page_routes_source_file ON page_routes(source_file);

CREATE TABLE IF NOT EXISTS page_route_file_state (
  file_path          TEXT PRIMARY KEY,
  project_slug       TEXT NOT NULL,
  framework          TEXT NOT NULL,
  mtime_ns           BIGINT,
  size_bytes         BIGINT,
  sha256             TEXT,
  pages_count        INT DEFAULT 0,
  parser_error       TEXT,
  indexed_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_page_file_project ON page_route_file_state(project_slug);

-- ─── v0.3.6: env var grounding ──────────────────────────────────────────────
-- One row per *declaration* of an environment variable. The env_grounding
-- lint strategy resolves `process.env.X` / `os.getenv("X")` /
-- `Deno.env.get("X")` / `import.meta.env.X` against this table to flag
-- typos and fabricated names before they hit disk.
--
-- 'source' tiers (decreasing authority):
--   env-example   .env.example / .env.template / .env.sample (canonical)
--   zod-schema    a zod / valibot / envalid env validator declares the var
--   docker-compose  environment: block in compose YAML
--   github-actions  env: block in workflow YAML
--   dotenv-runtime  .env.local / .env.development / .env.production
--   source-read   process.env.X read in source code (lowest authority — read
--                 somewhere ≠ declared anywhere; surfaces as low_confidence)
CREATE TABLE IF NOT EXISTS env_var_decls (
  id                 BIGSERIAL PRIMARY KEY,
  project_slug       TEXT NOT NULL,
  name               TEXT NOT NULL,
  source             TEXT NOT NULL,
  source_file        TEXT NOT NULL,
  start_line         INT,
  default_value      TEXT,
  description        TEXT,             -- comment line above the declaration
  is_secret          BOOLEAN DEFAULT FALSE,  -- KEY/TOKEN/SECRET/PASSWORD heuristic
  indexed_at         TIMESTAMPTZ DEFAULT now(),
  UNIQUE(project_slug, name, source, source_file)
);
CREATE INDEX IF NOT EXISTS idx_env_vars_project ON env_var_decls(project_slug);
CREATE INDEX IF NOT EXISTS idx_env_vars_name ON env_var_decls(name);
CREATE INDEX IF NOT EXISTS idx_env_vars_name_trgm ON env_var_decls USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_env_vars_source ON env_var_decls(source);

-- ─── v0.3.7: database schema grounding ───────────────────────────────────────
-- Indexes models + fields from canonical schema sources so the
-- db_grounding lint strategy can ground:
--   prisma.user.findUnique({ where: { emaill: ... } })  ← typo of email
--   db.select().from(usrs)                              ← typo of users
--   pg.query("SELECT email FROM postss")                ← typo of posts
-- Sources: Prisma `model X { ... }` blocks, Drizzle `pgTable(...)`,
--          TypeORM @Entity decorators, raw SQL CREATE TABLE migrations.
CREATE TABLE IF NOT EXISTS db_models (
  id                 BIGSERIAL PRIMARY KEY,
  project_slug       TEXT NOT NULL,
  model_name         TEXT NOT NULL,    -- 'User' (Prisma model) | 'users' (Drizzle const) | 'AuthUser' (TypeORM entity)
  source             TEXT NOT NULL,    -- 'prisma'|'drizzle'|'typeorm'|'sequelize'|'mongoose'|'sql-migration'
  source_file        TEXT NOT NULL,
  start_line         INT,
  table_name         TEXT,             -- explicit DB table name if mapped (e.g. @@map("users"))
  description        TEXT,             -- leading comment / docstring
  indexed_at         TIMESTAMPTZ DEFAULT now(),
  UNIQUE(project_slug, model_name, source, source_file)
);
CREATE INDEX IF NOT EXISTS idx_db_models_project ON db_models(project_slug);
CREATE INDEX IF NOT EXISTS idx_db_models_name ON db_models(model_name);
CREATE INDEX IF NOT EXISTS idx_db_models_name_trgm ON db_models USING gin (model_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_db_models_source ON db_models(source);

CREATE TABLE IF NOT EXISTS db_fields (
  id                 BIGSERIAL PRIMARY KEY,
  model_id           BIGINT NOT NULL REFERENCES db_models(id) ON DELETE CASCADE,
  project_slug       TEXT NOT NULL,    -- denormalized for cheap fuzzy lookups across models
  model_name         TEXT NOT NULL,    -- denormalized — same reason
  field_name         TEXT NOT NULL,
  field_type         TEXT,             -- 'String', 'Int', 'DateTime', 'Json', 'Decimal', 'text', 'integer', etc.
  is_required        BOOLEAN DEFAULT TRUE,
  is_id              BOOLEAN DEFAULT FALSE,
  is_unique          BOOLEAN DEFAULT FALSE,
  is_relation        BOOLEAN DEFAULT FALSE,
  related_model      TEXT,             -- target model name for relation fields
  start_line         INT,
  description        TEXT
);
CREATE INDEX IF NOT EXISTS idx_db_fields_model ON db_fields(model_id);
CREATE INDEX IF NOT EXISTS idx_db_fields_project ON db_fields(project_slug);
CREATE INDEX IF NOT EXISTS idx_db_fields_name ON db_fields(field_name);
CREATE INDEX IF NOT EXISTS idx_db_fields_name_trgm ON db_fields USING gin (field_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_db_fields_modelname ON db_fields(model_name);

-- ─── v0.3.8: GraphQL schema grounding ────────────────────────────────────────
-- Indexes types + fields from .graphql / .gql SDL files (canonical) and
-- inline `gql\`type X { … }\`` template literals so the graphql_grounding
-- lint strategy can ground operations like:
--    gql`query { user(id: 1) { emaill } }`        -> typo of email
--    gql`mutation { createUer(...) { id } }`     -> typo of createUser
--    gql`{ totallyFakeField { id } }`            -> fabricated
CREATE TABLE IF NOT EXISTS graphql_types (
  id                 BIGSERIAL PRIMARY KEY,
  project_slug       TEXT NOT NULL,
  type_name          TEXT NOT NULL,    -- 'User', 'Query', 'Mutation', 'CreateUserInput'
  kind               TEXT NOT NULL,    -- 'object'|'input'|'enum'|'interface'|'union'|'scalar'|'schema'
  source             TEXT NOT NULL,    -- 'sdl-file'|'inline-gql'|'codegen'
  source_file        TEXT NOT NULL,
  start_line         INT,
  description        TEXT,
  indexed_at         TIMESTAMPTZ DEFAULT now(),
  UNIQUE(project_slug, type_name, source, source_file)
);
CREATE INDEX IF NOT EXISTS idx_gql_types_project ON graphql_types(project_slug);
CREATE INDEX IF NOT EXISTS idx_gql_types_name ON graphql_types(type_name);
CREATE INDEX IF NOT EXISTS idx_gql_types_name_trgm ON graphql_types USING gin (type_name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS graphql_fields (
  id                 BIGSERIAL PRIMARY KEY,
  type_id            BIGINT NOT NULL REFERENCES graphql_types(id) ON DELETE CASCADE,
  project_slug       TEXT NOT NULL,
  type_name          TEXT NOT NULL,
  field_name         TEXT NOT NULL,
  field_type         TEXT,             -- 'String!', '[User!]!', 'Int', etc.
  args_json          JSONB,            -- field arguments as [{name, type, default?}]
  is_nullable        BOOLEAN DEFAULT TRUE,
  is_list            BOOLEAN DEFAULT FALSE,
  start_line         INT,
  description        TEXT
);
CREATE INDEX IF NOT EXISTS idx_gql_fields_type ON graphql_fields(type_id);
CREATE INDEX IF NOT EXISTS idx_gql_fields_project ON graphql_fields(project_slug);
CREATE INDEX IF NOT EXISTS idx_gql_fields_name ON graphql_fields(field_name);
CREATE INDEX IF NOT EXISTS idx_gql_fields_name_trgm ON graphql_fields USING gin (field_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_gql_fields_typename ON graphql_fields(type_name);

-- ─── v0.3.10: feature flag grounding ─────────────────────────────────────────
-- Catches `client.variation('flag-key', user)`, `isEnabled('flag-key')`,
-- `checkGate('gate_key')`, etc. where the key isn't declared in any
-- known config (LaunchDarkly, Unleash, GrowthBook, custom flags.json).
CREATE TABLE IF NOT EXISTS feature_flags (
  id                 BIGSERIAL PRIMARY KEY,
  project_slug       TEXT NOT NULL,
  flag_key           TEXT NOT NULL,    -- the canonical key string used at the call site
  source             TEXT NOT NULL,    -- 'flags-json'|'feature-flags-ts'|'launchdarkly'|'unleash'|'growthbook'|'statsig'|'posthog'|'source-read'
  source_file        TEXT NOT NULL,
  start_line         INT,
  default_value      TEXT,             -- JSON-stringified default if known
  description        TEXT,
  is_kill_switch     BOOLEAN DEFAULT FALSE,  -- KEY contains "kill"|"emergency"|"disable" heuristic
  indexed_at         TIMESTAMPTZ DEFAULT now(),
  UNIQUE(project_slug, flag_key, source, source_file)
);
CREATE INDEX IF NOT EXISTS idx_flags_project ON feature_flags(project_slug);
CREATE INDEX IF NOT EXISTS idx_flags_key ON feature_flags(flag_key);
CREATE INDEX IF NOT EXISTS idx_flags_key_trgm ON feature_flags USING gin (flag_key gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_flags_source ON feature_flags(source);

-- ─── v0.3.10: adaptive threshold cache ───────────────────────────────────────
-- Stores the per-project, per-channel optimal trigram threshold so future
-- resolves use a tuned value instead of the default 0.4. Recomputed
-- whenever correction_outcomes accumulate ≥ N new rows for a channel.
CREATE TABLE IF NOT EXISTS resolver_thresholds (
  id                 BIGSERIAL PRIMARY KEY,
  project_slug       TEXT NOT NULL,
  channel            TEXT NOT NULL,    -- 'symbol'|'route'|'page'|'env'|'db'|'graphql'|'flag'
  threshold          REAL NOT NULL,    -- 0.0..1.0
  outcomes_observed  INT DEFAULT 0,
  precision_at_top   REAL,             -- how often top-1 was accepted
  recall_at_top3     REAL,             -- how often correct match was in top-3
  last_tuned_at      TIMESTAMPTZ DEFAULT now(),
  UNIQUE(project_slug, channel)
);
CREATE INDEX IF NOT EXISTS idx_thresholds_project ON resolver_thresholds(project_slug);

-- ─── v0.3.14: quick-win grounding domains ────────────────────────────────────

-- CSS / Tailwind class names. Catches `className="rouded-lg"` typos.
CREATE TABLE IF NOT EXISTS css_classes (
  id            BIGSERIAL PRIMARY KEY,
  project_slug  TEXT NOT NULL,
  class_name    TEXT NOT NULL,
  source        TEXT NOT NULL,    -- 'tailwind-builtin'|'tailwind-config'|'css-module'|'global-css'
  source_file   TEXT NOT NULL DEFAULT '',
  start_line    INT,
  indexed_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE(project_slug, class_name, source, source_file)
);
CREATE INDEX IF NOT EXISTS idx_css_classes_project ON css_classes(project_slug);
CREATE INDEX IF NOT EXISTS idx_css_classes_name ON css_classes(class_name);
CREATE INDEX IF NOT EXISTS idx_css_classes_name_trgm ON css_classes USING gin (class_name gin_trgm_ops);

-- i18n keys. Catches `t('user.profle.name')` typos.
CREATE TABLE IF NOT EXISTS i18n_keys (
  id            BIGSERIAL PRIMARY KEY,
  project_slug  TEXT NOT NULL,
  key           TEXT NOT NULL,
  locale        TEXT,             -- 'en', 'de', etc., or NULL for shared
  source        TEXT NOT NULL,    -- 'json'|'po'|'ftl'|'yaml'
  source_file   TEXT NOT NULL,
  start_line    INT,
  default_value TEXT,
  indexed_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE(project_slug, key, locale, source_file)
);
CREATE INDEX IF NOT EXISTS idx_i18n_project ON i18n_keys(project_slug);
CREATE INDEX IF NOT EXISTS idx_i18n_key ON i18n_keys(key);
CREATE INDEX IF NOT EXISTS idx_i18n_key_trgm ON i18n_keys USING gin (key gin_trgm_ops);

-- ─── v0.3.16: module export grounding ────────────────────────────────────────
-- Catches `import { fooBar } from 'lodash'` where lodash doesn't export fooBar.
CREATE TABLE IF NOT EXISTS module_exports (
  id            BIGSERIAL PRIMARY KEY,
  project_slug  TEXT NOT NULL,
  package_name  TEXT NOT NULL,    -- 'lodash', '@radix-ui/react-dialog'
  export_name   TEXT NOT NULL,    -- 'debounce', 'Dialog', 'default'
  kind          TEXT,             -- 'function'|'class'|'type'|'const'|'default'|'namespace'
  source_file   TEXT,
  indexed_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE(project_slug, package_name, export_name)
);
CREATE INDEX IF NOT EXISTS idx_modexp_project ON module_exports(project_slug);
CREATE INDEX IF NOT EXISTS idx_modexp_pkg ON module_exports(package_name);
CREATE INDEX IF NOT EXISTS idx_modexp_export_trgm ON module_exports USING gin (export_name gin_trgm_ops);

-- ─── v0.4.0: name registry (prose / generalist grounding) ────────────────────
-- Catches typos in proper nouns, defined terms, citation keys, character /
-- place / concept names — anything that should be consistent across a
-- structured creative or professional document.
CREATE TABLE IF NOT EXISTS name_registry (
  id            BIGSERIAL PRIMARY KEY,
  project_slug  TEXT NOT NULL,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL,    -- 'character'|'place'|'term'|'citation'|'concept'|'brand'|'party'|'other'
  aliases       TEXT[] DEFAULT '{}',
  description   TEXT,
  source        TEXT NOT NULL,    -- 'name-registry-json'|'name-registry-yaml'|'bibtex'|'auto-extracted'
  source_file   TEXT,
  start_line    INT,
  indexed_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE(project_slug, name, kind)
);
CREATE INDEX IF NOT EXISTS idx_namereg_project ON name_registry(project_slug);
CREATE INDEX IF NOT EXISTS idx_namereg_name ON name_registry(name);
CREATE INDEX IF NOT EXISTS idx_namereg_name_trgm ON name_registry USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_namereg_kind ON name_registry(kind);

-- ─── v0.4.5: indexer fingerprint cache ───────────────────────────────────────
-- Project-level skip cache. Each indexer records the count + max-mtime + sum-
-- size of the files it considers "relevant"; on next run, if the fingerprint
-- matches, the indexer skips itself entirely. Saves multi-minute walks on
-- repos where nothing has changed since the last reindex.
CREATE TABLE IF NOT EXISTS indexer_fingerprints (
  project_slug    TEXT NOT NULL,
  indexer_kind    TEXT NOT NULL,    -- 'env'|'db'|'graphql'|'flag'|'quickwin'|'route'|'page'|'registry'|'module'
  file_count      INT,              -- # of relevant files at last run
  max_mtime_ns    BIGINT,           -- max(mtime_ns) across those files
  sum_size_bytes  BIGINT,           -- sum(size) across those files (catches edits that don't change mtime)
  cached_stats    JSONB,            -- the stats dict from last run, returned verbatim on cache hit
  last_run_at     TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (project_slug, indexer_kind)
);

-- ─── v0.5.0a: per-project fabrication memory ─────────────────────────────────
-- The rewriter records every name it could not resolve deterministically
-- (RESIDUAL outcomes) so future encounters of the same name can short-circuit
-- the cascade and surface to the user faster. After N fabrications of the
-- same name, the user sees "this keeps appearing — declare it or blacklist."
CREATE TABLE IF NOT EXISTS fabrication_history (
  project_slug    TEXT NOT NULL,
  name            TEXT NOT NULL,
  domain          TEXT NOT NULL,    -- 'env'|'db'|'route'|'graphql'|'flag'|'symbol'|'other'
  count           INT NOT NULL DEFAULT 1,
  first_seen_at   TIMESTAMPTZ DEFAULT now(),
  last_seen_at    TIMESTAMPTZ DEFAULT now(),
  -- Triage state set by the user via `dejavu fabrications` CLI:
  --   'open'         (default — not yet triaged)
  --   'declared'     (user added it to .env.example / schema / etc.)
  --   'blacklisted'  (intentional reject — model keeps proposing wrong name)
  status          TEXT NOT NULL DEFAULT 'open',
  notes           TEXT,
  PRIMARY KEY (project_slug, name, domain)
);
CREATE INDEX IF NOT EXISTS idx_fabrication_project ON fabrication_history(project_slug);
CREATE INDEX IF NOT EXISTS idx_fabrication_status ON fabrication_history(project_slug, status);
CREATE INDEX IF NOT EXISTS idx_fabrication_count ON fabrication_history(project_slug, count DESC);
