-- Migration 0001: repos registry + doc embedding cursor + workspace
-- column rename (slugs → repo UUIDs).
--
-- Forward-only; idempotent via IF EXISTS / IF NOT EXISTS guards.

-- ─── repos registry ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS repos (
  id              UUID PRIMARY KEY,
  label           TEXT,
  last_seen_path  TEXT,
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_repos_last_seen_path ON repos(last_seen_path);

-- ─── doc_file_state cursor (drives Stop-hook diff scan) ────────────
CREATE TABLE IF NOT EXISTS doc_file_state (
  repo_id       UUID NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
  file_path     TEXT NOT NULL,
  mtime         BIGINT NOT NULL,
  content_hash  TEXT NOT NULL,
  chunk_count   INT NOT NULL,
  embedded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (repo_id, file_path)
);
CREATE INDEX IF NOT EXISTS idx_doc_file_state_repo ON doc_file_state(repo_id);

-- ─── workspaces.projects → workspaces.repo_ids ─────────────────────
-- Rename only if the old column exists. Idempotent on re-apply.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='workspaces' AND column_name='projects'
  ) THEN
    ALTER TABLE workspaces RENAME COLUMN projects TO repo_ids;
  END IF;
END $$;

-- Old GIN index referenced the old column name. Drop + recreate.
DROP INDEX IF EXISTS idx_workspaces_projects;
CREATE INDEX IF NOT EXISTS idx_workspaces_repo_ids
  ON workspaces USING gin (repo_ids);
