"""Anvil local runtime registry (Milestone 0.5).

The :class:`Registry` owns the installation-level *operational* state in SQLite
(projects, repos, runs, leases) and coordinates the on-disk layout: per-entity
JSON configs, per-run directories with controller-state JSON, and per-run git
worktrees.

Design split (from the roadmap):
- SQLite  → operational queries: status, lease conflicts, lifecycle.
- JSON    → durable, reviewable per-entity / per-run config and pipeline state.

Safety is enforced at the database layer where possible (UNIQUE repo path,
partial UNIQUE indexes for active leases) so concurrent connections cannot race
past application-level checks. Application code adds friendly diagnostics,
run↔repo binding, lifecycle gating, and audit metadata on top.

This milestone deliberately stops short of the Milestone 1 controller: there is
no state-machine execution, no baseline capture, no agents. ``create_run`` exists
as the library primitive those later milestones build on, and is exercised here
through worktree allocation, leases, pause/resume, and drift detection.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .. import gitutils
from ..config import (
    InstallationConfig,
    ProjectConfig,
    RepoConfig,
    TaskScope,
)
from ..errors import (
    AlreadyExistsError,
    LeaseConflictError,
    NotFoundError,
    NotInitializedError,
    StateTransitionError,
    ValidationError,
)
from ..paths import AnvilPaths, default_home
from ..timeutil import iso_after_seconds, now_iso
from ._schema import SCHEMA_SQL

# ID validation patterns, consistent with the Milestone 0 entity schemas.
_PROJECT_ID_RE = re.compile(r"^proj-[A-Za-z0-9._-]+$")
_REPO_ID_RE = re.compile(r"^repo-[A-Za-z0-9._-]+$")
_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_RUN_ID_RE = re.compile(r"^RUN-[0-9]{8}-[0-9]{3,}$")

DEFAULT_MODE = "pending_risk_assessment"

# Lifecycle states (operational). Distinct from pipeline_state (progress).
LIFECYCLE_STATES = {
    "created",
    "active",
    "paused",
    "blocked",
    "waiting_for_human",
    "finalized",
    "aborted",
    "archived",
}
_TERMINAL_STATES = {"finalized", "aborted", "archived"}

# Centralized, strict lifecycle transition table. A transition is allowed only
# if the target state is in the source state's allowed set (mirrors the roadmap
# lifecycle diagram). This blocks resurrecting terminal runs.
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    # Roadmap flow: created -> active -> paused -> active -> finalized.
    "created": {"active", "aborted"},
    "active": {"paused", "blocked", "waiting_for_human", "finalized", "aborted"},
    "paused": {"active", "aborted"},
    "blocked": {"active", "aborted"},
    "waiting_for_human": {"active", "aborted"},
    "finalized": {"archived"},
    "aborted": set(),
    "archived": set(),
}
# States a run may be resumed FROM (back to active). 'created' is excluded —
# a never-started run is activated via activate_run, not resume_run.
_RESUMABLE_STATES = {"paused", "blocked", "waiting_for_human"}

# Lifecycle states in which a run may hold/acquire leases. A never-started
# 'created' run has done no work and must not lock repo paths; terminal runs
# have released theirs. Leases are acquired while active and retained across the
# paused-like states.
_LEASE_HOLDING_STATES = {"active", "paused", "blocked", "waiting_for_human"}

# Valid lease release statuses (anything other than 'active'). release_lease and
# friends must not write arbitrary status strings.
_LEASE_RELEASE_STATUSES = {
    "released",
    "overridden",
    "force_released",
    "aborted",
    "finalized",
}

LEASE_TYPES = {"file_write", "merge_queue"}
DEFAULT_LEASE_TTL_SECONDS = 4 * 60 * 60  # 4 hours, per the roadmap


@dataclass
class DriftResult:
    """Structured result of a base-commit drift check (detection only)."""

    base_commit: str
    target_branch: str
    target_head_at_start: str
    target_head_current: str
    base_is_stale: bool
    rebase_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_commit": self.base_commit,
            "target_branch": self.target_branch,
            "target_head_at_start": self.target_head_at_start,
            "target_head_current": self.target_head_current,
            "base_is_stale": self.base_is_stale,
            "rebase_required": self.rebase_required,
        }


def normalize_lease_path(path: str) -> str:
    """Normalize a repo-relative lease path, rejecting unsafe forms.

    Leases conflict on the *same normalized path*. To make that comparison sound
    we require a strict repo-relative path: no absolute paths, no empty paths, no
    ``..`` traversal. Returns the POSIX-normalized relative path.
    """
    if path is None or not str(path).strip():
        raise ValidationError("Lease scope path must be a non-empty string")
    raw = str(path).strip().replace("\\", "/")
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise ValidationError(f"Lease path must be repo-relative, got absolute: {path!r}")
    parts: list[str] = []
    for segment in pure.parts:
        if segment in ("", "."):
            continue
        if segment == "..":
            raise ValidationError(f"Lease path must not contain '..' traversal: {path!r}")
        parts.append(segment)
    if not parts:
        raise ValidationError(f"Lease path normalizes to empty: {path!r}")
    return "/".join(parts)


class Registry:
    """Operational registry + on-disk coordinator for one Anvil installation."""

    def __init__(self, home: Path | None = None) -> None:
        self.paths = AnvilPaths(home or default_home())
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if not self.paths.registry_db.exists():
                raise NotInitializedError(
                    f"No Anvil installation at {self.paths.home}. Run `anvil init` first."
                )
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.paths.registry_db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # Fail fast instead of hanging if another connection holds a write lock.
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # init
    # ------------------------------------------------------------------ #
    def init(self) -> InstallationConfig:
        """Create the Anvil home, directory layout, and SQLite registry.

        Idempotent: re-running against an existing installation is a no-op that
        returns the existing installation config.
        """
        home = self.paths.home
        home.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.paths.projects_dir,
            self.paths.repos_dir,
            self.paths.runs_dir,
            self.paths.worktrees_dir,
            self.paths.shared_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        if self.paths.installation_json.exists():
            installation = InstallationConfig.load(self.paths.installation_json)
        else:
            installation = InstallationConfig()
            installation.save(self.paths.installation_json)

        self._conn = self._connect()
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        return installation

    def _require_initialized(self) -> None:
        if not self.paths.exists():
            raise NotInitializedError(
                f"No Anvil installation at {self.paths.home}. Run `anvil init` first."
            )

    # ------------------------------------------------------------------ #
    # repos
    # ------------------------------------------------------------------ #
    def register_repo(self, repo_id: str, path: str | Path) -> RepoConfig:
        """Register an existing local git repo as a shared resource.

        The canonical repository top level is stored, and ``repos.path`` is UNIQUE
        so the same physical repo cannot be registered under two ids (which would
        otherwise split repo-level lease conflict detection).
        """
        self._require_initialized()
        if not _REPO_ID_RE.match(repo_id):
            raise ValidationError(f"repo_id '{repo_id}' must match {_REPO_ID_RE.pattern}")
        repo_path = Path(path).expanduser().resolve()
        if not repo_path.exists():
            raise NotFoundError(f"Repo path does not exist: {repo_path}")
        if not gitutils.is_git_repo(repo_path):
            raise ValidationError(f"Not a git repository: {repo_path}")
        if self._get_repo_row(repo_id) is not None:
            raise AlreadyExistsError(f"Repo '{repo_id}' is already registered")

        toplevel = gitutils.repo_toplevel(repo_path)
        canonical = str(toplevel)
        existing = self.conn.execute(
            "SELECT repo_id FROM repos WHERE path = ?", (canonical,)
        ).fetchone()
        if existing is not None:
            raise AlreadyExistsError(
                f"Repository at {canonical} is already registered as '{existing['repo_id']}'"
            )

        default_branch = gitutils.current_branch(toplevel)
        config = RepoConfig(
            repo_id=repo_id,
            name=repo_id,
            path=canonical,
            default_branch=default_branch,
        )
        config.save(self.paths.repo_json(repo_id))
        try:
            self.conn.execute(
                "INSERT INTO repos (repo_id, path, default_branch, config_path, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (repo_id, canonical, default_branch, str(self.paths.repo_json(repo_id)), config.created_at),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise AlreadyExistsError(
                f"Repository at {canonical} is already registered under another id"
            ) from exc
        return config

    def _get_repo_row(self, repo_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM repos WHERE repo_id = ?", (repo_id,)).fetchone()

    def get_repo(self, repo_id: str) -> sqlite3.Row:
        row = self._get_repo_row(repo_id)
        if row is None:
            raise NotFoundError(f"Repo '{repo_id}' is not registered")
        return row

    def list_repos(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM repos ORDER BY repo_id"))

    # ------------------------------------------------------------------ #
    # projects
    # ------------------------------------------------------------------ #
    def create_project(self, project_id: str, repos: Iterable[str], name: str | None = None) -> ProjectConfig:
        """Create a project that references one or more already-registered repos."""
        self._require_initialized()
        if not _PROJECT_ID_RE.match(project_id):
            raise ValidationError(f"project_id '{project_id}' must match {_PROJECT_ID_RE.pattern}")
        repo_list = list(repos)
        if not repo_list:
            raise ValidationError("A project must reference at least one repo")
        for repo_id in repo_list:
            self.get_repo(repo_id)  # raises NotFoundError if missing
        if self._get_project_row(project_id) is not None:
            raise AlreadyExistsError(f"Project '{project_id}' already exists")

        config = ProjectConfig(project_id=project_id, name=name or project_id, repos=repo_list)
        config.save(self.paths.project_json(project_id))
        self.conn.execute(
            "INSERT INTO projects (project_id, name, config_path, created_at) VALUES (?, ?, ?, ?)",
            (project_id, config.name, str(self.paths.project_json(project_id)), config.created_at),
        )
        self.conn.commit()
        return config

    def _get_project_row(self, project_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()

    def get_project(self, project_id: str) -> sqlite3.Row:
        row = self._get_project_row(project_id)
        if row is None:
            raise NotFoundError(f"Project '{project_id}' does not exist")
        return row

    def load_project_config(self, project_id: str) -> ProjectConfig:
        self.get_project(project_id)
        return ProjectConfig.load(self.paths.project_json(project_id))

    def list_projects(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM projects ORDER BY project_id"))

    # ------------------------------------------------------------------ #
    # task scopes (stored inside project.json)
    # ------------------------------------------------------------------ #
    def create_scope(
        self,
        project_id: str,
        scope_id: str,
        root_paths: Iterable[str],
        discovery_focus_paths: Iterable[str] | None = None,
        default_forbidden_changes: Iterable[str] | None = None,
    ) -> TaskScope:
        """Add a task scope to a project's config."""
        if not _SCOPE_ID_RE.match(scope_id):
            raise ValidationError(f"scope_id '{scope_id}' must match {_SCOPE_ID_RE.pattern}")
        roots = [p for p in root_paths if p]
        if not roots:
            raise ValidationError("A task scope requires at least one root path")
        config = self.load_project_config(project_id)
        if scope_id in config.task_scopes:
            raise AlreadyExistsError(
                f"Scope '{scope_id}' already exists in project '{project_id}'"
            )
        scope = TaskScope(
            scope_id=scope_id,
            root_paths=roots,
            discovery_focus_paths=list(discovery_focus_paths or []),
            default_forbidden_changes=list(default_forbidden_changes or []),
        )
        config.task_scopes[scope_id] = scope
        config.save(self.paths.project_json(project_id))
        return scope

    def get_scope(self, project_id: str, scope_id: str) -> TaskScope:
        config = self.load_project_config(project_id)
        if scope_id not in config.task_scopes:
            raise NotFoundError(f"Scope '{scope_id}' not found in project '{project_id}'")
        return config.task_scopes[scope_id]

    # ------------------------------------------------------------------ #
    # runs + worktree allocation
    # ------------------------------------------------------------------ #
    def create_run(
        self,
        run_id: str,
        project_id: str,
        repo_id: str,
        initiated_by: str,
        task_scope_id: str | None = None,
        target_branch: str | None = None,
        task_summary: str | None = None,
        mode: str = DEFAULT_MODE,
        allocate_worktree: bool = True,
    ) -> sqlite3.Row:
        """Create a run, allocate its per-run worktree, and register it.

        On any failure after the worktree/run-dir is created, the filesystem and
        git state are compensated (worktree removed, run dir cleaned) so a failed
        registration never leaves orphaned state.
        """
        self._require_initialized()
        if not _RUN_ID_RE.match(run_id):
            raise ValidationError(f"run_id '{run_id}' must match {_RUN_ID_RE.pattern}")
        self.get_project(project_id)
        repo = self.get_repo(repo_id)
        project_config = ProjectConfig.load(self.paths.project_json(project_id))
        if repo_id not in project_config.repos:
            raise ValidationError(
                f"Repo '{repo_id}' is not referenced by project '{project_id}'"
            )
        if task_scope_id is not None and task_scope_id not in project_config.task_scopes:
            raise NotFoundError(f"Scope '{task_scope_id}' not found in project '{project_id}'")
        if self._get_run_row(run_id) is not None:
            raise AlreadyExistsError(f"Run '{run_id}' already exists")

        repo_path = Path(repo["path"])
        branch_name = target_branch or repo["default_branch"]
        base_commit = gitutils.branch_head(repo_path, branch_name)

        worktree_path: str | None = None
        run_branch: str | None = None
        run_dir = self.paths.run_dir(run_id)
        inserted = False
        try:
            if allocate_worktree:
                run_branch = f"anvil/{project_id}/{run_id}"
                wt_path = self.paths.worktree_path(repo_id, run_id)
                gitutils.add_worktree(repo_path, wt_path, run_branch, base_commit)
                worktree_path = str(wt_path)
            run_dir.mkdir(parents=True, exist_ok=True)

            timestamp = now_iso()
            # Write per-run JSON artifacts BEFORE the DB commit, so the SQLite row
            # is never committed while its on-disk artifacts are missing. If a
            # write fails, nothing has been committed and compensation runs.
            self._write_controller_state(run_id, mode)
            self._write_run_manifest(
                run_id=run_id,
                project_id=project_id,
                repo_id=repo_id,
                repo=repo,
                branch_name=branch_name,
                base_commit=base_commit,
                initiated_by=initiated_by,
                task_summary=task_summary,
                mode=mode,
                created_at=timestamp,
            )

            self.conn.execute(
                """
                INSERT INTO runs (
                  run_id, project_id, repo_id, task_scope_id,
                  lifecycle_state, pipeline_state, mode, task_summary,
                  base_commit, target_branch, target_head_at_start, current_target_head,
                  worktree_path, branch, initiated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'created', 'INIT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, project_id, repo_id, task_scope_id,
                    mode, task_summary,
                    base_commit, branch_name, base_commit, base_commit,
                    worktree_path, run_branch, initiated_by, timestamp, timestamp,
                ),
            )
            inserted = True
            self.conn.commit()
        except Exception:
            # Compensation: undo every side effect, including a committed row, so a
            # failed registration never leaves a run row pointing at missing state.
            self.conn.rollback()
            if inserted:
                self.conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
                self.conn.commit()
            if worktree_path:
                gitutils.remove_worktree(repo_path, Path(worktree_path), force=True)
            if run_branch:
                gitutils.delete_branch(repo_path, run_branch)
            self._cleanup_run_dir(run_dir)
            raise
        return self.get_run(run_id)

    def _write_controller_state(self, run_id: str, mode: str) -> None:
        """Initialize the per-run controller-state JSON (pipeline state lives in
        the run directory, per the roadmap). Conforms to controller_state.schema."""
        state = {
            "run_id": run_id,
            "current_state": "INIT",
            "mode": mode,
            "pending_human_decisions": [],
        }
        self._write_run_json(run_id, "controller_state.json", state)

    def _write_run_manifest(
        self,
        run_id: str,
        project_id: str,
        repo_id: str,
        repo: sqlite3.Row,
        branch_name: str,
        base_commit: str,
        initiated_by: str,
        task_summary: str | None,
        mode: str,
        created_at: str,
    ) -> None:
        """Initialize the per-run partial manifest with the fields known at
        registration time.

        Written to ``run_manifest.partial.json`` — a DISTINCT filename from the
        gate-critical ``run_manifest.json`` that Milestone 2 produces and
        validates against run_manifest.schema. Keeping them separate avoids any
        clash with that schema's ``additionalProperties: false`` and makes the
        provisional nature explicit. The gate-critical fields (models,
        prompt_versions/hashes, guardrails) are not known until later milestones.
        """
        manifest = {
            "run_id": run_id,
            "created_at": created_at,
            "project_id": project_id,
            "repo_id": repo_id,
            "initiated_by": initiated_by,
            "task_description": task_summary or "",
            "repo": {
                "name": repo["repo_id"],
                "branch": branch_name,
                "base_commit": base_commit,
            },
            "mode": mode,
            "artifact_root": str(self.paths.run_dir(run_id)),
            "complete": False,
        }
        self._write_run_json(run_id, "run_manifest.partial.json", manifest)

    def _write_run_json(self, run_id: str, filename: str, payload: dict[str, Any]) -> None:
        target = self.paths.run_dir(run_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")

    @staticmethod
    def _cleanup_run_dir(run_dir: Path) -> None:
        if not run_dir.exists():
            return
        for child in sorted(run_dir.rglob("*"), reverse=True):
            try:
                child.unlink() if child.is_file() else child.rmdir()
            except OSError:
                pass
        try:
            run_dir.rmdir()
        except OSError:
            pass

    def _get_run_row(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def get_run(self, run_id: str) -> sqlite3.Row:
        row = self._get_run_row(run_id)
        if row is None:
            raise NotFoundError(f"Run '{run_id}' does not exist")
        return row

    def list_runs(
        self, project_id: str | None = None, repo_id: str | None = None
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM runs"
        clauses: list[str] = []
        params: list[str] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if repo_id is not None:
            clauses.append("repo_id = ?")
            params.append(repo_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, run_id"
        return list(self.conn.execute(query, params))

    # ------------------------------------------------------------------ #
    # lifecycle transitions (lifecycle_state only; pipeline_state untouched)
    # ------------------------------------------------------------------ #
    def set_lifecycle_state(self, run_id: str, new_state: str) -> sqlite3.Row:
        """Transition a run's lifecycle state, gated by the transition table."""
        if new_state not in LIFECYCLE_STATES:
            raise ValidationError(f"Unknown lifecycle state: {new_state}")
        run = self.get_run(run_id)
        current = run["lifecycle_state"]
        if new_state == current:
            return run
        if new_state not in _ALLOWED_TRANSITIONS.get(current, set()):
            raise StateTransitionError(
                f"Illegal lifecycle transition for run '{run_id}': {current} -> {new_state}"
            )
        finalized_clause = ""
        params: list[Any] = [new_state, now_iso()]
        if new_state in _TERMINAL_STATES:
            finalized_clause = ", finalized_at = ?"
            params.append(now_iso())
        params.append(run_id)
        self.conn.execute(
            f"UPDATE runs SET lifecycle_state = ?, updated_at = ?{finalized_clause} WHERE run_id = ?",
            params,
        )
        self.conn.commit()
        return self.get_run(run_id)

    def pause_run(self, run_id: str) -> sqlite3.Row:
        """Pause a run: lifecycle -> paused, pipeline_state preserved.

        Active leases gain the default expiration so a stale paused lease becomes
        eligible for force-release (roadmap: paused runs retain leases *with* an
        expiration).
        """
        run = self.get_run(run_id)
        row = self.set_lifecycle_state(run_id, "paused")
        expires_at = iso_after_seconds(DEFAULT_LEASE_TTL_SECONDS)
        self.conn.execute(
            "UPDATE leases SET expires_at = ? WHERE run_id = ? AND status = 'active' AND expires_at IS NULL",
            (expires_at, run_id),
        )
        self.conn.commit()
        return row

    def activate_run(self, run_id: str) -> sqlite3.Row:
        """Start a freshly-created run: created -> active. Distinct from resume_run,
        which only brings a previously-active run back from a paused-like state."""
        run = self.get_run(run_id)
        if run["lifecycle_state"] != "created":
            raise StateTransitionError(
                f"activate_run requires a 'created' run, got '{run['lifecycle_state']}'"
            )
        return self.set_lifecycle_state(run_id, "active")

    def resume_run(self, run_id: str) -> sqlite3.Row:
        """Resume a previously-active run from a paused-like state, preserving
        pipeline_state. Only ``paused``/``blocked``/``waiting_for_human`` are
        resumable; a never-started ``created`` run must use ``activate_run``.

        If leases were force-released while paused, the run cannot silently resume
        as active holding nothing; the caller must re-acquire them first
        (``reacquire_lost_leases``). This closes the "resume with zero leases"
        bypass.
        """
        run = self.get_run(run_id)
        if run["lifecycle_state"] not in _RESUMABLE_STATES:
            raise StateTransitionError(
                f"Cannot resume run '{run_id}' from state '{run['lifecycle_state']}'; "
                f"resumable states are {sorted(_RESUMABLE_STATES)}"
            )
        lost = self._lost_lease_scopes(run_id)
        if lost:
            raise StateTransitionError(
                f"Run '{run_id}' lost leases while paused {sorted(lost)}; "
                "reacquire them (reacquire_lost_leases) before resuming"
            )
        # Clear the paused expiration on still-held leases as they return to active use.
        self.conn.execute(
            "UPDATE leases SET expires_at = NULL WHERE run_id = ? AND status = 'active'",
            (run_id,),
        )
        self.conn.commit()
        return self.set_lifecycle_state(run_id, "active")

    def abort_run(self, run_id: str, remove_worktree: bool = True) -> sqlite3.Row:
        """Abort a run: release leases, drop the worktree, mark aborted."""
        run = self.get_run(run_id)
        if run["lifecycle_state"] in _TERMINAL_STATES:
            raise StateTransitionError(
                f"Run '{run_id}' is already in terminal state '{run['lifecycle_state']}'"
            )
        self._release_all_leases(run_id, reason="aborted")
        if remove_worktree:
            self._teardown_worktree(run)
        return self.set_lifecycle_state(run_id, "aborted")

    def finalize_run(self, run_id: str, remove_worktree: bool = True) -> sqlite3.Row:
        """Finalize a run: release leases, drop the worktree, mark finalized."""
        run = self.get_run(run_id)
        if run["lifecycle_state"] in _TERMINAL_STATES:
            raise StateTransitionError(
                f"Run '{run_id}' is already in terminal state '{run['lifecycle_state']}'"
            )
        self._release_all_leases(run_id, reason="finalized")
        if remove_worktree:
            self._teardown_worktree(run)
        return self.set_lifecycle_state(run_id, "finalized")

    def _teardown_worktree(self, run: sqlite3.Row) -> None:
        """Remove a run's worktree and its run branch (best-effort on branch)."""
        if not run["worktree_path"]:
            return
        repo = self.get_repo(run["repo_id"])
        gitutils.remove_worktree(Path(repo["path"]), Path(run["worktree_path"]))
        if run["branch"]:
            gitutils.delete_branch(Path(repo["path"]), run["branch"])

    def set_pipeline_state(self, run_id: str, pipeline_state: str) -> sqlite3.Row:
        """Set the pipeline (progress) state. Provided so tests can prove that
        pause/resume leave it untouched; the real driver is Milestone 1."""
        self.get_run(run_id)
        self.conn.execute(
            "UPDATE runs SET pipeline_state = ?, updated_at = ? WHERE run_id = ?",
            (pipeline_state, now_iso(), run_id),
        )
        self.conn.commit()
        return self.get_run(run_id)

    # ------------------------------------------------------------------ #
    # leases
    # ------------------------------------------------------------------ #
    def acquire_lease(
        self,
        run_id: str,
        repo_id: str,
        lease_type: str,
        scope: str,
        access: str = "write",
        work_order_id: str | None = None,
        expires_at: str | None = None,
        allow_override: bool = False,
        override_approved_by: str | None = None,
        override_reason: str | None = None,
    ) -> sqlite3.Row:
        """Acquire a repo-level lease, enforcing the V1 conflict rules.

        Enforcement layers, strongest first:
        - run↔repo binding: the lease repo must equal the run's repo.
        - lifecycle gate: terminal runs cannot acquire leases.
        - DB atomicity: a partial UNIQUE index guarantees at most one active
          file_write per (repo, normalized path) and one active merge_queue per
          repo, so two racing connections cannot both succeed.

        ``file_write`` conflicts on (repo, normalized path); ``merge_queue`` on
        repo. Conflict detection is repo-level, catching cross-project,
        same-repo collisions. Re-acquiring an identical lease the run already
        holds is idempotent. ``allow_override`` performs a recorded handoff: the
        incumbent lease is released (status ``overridden``) and a new lease is
        granted carrying the approver/reason.
        """
        run = self.get_run(run_id)
        self.get_repo(repo_id)
        if lease_type not in LEASE_TYPES:
            raise ValidationError(f"Unknown lease type: {lease_type}")
        if run["repo_id"] != repo_id:
            raise ValidationError(
                f"Run '{run_id}' is bound to repo '{run['repo_id']}', "
                f"cannot lease against unrelated repo '{repo_id}'"
            )
        if run["lifecycle_state"] not in _LEASE_HOLDING_STATES:
            raise StateTransitionError(
                f"Run '{run_id}' in state '{run['lifecycle_state']}' may not acquire leases; "
                f"lease-holding states are {sorted(_LEASE_HOLDING_STATES)} "
                "(start the run with activate_run first)"
            )

        normalized = normalize_lease_path(scope) if lease_type == "file_write" else scope
        if lease_type == "merge_queue" and not str(scope).strip():
            raise ValidationError("merge_queue lease requires a non-empty target branch scope")

        # Idempotent: the same run re-acquiring an identical active lease.
        existing_self = self._active_lease_for(repo_id, lease_type, normalized, run_id)
        if existing_self is not None:
            return existing_self

        conflict = self._find_lease_conflict(repo_id, lease_type, normalized, run_id)
        if conflict is not None:
            if not allow_override:
                raise LeaseConflictError(
                    f"{lease_type} lease conflict on repo '{repo_id}'"
                    + (f" for path '{normalized}'" if lease_type == "file_write" else "")
                    + f": already held by run '{conflict['run_id']}'",
                    conflicting_lease_id=conflict["lease_id"],
                )
            if not override_approved_by or not override_reason:
                raise ValidationError(
                    "Override requires both override_approved_by and override_reason"
                )
            # Recorded handoff: release the incumbent before taking the lease.
            self.conn.execute(
                "UPDATE leases SET status = 'overridden', released_at = ? WHERE lease_id = ?",
                (now_iso(), conflict["lease_id"]),
            )

        lease_id = f"lease-{uuid.uuid4().hex[:12]}"
        try:
            self.conn.execute(
                """
                INSERT INTO leases (
                  lease_id, lease_type, repo_id, run_id, work_order_id,
                  scope, access, status, acquired_at, expires_at,
                  override_approved_by, override_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    lease_id, lease_type, repo_id, run_id, work_order_id,
                    normalized, access, now_iso(), expires_at,
                    override_approved_by if conflict is not None else None,
                    override_reason if conflict is not None else None,
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            # Lost a race to another connection that inserted the active lease first.
            self.conn.rollback()
            winner = self._find_lease_conflict(repo_id, lease_type, normalized, run_id)
            raise LeaseConflictError(
                f"{lease_type} lease conflict on repo '{repo_id}'"
                + (f" for path '{normalized}'" if lease_type == "file_write" else "")
                + " (lost acquisition race)",
                conflicting_lease_id=winner["lease_id"] if winner else None,
            ) from exc
        return self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()

    def _active_lease_for(
        self, repo_id: str, lease_type: str, normalized_scope: str, run_id: str
    ) -> sqlite3.Row | None:
        rows = self.conn.execute(
            "SELECT * FROM leases WHERE repo_id = ? AND lease_type = ? AND status = 'active' AND run_id = ?",
            (repo_id, lease_type, run_id),
        ).fetchall()
        for row in rows:
            if lease_type == "merge_queue" or row["scope"] == normalized_scope:
                return row
        return None

    def _find_lease_conflict(
        self, repo_id: str, lease_type: str, normalized_scope: str, run_id: str
    ) -> sqlite3.Row | None:
        active = self.conn.execute(
            "SELECT * FROM leases WHERE repo_id = ? AND lease_type = ? AND status = 'active'",
            (repo_id, lease_type),
        ).fetchall()
        for row in active:
            if row["run_id"] == run_id:
                continue
            if lease_type == "merge_queue" or row["scope"] == normalized_scope:
                return row
        return None

    def _lost_lease_scopes(self, run_id: str) -> set[str]:
        """Scopes for which this run had a lease force-released and has no active
        replacement — i.e. coverage the run silently lost while paused."""
        forced = self.conn.execute(
            "SELECT lease_type, scope FROM leases WHERE run_id = ? AND status = 'force_released'",
            (run_id,),
        ).fetchall()
        active = {
            (r["lease_type"], r["scope"])
            for r in self.conn.execute(
                "SELECT lease_type, scope FROM leases WHERE run_id = ? AND status = 'active'",
                (run_id,),
            )
        }
        return {
            f"{r['lease_type']}:{r['scope']}"
            for r in forced
            if (r["lease_type"], r["scope"]) not in active
        }

    def reacquire_lost_leases(self, run_id: str) -> list[sqlite3.Row]:
        """Re-acquire leases this run lost (force-released) while paused.

        Subject to the same conflict rules, so a path grabbed by another run in
        the meantime will raise :class:`LeaseConflictError`.
        """
        run = self.get_run(run_id)
        forced = self.conn.execute(
            "SELECT * FROM leases WHERE run_id = ? AND status = 'force_released'",
            (run_id,),
        ).fetchall()
        active = {
            (r["lease_type"], r["scope"])
            for r in self.conn.execute(
                "SELECT lease_type, scope FROM leases WHERE run_id = ? AND status = 'active'",
                (run_id,),
            )
        }
        reacquired: list[sqlite3.Row] = []
        for row in forced:
            if (row["lease_type"], row["scope"]) in active:
                continue
            reacquired.append(
                self.acquire_lease(
                    run_id, run["repo_id"], row["lease_type"], row["scope"], access=row["access"]
                )
            )
        return reacquired

    def release_lease(self, lease_id: str, reason: str = "released") -> None:
        if reason not in _LEASE_RELEASE_STATUSES:
            raise ValidationError(
                f"Invalid lease release status '{reason}'; "
                f"allowed: {sorted(_LEASE_RELEASE_STATUSES)}"
            )
        row = self.conn.execute("SELECT * FROM leases WHERE lease_id = ?", (lease_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Lease '{lease_id}' does not exist")
        self.conn.execute(
            "UPDATE leases SET status = ?, released_at = ? WHERE lease_id = ?",
            (reason, now_iso(), lease_id),
        )
        self.conn.commit()

    def _release_all_leases(self, run_id: str, reason: str) -> None:
        if reason not in _LEASE_RELEASE_STATUSES:
            raise ValidationError(
                f"Invalid lease release status '{reason}'; "
                f"allowed: {sorted(_LEASE_RELEASE_STATUSES)}"
            )
        self.conn.execute(
            "UPDATE leases SET status = ?, released_at = ? WHERE run_id = ? AND status = 'active'",
            (reason, now_iso(), run_id),
        )
        self.conn.commit()

    def list_leases(self, repo_id: str | None = None, active_only: bool = True) -> list[sqlite3.Row]:
        query = "SELECT * FROM leases"
        clauses: list[str] = []
        params: list[str] = []
        if repo_id is not None:
            clauses.append("repo_id = ?")
            params.append(repo_id)
        if active_only:
            clauses.append("status = 'active'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY acquired_at, lease_id"
        return list(self.conn.execute(query, params))

    def force_release_stale_leases(
        self, now: str | None = None, repo_id: str | None = None
    ) -> list[str]:
        """Force-release active leases whose ``expires_at`` has passed and whose
        run is not currently active. Returns the released lease ids.

        When ``repo_id`` is given, only that repo's leases are considered, so a
        repo-scoped CLI call never touches unrelated repos.
        """
        marker = now or now_iso()
        query = """
            SELECT l.lease_id FROM leases l
            JOIN runs r ON r.run_id = l.run_id
            WHERE l.status = 'active'
              AND l.expires_at IS NOT NULL
              AND l.expires_at < ?
              AND r.lifecycle_state != 'active'
        """
        params: list[str] = [marker]
        if repo_id is not None:
            query += " AND l.repo_id = ?"
            params.append(repo_id)
        rows = self.conn.execute(query, params).fetchall()
        released = [row["lease_id"] for row in rows]
        for lease_id in released:
            self.conn.execute(
                "UPDATE leases SET status = 'force_released', released_at = ? WHERE lease_id = ?",
                (marker, lease_id),
            )
        self.conn.commit()
        return released

    # ------------------------------------------------------------------ #
    # base-commit drift (detection only)
    # ------------------------------------------------------------------ #
    def check_drift(self, run_id: str) -> DriftResult:
        """Compare the run's recorded target head against the live branch head.

        Detection only — no rebase is performed (that is Milestone 1+). Updates
        ``current_target_head`` so ``status`` reflects the latest observation.
        """
        run = self.get_run(run_id)
        repo = self.get_repo(run["repo_id"])
        current_head = gitutils.branch_head(Path(repo["path"]), run["target_branch"])
        at_start = run["target_head_at_start"]
        is_stale = current_head != at_start
        self.conn.execute(
            "UPDATE runs SET current_target_head = ?, updated_at = ? WHERE run_id = ?",
            (current_head, now_iso(), run_id),
        )
        self.conn.commit()
        return DriftResult(
            base_commit=run["base_commit"],
            target_branch=run["target_branch"],
            target_head_at_start=at_start,
            target_head_current=current_head,
            base_is_stale=is_stale,
            rebase_required=is_stale,
        )


__all__ = [
    "Registry",
    "DriftResult",
    "LIFECYCLE_STATES",
    "LEASE_TYPES",
    "DEFAULT_LEASE_TTL_SECONDS",
    "normalize_lease_path",
]
