"""Lifecycle vs pipeline state: pause/resume must not lose pipeline progress."""

from __future__ import annotations

import pytest

from anvil.errors import StateTransitionError
from anvil.registry import Registry


def _make_run(registry: Registry, git_repo_factory) -> str:
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    registry.create_project("proj-svc", ["repo-svc"])
    registry.create_run("RUN-20260601-001", "proj-svc", "repo-svc", "tester")
    return "RUN-20260601-001"


def test_pause_resume_preserves_pipeline_state(registry: Registry, git_repo_factory):
    """Acceptance: pause/resume updates lifecycle without losing pipeline state."""
    run_id = _make_run(registry, git_repo_factory)
    # Simulate the controller having advanced the pipeline (Milestone 1 will do
    # this for real); pause/resume must not disturb it.
    registry.set_pipeline_state(run_id, "PLAN_CREATED")
    registry.set_lifecycle_state(run_id, "active")

    paused = registry.pause_run(run_id)
    assert paused["lifecycle_state"] == "paused"
    assert paused["pipeline_state"] == "PLAN_CREATED"

    resumed = registry.resume_run(run_id)
    assert resumed["lifecycle_state"] == "active"
    assert resumed["pipeline_state"] == "PLAN_CREATED"


def test_pause_retains_leases(registry: Registry, git_repo_factory):
    run_id = _make_run(registry, git_repo_factory)
    registry.activate_run(run_id)
    registry.acquire_lease(run_id, "repo-svc", "file_write", "src/app.py")
    registry.pause_run(run_id)
    # Paused runs retain their leases.
    active = registry.list_leases(repo_id="repo-svc", active_only=True)
    assert len(active) == 1
    assert active[0]["run_id"] == run_id


def test_cannot_pause_terminal_run(registry: Registry, git_repo_factory):
    run_id = _make_run(registry, git_repo_factory)
    registry.abort_run(run_id)
    with pytest.raises(StateTransitionError):
        registry.pause_run(run_id)


def test_cannot_resume_active_run(registry: Registry, git_repo_factory):
    run_id = _make_run(registry, git_repo_factory)
    registry.activate_run(run_id)
    # resume_run only applies to paused-like states; an active run is rejected.
    with pytest.raises(StateTransitionError):
        registry.resume_run(run_id)


def test_cannot_resume_created_run(registry: Registry, git_repo_factory):
    """A never-started run must be activated, not resumed."""
    run_id = _make_run(registry, git_repo_factory)
    with pytest.raises(StateTransitionError):
        registry.resume_run(run_id)
    # The correct primitive starts it.
    started = registry.activate_run(run_id)
    assert started["lifecycle_state"] == "active"


def test_activate_requires_created(registry: Registry, git_repo_factory):
    run_id = _make_run(registry, git_repo_factory)
    registry.activate_run(run_id)
    with pytest.raises(StateTransitionError):
        registry.activate_run(run_id)  # already active


def test_cannot_pause_created_run_directly(registry: Registry, git_repo_factory):
    """Roadmap flow is created -> active -> paused; pausing a created run is
    rejected by the transition table."""
    run_id = _make_run(registry, git_repo_factory)
    with pytest.raises(StateTransitionError):
        registry.pause_run(run_id)


def test_abort_is_terminal(registry: Registry, git_repo_factory):
    run_id = _make_run(registry, git_repo_factory)
    registry.abort_run(run_id)
    assert registry.get_run(run_id)["lifecycle_state"] == "aborted"
    with pytest.raises(StateTransitionError):
        registry.abort_run(run_id)
