"""Repo-level lease acquisition, conflict detection, and release."""

from __future__ import annotations

import pytest

from anvil.errors import LeaseConflictError, ValidationError
from anvil.registry import Registry


def _two_projects_one_repo_two_runs(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("data-pipelines")
    registry.register_repo("repo-data-pipelines", repo_path)
    registry.create_project("proj-a", ["repo-data-pipelines"])
    registry.create_project("proj-b", ["repo-data-pipelines"])
    r1 = registry.create_run("RUN-20260601-001", "proj-a", "repo-data-pipelines", "tester")
    r2 = registry.create_run("RUN-20260601-002", "proj-b", "repo-data-pipelines", "tester")
    # Leases are only held by started runs.
    registry.activate_run("RUN-20260601-001")
    registry.activate_run("RUN-20260601-002")
    return r1, r2


def test_file_write_lease_conflict_same_path(registry: Registry, git_repo_factory):
    """Acceptance: two runs claiming the same file on the same repo conflict.

    The two runs belong to DIFFERENT projects, proving repo-level (cross-project)
    conflict detection.
    """
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "file_write", "shared/config/app.py"
    )
    with pytest.raises(LeaseConflictError) as excinfo:
        registry.acquire_lease(
            "RUN-20260601-002", "repo-data-pipelines", "file_write", "shared/config/app.py"
        )
    assert excinfo.value.conflicting_lease_id is not None


def test_file_write_lease_path_normalization(registry: Registry, git_repo_factory):
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "file_write", "shared/config/app.py"
    )
    # Same path expressed differently still conflicts.
    with pytest.raises(LeaseConflictError):
        registry.acquire_lease(
            "RUN-20260601-002", "repo-data-pipelines", "file_write", "./shared/config/app.py"
        )


def test_file_write_different_paths_ok(registry: Registry, git_repo_factory):
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "file_write", "shared/config/a.py"
    )
    # Different path → no conflict.
    lease = registry.acquire_lease(
        "RUN-20260601-002", "repo-data-pipelines", "file_write", "shared/config/b.py"
    )
    assert lease["status"] == "active"


def test_file_write_conflict_override(registry: Registry, git_repo_factory):
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "file_write", "shared/config/app.py"
    )
    # Explicit override is allowed only with recorded approver + reason.
    lease = registry.acquire_lease(
        "RUN-20260601-002", "repo-data-pipelines", "file_write", "shared/config/app.py",
        allow_override=True, override_approved_by="maintainer", override_reason="hotfix handoff",
    )
    assert lease["status"] == "active"
    assert lease["override_approved_by"] == "maintainer"
    assert lease["override_reason"] == "hotfix handoff"
    # The incumbent lease was released (handed off), not left as a second active.
    active = registry.list_leases(repo_id="repo-data-pipelines", active_only=True)
    assert len(active) == 1
    assert active[0]["run_id"] == "RUN-20260601-002"


def test_file_write_override_requires_approval(registry: Registry, git_repo_factory):
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "file_write", "shared/config/app.py"
    )
    # Override without approver/reason is rejected.
    with pytest.raises(ValidationError):
        registry.acquire_lease(
            "RUN-20260601-002", "repo-data-pipelines", "file_write", "shared/config/app.py",
            allow_override=True,
        )


def test_merge_queue_lease_conflict(registry: Registry, git_repo_factory):
    """Acceptance: only one run may hold the merge_queue lease per repo."""
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "merge_queue", "main"
    )
    with pytest.raises(LeaseConflictError):
        registry.acquire_lease(
            "RUN-20260601-002", "repo-data-pipelines", "merge_queue", "main"
        )


def test_merge_queue_released_allows_next(registry: Registry, git_repo_factory):
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    lease = registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "merge_queue", "main"
    )
    registry.release_lease(lease["lease_id"])
    # After release, the second run can acquire it.
    nxt = registry.acquire_lease(
        "RUN-20260601-002", "repo-data-pipelines", "merge_queue", "main"
    )
    assert nxt["status"] == "active"


def test_leases_released_on_abort(registry: Registry, git_repo_factory):
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "file_write", "shared/config/app.py"
    )
    registry.abort_run("RUN-20260601-001")
    assert registry.list_leases(repo_id="repo-data-pipelines", active_only=True) == []
    # Path is now free for the other run.
    lease = registry.acquire_lease(
        "RUN-20260601-002", "repo-data-pipelines", "file_write", "shared/config/app.py"
    )
    assert lease["status"] == "active"


def test_leases_released_on_finalize(registry: Registry, git_repo_factory):
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "merge_queue", "main"
    )
    registry.finalize_run("RUN-20260601-001")
    assert registry.list_leases(repo_id="repo-data-pipelines", active_only=True) == []


def test_unknown_lease_type(registry: Registry, git_repo_factory):
    _two_projects_one_repo_two_runs(registry, git_repo_factory)
    with pytest.raises(ValidationError):
        registry.acquire_lease(
            "RUN-20260601-001", "repo-data-pipelines", "exclusive_db", "main"
        )


def test_force_release_stale_lease(registry: Registry, git_repo_factory):
    """A paused run's expired lease can be force-released; an active run's cannot."""
    _two_projects_one_repo_two_runs(registry, git_repo_factory)  # both runs active
    # Lease expired in the past, run paused → eligible for force release.
    registry.acquire_lease(
        "RUN-20260601-001", "repo-data-pipelines", "file_write", "shared/config/app.py",
        expires_at="2000-01-01T00:00:00Z",
    )
    registry.pause_run("RUN-20260601-001")

    # Active run with an expired lease should NOT be force-released.
    registry.acquire_lease(
        "RUN-20260601-002", "repo-data-pipelines", "file_write", "shared/config/other.py",
        expires_at="2000-01-01T00:00:00Z",
    )

    released = registry.force_release_stale_leases(now="2025-01-01T00:00:00Z")
    assert len(released) == 1
    remaining_runs = {l["run_id"] for l in registry.list_leases(repo_id="repo-data-pipelines")}
    assert remaining_runs == {"RUN-20260601-002"}
