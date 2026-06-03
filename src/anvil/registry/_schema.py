"""SQLite DDL for the installation-level operational registry (V1, 4 tables).

Mirrors the roadmap's Milestone 0.5 schema: projects, repos, runs, leases.

Safety properties enforced at the database layer (not just in application code):
- ``repos.path`` is UNIQUE, so one physical repo cannot be registered under two
  ids and thereby bypass repo-level lease conflict detection.
- Partial UNIQUE indexes guarantee at most one *active* ``file_write`` lease per
  (repo, normalized path) and at most one *active* ``merge_queue`` lease per
  repo, regardless of races between connections.
- Foreign keys are declared and enforced (PRAGMA set at connection time).
"""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
  project_id   TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  config_path  TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
  repo_id         TEXT PRIMARY KEY,
  path            TEXT NOT NULL UNIQUE,
  default_branch  TEXT NOT NULL,
  config_path     TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id              TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL REFERENCES projects(project_id),
  repo_id             TEXT NOT NULL REFERENCES repos(repo_id),
  task_scope_id       TEXT,
  lifecycle_state     TEXT NOT NULL DEFAULT 'created',
  pipeline_state      TEXT NOT NULL DEFAULT 'INIT',
  mode                TEXT NOT NULL DEFAULT 'pending_risk_assessment',
  task_summary        TEXT,
  base_commit         TEXT NOT NULL,
  target_branch       TEXT NOT NULL,
  target_head_at_start TEXT NOT NULL,
  current_target_head TEXT,
  worktree_path       TEXT,
  branch              TEXT,
  initiated_by        TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  finalized_at        TEXT
);

CREATE TABLE IF NOT EXISTS leases (
  lease_id              TEXT PRIMARY KEY,
  lease_type            TEXT NOT NULL,
  repo_id               TEXT NOT NULL REFERENCES repos(repo_id),
  run_id                TEXT NOT NULL REFERENCES runs(run_id),
  work_order_id         TEXT,
  scope                 TEXT NOT NULL,
  access                TEXT NOT NULL,
  status                TEXT NOT NULL DEFAULT 'active',
  acquired_at           TEXT NOT NULL,
  expires_at            TEXT,
  released_at           TEXT,
  override_approved_by  TEXT,
  override_reason       TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo_id);
CREATE INDEX IF NOT EXISTS idx_leases_repo_status ON leases(repo_id, status);

-- At most one ACTIVE file_write lease per (repo, normalized path).
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_file_write
  ON leases(repo_id, scope)
  WHERE status = 'active' AND lease_type = 'file_write';

-- At most one ACTIVE merge_queue lease per repo.
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_merge_queue
  ON leases(repo_id)
  WHERE status = 'active' AND lease_type = 'merge_queue';
"""
