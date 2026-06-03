"""Adversarial safety tests for the runtime registry.

These cover the bypasses a prior review reproduced: lease races across two
connections, duplicate repo identity, cross-repo leases, path traversal,
paused-lease expiry + resume blocking, and lifecycle resurrection.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from anvil.errors import (
    AlreadyExistsError,
    LeaseConflictError,
    StateTransitionError,
    ValidationError,
)
from anvil.registry import Registry, normalize_lease_path


def _bootstrap(registry: Registry, git_repo_factory, activate: bool = True):
    repo_path = git_repo_factory("data-pipelines")
    registry.register_repo("repo-dp", repo_path)
    registry.create_project("proj-a", ["repo-dp"])
    registry.create_project("proj-b", ["repo-dp"])
    registry.create_run("RUN-20260601-001", "proj-a", "repo-dp", "tester")
    registry.create_run("RUN-20260601-002", "proj-b", "repo-dp", "tester")
    if activate:
        # Leases are only held by started runs; activate both by default.
        registry.activate_run("RUN-20260601-001")
        registry.activate_run("RUN-20260601-002")
    return repo_path


# ----------------------------------------------------------------------------
# Blocker 1: lease acquisition must be atomic across connections.
# ----------------------------------------------------------------------------

def test_file_write_conflict_across_two_connections(anvil_home: Path, git_repo_factory):
    """Two independent Registry connections cannot both hold the same file_write
    lease — the partial UNIQUE index makes the second insert fail. (Sequential
    across connections; see test_file_write_concurrent_processes for true
    concurrency.)"""
    boot = Registry()
    boot.init()
    _bootstrap(boot, git_repo_factory)
    boot.close()

    reg_a = Registry()
    reg_b = Registry()
    try:
        reg_a.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")
        with pytest.raises(LeaseConflictError):
            reg_b.acquire_lease("RUN-20260601-002", "repo-dp", "file_write", "shared/config/app.py")
        # Exactly one active lease exists in the shared database.
        assert len(reg_b.list_leases(repo_id="repo-dp", active_only=True)) == 1
    finally:
        reg_a.close()
        reg_b.close()


def test_file_write_concurrent_processes(anvil_home: Path, git_repo_factory):
    """Genuinely concurrent acquisition from separate OS processes: exactly one
    process wins the same file_write lease; the database holds one active row."""
    boot = Registry()
    boot.init()
    _bootstrap(boot, git_repo_factory)
    home = str(boot.paths.home)
    boot.close()

    prog = (
        "import sys; from anvil.registry import Registry; "
        "from anvil.errors import LeaseConflictError, StateTransitionError; "
        "r=Registry();\n"
        "import os\n"
        "try:\n"
        "    r.acquire_lease(sys.argv[1], 'repo-dp', 'file_write', 'shared/config/app.py')\n"
        "    print('ACQUIRED')\n"
        "except (LeaseConflictError, StateTransitionError):\n"
        "    print('CONFLICT')\n"
    )
    env = dict(os.environ, ANVIL_HOME=home)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", prog, run_id],
            stdout=subprocess.PIPE, text=True, env=env,
        )
        for run_id in ("RUN-20260601-001", "RUN-20260601-002")
    ]
    outs = [p.communicate()[0].strip() for p in procs]

    assert sorted(outs) == ["ACQUIRED", "CONFLICT"], outs
    check = Registry()
    try:
        assert len(check.list_leases(repo_id="repo-dp", active_only=True)) == 1
    finally:
        check.close()


def test_merge_queue_conflict_across_two_connections(anvil_home: Path, git_repo_factory):
    boot = Registry()
    boot.init()
    _bootstrap(boot, git_repo_factory)
    boot.close()

    reg_a = Registry()
    reg_b = Registry()
    try:
        reg_a.acquire_lease("RUN-20260601-001", "repo-dp", "merge_queue", "main")
        with pytest.raises(LeaseConflictError):
            reg_b.acquire_lease("RUN-20260601-002", "repo-dp", "merge_queue", "main")
    finally:
        reg_a.close()
        reg_b.close()


# ----------------------------------------------------------------------------
# Blocker 2: a physical repo cannot be registered twice under different ids.
# ----------------------------------------------------------------------------

def test_same_repo_two_ids_rejected(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    with pytest.raises(AlreadyExistsError):
        registry.register_repo("repo-svc-alias", repo_path)


def test_same_repo_subdir_path_rejected(registry: Registry, git_repo_factory):
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    # Registering via a subdirectory resolves to the same toplevel → rejected.
    with pytest.raises(AlreadyExistsError):
        registry.register_repo("repo-svc-2", repo_path / "src")


# ----------------------------------------------------------------------------
# Blocker 3: a run cannot lease against a repo it is not bound to.
# ----------------------------------------------------------------------------

def test_created_run_cannot_acquire_lease(registry: Registry, git_repo_factory):
    """A never-started 'created' run must not lock repo paths; it has done no
    work. It can only lease after activate_run."""
    _bootstrap(registry, git_repo_factory, activate=False)
    with pytest.raises(StateTransitionError):
        registry.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")
    # After activation, the same acquisition succeeds.
    registry.activate_run("RUN-20260601-001")
    lease = registry.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")
    assert lease["status"] == "active"


def test_paused_run_retains_lease_acquisition(registry: Registry, git_repo_factory):
    """Paused/blocked/waiting_for_human are lease-holding states (leases retained
    across them), so acquisition is still permitted while paused."""
    _bootstrap(registry, git_repo_factory)  # both active
    registry.pause_run("RUN-20260601-001")
    lease = registry.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")
    assert lease["status"] == "active"


def test_release_lease_rejects_invalid_status(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)  # both active
    lease = registry.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")
    with pytest.raises(ValidationError):
        registry.release_lease(lease["lease_id"], reason="totally-made-up")
    # A valid status works.
    registry.release_lease(lease["lease_id"], reason="released")
    assert registry.list_leases(repo_id="repo-dp", active_only=True) == []


def test_lease_against_unrelated_repo_rejected(registry: Registry, git_repo_factory):
    repo_a = git_repo_factory("a")
    repo_b = git_repo_factory("b")
    registry.register_repo("repo-a", repo_a)
    registry.register_repo("repo-b", repo_b)
    registry.create_project("proj-a", ["repo-a"])
    registry.create_project("proj-b", ["repo-b"])
    registry.create_run("RUN-20260601-001", "proj-a", "repo-a", "tester")
    # Run is bound to repo-a; leasing repo-b must be rejected.
    with pytest.raises(ValidationError):
        registry.acquire_lease("RUN-20260601-001", "repo-b", "file_write", "src/x.py")


# ----------------------------------------------------------------------------
# Blocker 4: path normalization rejects traversal / absolute / empty.
# ----------------------------------------------------------------------------

def test_normalize_rejects_absolute():
    with pytest.raises(ValidationError):
        normalize_lease_path("/etc/passwd")


def test_normalize_rejects_traversal():
    with pytest.raises(ValidationError):
        normalize_lease_path("shared/tmp/../config/app.py")


def test_normalize_rejects_empty():
    for bad in ("", "   ", ".", "./"):
        with pytest.raises(ValidationError):
            normalize_lease_path(bad)


def test_traversal_paths_do_not_coexist(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)
    registry.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")
    # A traversal that resolves to the same path is rejected outright (not a
    # silently-coexisting second lease).
    with pytest.raises(ValidationError):
        registry.acquire_lease(
            "RUN-20260601-002", "repo-dp", "file_write", "shared/tmp/../config/app.py"
        )


def test_lease_path_normalized_in_storage(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)
    lease = registry.acquire_lease(
        "RUN-20260601-001", "repo-dp", "file_write", "./shared/config/app.py"
    )
    assert lease["scope"] == "shared/config/app.py"


# ----------------------------------------------------------------------------
# Blocker 5: paused leases expire; a run cannot resume holding nothing.
# ----------------------------------------------------------------------------

def test_pause_sets_default_expiry(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)
    registry.set_lifecycle_state("RUN-20260601-001", "active")
    registry.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")
    registry.pause_run("RUN-20260601-001")
    active = registry.list_leases(repo_id="repo-dp", active_only=True)
    assert len(active) == 1
    # The previously open-ended lease now carries an expiration.
    assert active[0]["expires_at"] is not None


def test_resume_blocked_after_lease_force_released(registry: Registry, git_repo_factory):
    """The reported bypass: pause → stale force-release → resume active with zero
    leases. Resume must now refuse until leases are re-acquired."""
    _bootstrap(registry, git_repo_factory)
    registry.set_lifecycle_state("RUN-20260601-001", "active")
    registry.acquire_lease(
        "RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py",
        expires_at="2000-01-01T00:00:00Z",
    )
    registry.pause_run("RUN-20260601-001")
    released = registry.force_release_stale_leases(now="2025-01-01T00:00:00Z")
    assert len(released) == 1

    with pytest.raises(StateTransitionError):
        registry.resume_run("RUN-20260601-001")
    # Still paused, not silently active.
    assert registry.get_run("RUN-20260601-001")["lifecycle_state"] == "paused"


def test_resume_after_reacquiring_lost_leases(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)
    registry.set_lifecycle_state("RUN-20260601-001", "active")
    registry.acquire_lease(
        "RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py",
        expires_at="2000-01-01T00:00:00Z",
    )
    registry.pause_run("RUN-20260601-001")
    registry.force_release_stale_leases(now="2025-01-01T00:00:00Z")

    reacquired = registry.reacquire_lost_leases("RUN-20260601-001")
    assert len(reacquired) == 1
    resumed = registry.resume_run("RUN-20260601-001")
    assert resumed["lifecycle_state"] == "active"


def test_reacquire_fails_if_path_taken(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)
    registry.set_lifecycle_state("RUN-20260601-001", "active")
    registry.acquire_lease(
        "RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py",
        expires_at="2000-01-01T00:00:00Z",
    )
    registry.pause_run("RUN-20260601-001")
    registry.force_release_stale_leases(now="2025-01-01T00:00:00Z")
    # Another run grabs the freed path in the meantime.
    registry.acquire_lease("RUN-20260601-002", "repo-dp", "file_write", "shared/config/app.py")
    with pytest.raises(LeaseConflictError):
        registry.reacquire_lost_leases("RUN-20260601-001")


# ----------------------------------------------------------------------------
# High priority: lifecycle table forbids resurrecting terminal runs.
# ----------------------------------------------------------------------------

def test_aborted_run_cannot_be_resurrected(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)
    registry.abort_run("RUN-20260601-001")
    with pytest.raises(StateTransitionError):
        registry.set_lifecycle_state("RUN-20260601-001", "active")
    # And therefore cannot acquire new leases.
    with pytest.raises(StateTransitionError):
        registry.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")


def test_finalized_run_only_archives(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)
    registry.set_lifecycle_state("RUN-20260601-001", "active")
    registry.finalize_run("RUN-20260601-001")
    with pytest.raises(StateTransitionError):
        registry.set_lifecycle_state("RUN-20260601-001", "active")
    archived = registry.set_lifecycle_state("RUN-20260601-001", "archived")
    assert archived["lifecycle_state"] == "archived"


def test_idempotent_lease_reacquire(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)
    first = registry.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")
    again = registry.acquire_lease("RUN-20260601-001", "repo-dp", "file_write", "shared/config/app.py")
    assert first["lease_id"] == again["lease_id"]
    assert len(registry.list_leases(repo_id="repo-dp", active_only=True)) == 1


# ----------------------------------------------------------------------------
# High priority: create_run compensates filesystem state on DB failure.
# ----------------------------------------------------------------------------

def test_create_run_compensates_on_failure(registry: Registry, git_repo_factory, monkeypatch):
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    registry.create_project("proj-svc", ["repo-svc"])

    # Force a failure AFTER the worktree is created but during run registration
    # (now_iso is called to build the INSERT params, after add_worktree).
    import anvil.registry as reg_mod

    calls = {"n": 0}

    def boom() -> str:
        calls["n"] += 1
        raise RuntimeError("simulated failure during run registration")

    monkeypatch.setattr(reg_mod, "now_iso", boom)
    with pytest.raises(RuntimeError):
        registry.create_run("RUN-20260601-009", "proj-svc", "repo-svc", "tester")
    monkeypatch.undo()

    # Compensation removed the worktree, run dir, and left no run row.
    assert calls["n"] >= 1
    assert not registry.paths.worktree_path("repo-svc", "RUN-20260601-009").exists()
    assert not registry.paths.run_dir("RUN-20260601-009").exists()
    assert registry._get_run_row("RUN-20260601-009") is None
    # The git worktree metadata is also clean (a fresh run with the same id works).
    ok = registry.create_run("RUN-20260601-009", "proj-svc", "repo-svc", "tester")
    assert ok["run_id"] == "RUN-20260601-009"


# ----------------------------------------------------------------------------
# High priority: controller-state JSON is written into the run directory.
# ----------------------------------------------------------------------------

def test_create_run_writes_controller_state(registry: Registry, git_repo_factory):
    _bootstrap(registry, git_repo_factory)
    state_path = registry.paths.run_dir("RUN-20260601-001") / "controller_state.json"
    assert state_path.exists()
    import json

    data = json.loads(state_path.read_text())
    assert data["run_id"] == "RUN-20260601-001"
    assert data["current_state"] == "INIT"
    assert data["pending_human_decisions"] == []


def test_controller_state_matches_schema(registry: Registry, git_repo_factory):
    """The written controller_state.json validates against the M0 schema."""
    _bootstrap(registry, git_repo_factory)
    import json

    from jsonschema import Draft202012Validator, FormatChecker

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "controller_state.schema.json"
    schema = json.loads(schema_path.read_text())
    state = json.loads(
        (registry.paths.run_dir("RUN-20260601-001") / "controller_state.json").read_text()
    )
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(state))
    assert errors == []


def test_no_run_row_when_controller_state_write_fails(registry: Registry, git_repo_factory, monkeypatch):
    """P0: the SQLite row must NOT survive if the per-run JSON write fails.

    JSON is written before the DB commit, so a JSON failure leaves no committed
    row and no orphaned worktree/run dir.
    """
    repo_path = git_repo_factory("svc")
    registry.register_repo("repo-svc", repo_path)
    registry.create_project("proj-svc", ["repo-svc"])

    def boom(self, run_id, mode):  # noqa: ANN001
        raise OSError("simulated controller_state.json write failure")

    monkeypatch.setattr(Registry, "_write_controller_state", boom)
    with pytest.raises(OSError):
        registry.create_run("RUN-20260601-009", "proj-svc", "repo-svc", "tester")
    monkeypatch.undo()

    assert registry._get_run_row("RUN-20260601-009") is None
    assert not registry.paths.worktree_path("repo-svc", "RUN-20260601-009").exists()
    assert not registry.paths.run_dir("RUN-20260601-009").exists()


def test_create_run_writes_partial_manifest(registry: Registry, git_repo_factory):
    """create_run initializes a PARTIAL manifest under a distinct filename so it
    never collides with the gate-critical run_manifest.json that Milestone 2
    writes (and which run_manifest.schema validates)."""
    _bootstrap(registry, git_repo_factory)
    import json

    run_dir = registry.paths.run_dir("RUN-20260601-001")
    partial_path = run_dir / "run_manifest.partial.json"
    assert partial_path.exists()
    # The gate-critical filename is NOT written at M0.5.
    assert not (run_dir / "run_manifest.json").exists()

    data = json.loads(partial_path.read_text())
    assert data["run_id"] == "RUN-20260601-001"
    assert data["project_id"] == "proj-a"
    assert data["repo_id"] == "repo-dp"
    assert data["repo"]["branch"]
    assert data["repo"]["base_commit"]
    assert data["complete"] is False


def test_partial_manifest_does_not_falsely_pass_run_manifest_schema(registry: Registry, git_repo_factory):
    """Guard: the partial manifest is intentionally NOT schema-complete, so it
    must not be mistaken for a valid gate-critical run_manifest."""
    _bootstrap(registry, git_repo_factory)
    import json

    from jsonschema import Draft202012Validator, FormatChecker

    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "run_manifest.schema.json"
    schema = json.loads(schema_path.read_text())
    partial = json.loads(
        (registry.paths.run_dir("RUN-20260601-001") / "run_manifest.partial.json").read_text()
    )
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(partial))
    # It deliberately fails the gate schema (missing M2 fields, extra `complete`).
    assert errors != []


# ----------------------------------------------------------------------------
# P0: stale-lease force release is repo-scoped.
# ----------------------------------------------------------------------------

def test_force_release_stale_is_repo_scoped(registry: Registry, git_repo_factory):
    """A repo-scoped force-release must not touch other repos' stale leases."""
    repo_a = git_repo_factory("a")
    repo_b = git_repo_factory("b")
    registry.register_repo("repo-a", repo_a)
    registry.register_repo("repo-b", repo_b)
    registry.create_project("proj-a", ["repo-a"])
    registry.create_project("proj-b", ["repo-b"])
    registry.create_run("RUN-20260601-001", "proj-a", "repo-a", "tester")
    registry.create_run("RUN-20260601-002", "proj-b", "repo-b", "tester")

    registry.activate_run("RUN-20260601-001")
    registry.acquire_lease(
        "RUN-20260601-001", "repo-a", "file_write", "src/x.py", expires_at="2000-01-01T00:00:00Z"
    )
    registry.pause_run("RUN-20260601-001")
    registry.activate_run("RUN-20260601-002")
    registry.acquire_lease(
        "RUN-20260601-002", "repo-b", "file_write", "src/y.py", expires_at="2000-01-01T00:00:00Z"
    )
    registry.pause_run("RUN-20260601-002")

    # Force-release scoped to repo-a only.
    released = registry.force_release_stale_leases(now="2025-01-01T00:00:00Z", repo_id="repo-a")
    assert len(released) == 1
    # repo-b's stale lease is untouched.
    assert len(registry.list_leases(repo_id="repo-b", active_only=True)) == 1
    assert len(registry.list_leases(repo_id="repo-a", active_only=True)) == 0
