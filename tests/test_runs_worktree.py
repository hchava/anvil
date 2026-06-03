"""Run creation and per-run worktree allocation."""

from __future__ import annotations

from pathlib import Path

import pytest

from anvil import gitutils
from anvil.errors import AlreadyExistsError, NotFoundError, ValidationError
from anvil.registry import Registry


def _setup_project(registry: Registry, git_repo_factory, scopes=("ingestion", "export")):
    repo_path = git_repo_factory("data-pipelines")
    registry.register_repo("repo-data-pipelines", repo_path)
    registry.create_project("proj-platform", ["repo-data-pipelines"])
    for s in scopes:
        registry.create_scope("proj-platform", s, [f"src/{s}/"])
    return repo_path


def test_create_run_allocates_worktree(registry: Registry, git_repo_factory):
    repo_path = _setup_project(registry, git_repo_factory)
    run = registry.create_run(
        run_id="RUN-20260601-001",
        project_id="proj-platform",
        repo_id="repo-data-pipelines",
        initiated_by="tester",
        task_scope_id="ingestion",
    )

    # Worktree path follows worktrees/{repo_id}/{run_id} and exists on disk.
    expected_wt = registry.paths.worktree_path("repo-data-pipelines", "RUN-20260601-001")
    assert run["worktree_path"] == str(expected_wt)
    assert expected_wt.is_dir()
    assert (expected_wt / "README.md").exists()  # checked out from base commit

    # Branch follows anvil/{project_id}/{run_id} and the harness did NOT touch
    # the user's checkout (still on default branch).
    assert run["branch"] == "anvil/proj-platform/RUN-20260601-001"
    assert gitutils.current_branch(repo_path) == "main"
    assert gitutils.current_branch(expected_wt) == "anvil/proj-platform/RUN-20260601-001"

    # base_commit captured from target branch HEAD; defaults recorded.
    assert run["base_commit"] == gitutils.branch_head(repo_path, "main")
    assert run["target_branch"] == "main"
    assert run["target_head_at_start"] == run["base_commit"]
    assert run["lifecycle_state"] == "created"
    assert run["pipeline_state"] == "INIT"

    # Per-run dir created for pipeline-state JSON.
    assert registry.paths.run_dir("RUN-20260601-001").is_dir()


def test_two_runs_same_repo_different_scopes(registry: Registry, git_repo_factory):
    """Acceptance: two runs on the same repo with different scopes coexist."""
    repo_path = _setup_project(registry, git_repo_factory)
    r1 = registry.create_run(
        "RUN-20260601-001", "proj-platform", "repo-data-pipelines", "tester",
        task_scope_id="ingestion",
    )
    r2 = registry.create_run(
        "RUN-20260601-002", "proj-platform", "repo-data-pipelines", "tester",
        task_scope_id="export",
    )

    assert r1["task_scope_id"] == "ingestion"
    assert r2["task_scope_id"] == "export"
    # Separate worktrees, both present.
    assert Path(r1["worktree_path"]).is_dir()
    assert Path(r2["worktree_path"]).is_dir()
    assert r1["worktree_path"] != r2["worktree_path"]
    assert {row["run_id"] for row in registry.list_runs(repo_id="repo-data-pipelines")} == {
        "RUN-20260601-001",
        "RUN-20260601-002",
    }


def test_create_run_rejects_unbound_repo(registry: Registry, git_repo_factory):
    # Two repos exist, project references only one.
    repo_a = git_repo_factory("a")
    repo_b = git_repo_factory("b")
    registry.register_repo("repo-a", repo_a)
    registry.register_repo("repo-b", repo_b)
    registry.create_project("proj-x", ["repo-a"])
    with pytest.raises(ValidationError):
        registry.create_run(
            "RUN-20260601-001", "proj-x", "repo-b", "tester"
        )


def test_create_run_rejects_unknown_scope(registry: Registry, git_repo_factory):
    _setup_project(registry, git_repo_factory, scopes=("ingestion",))
    with pytest.raises(NotFoundError):
        registry.create_run(
            "RUN-20260601-001", "proj-platform", "repo-data-pipelines", "tester",
            task_scope_id="does-not-exist",
        )


def test_create_run_rejects_bad_run_id(registry: Registry, git_repo_factory):
    _setup_project(registry, git_repo_factory)
    with pytest.raises(ValidationError):
        registry.create_run("run-1", "proj-platform", "repo-data-pipelines", "tester")


def test_create_run_duplicate(registry: Registry, git_repo_factory):
    _setup_project(registry, git_repo_factory)
    registry.create_run("RUN-20260601-001", "proj-platform", "repo-data-pipelines", "tester")
    with pytest.raises(AlreadyExistsError):
        registry.create_run("RUN-20260601-001", "proj-platform", "repo-data-pipelines", "tester")
