"""Base-commit drift detection (detection only — no rebase)."""

from __future__ import annotations

from pathlib import Path

from anvil.registry import Registry
from tests.conftest import commit_change


def _make_run(registry: Registry, git_repo_factory) -> tuple[str, Path]:
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    registry.create_project("proj-svc", ["repo-svc"])
    registry.create_run("RUN-20260601-001", "proj-svc", "repo-svc", "tester")
    return "RUN-20260601-001", repo_path


def test_no_drift_when_target_unchanged(registry: Registry, git_repo_factory):
    run_id, _ = _make_run(registry, git_repo_factory)
    result = registry.check_drift(run_id)
    assert result.base_is_stale is False
    assert result.rebase_required is False
    assert result.target_head_at_start == result.target_head_current
    assert result.target_branch == "main"


def test_drift_detected_when_target_moves(registry: Registry, git_repo_factory):
    """Acceptance: drift detected when the target branch moves after run start."""
    run_id, repo_path = _make_run(registry, git_repo_factory)
    start = registry.get_run(run_id)["target_head_at_start"]

    # Advance main on the user's checkout AFTER the run captured its base.
    new_head = commit_change(repo_path, "src/app.py", "VALUE = 2\n", "move main forward")
    assert new_head != start

    result = registry.check_drift(run_id)
    assert result.base_is_stale is True
    assert result.rebase_required is True
    assert result.target_head_at_start == start
    assert result.target_head_current == new_head

    # Structured result shape per the roadmap.
    data = result.to_dict()
    assert set(data) == {
        "base_commit",
        "target_branch",
        "target_head_at_start",
        "target_head_current",
        "base_is_stale",
        "rebase_required",
    }

    # check_drift records the latest observation; no rebase performed (the run's
    # worktree branch is untouched and base_commit is unchanged).
    refreshed = registry.get_run(run_id)
    assert refreshed["current_target_head"] == new_head
    assert refreshed["base_commit"] == start
