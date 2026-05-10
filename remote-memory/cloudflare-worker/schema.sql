CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY,
  content_redacted TEXT NOT NULL,
  summary TEXT NOT NULL,
  tags TEXT NOT NULL DEFAULT '[]',
  scope TEXT NOT NULL CHECK (scope IN ('global', 'project')),
  project_id TEXT NOT NULL,
  repo_url TEXT NOT NULL DEFAULT '',
  source_agent TEXT NOT NULL DEFAULT 'unknown',
  source_surface TEXT NOT NULL DEFAULT 'api',
  sensitivity TEXT NOT NULL DEFAULT 'internal',
  dedupe_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  expires_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_entries_scope_project_created
  ON entries(scope, project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entries_dedupe
  ON entries(dedupe_hash);
CREATE INDEX IF NOT EXISTS idx_entries_source_agent
  ON entries(source_agent);
