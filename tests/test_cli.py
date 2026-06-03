"""End-to-end CLI tests via Click's CliRunner against a temp ANVIL_HOME."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from anvil.cli import cli


def _run(args: list[str]):
    return CliRunner().invoke(cli, args, catch_exceptions=False)


def test_cli_init_then_register_and_status(anvil_home: Path, git_repo_factory):
    repo_path = git_repo_factory("data-pipelines")

    assert _run(["init"]).exit_code == 0
    assert _run(["repo", "register", "--path", str(repo_path), "--name", "repo-dp"]).exit_code == 0
    assert _run(["project", "create", "--name", "proj-a", "--repo", "repo-dp"]).exit_code == 0
    assert _run(
        ["scope", "create", "--project", "proj-a", "--scope", "ingestion", "--root-paths", "src/,tests/"]
    ).exit_code == 0

    # A second project sharing the same repo.
    assert _run(["project", "create", "--name", "proj-b", "--repo", "repo-dp"]).exit_code == 0

    out = _run(["status"]).output
    assert "No runs." in out  # no runs created via CLI in M0.5

    repos_out = _run(["repo", "list"]).output
    assert "repo-dp" in repos_out
    projects_out = _run(["project", "list"]).output
    assert "proj-a" in projects_out and "proj-b" in projects_out


def test_cli_status_without_init_errors(anvil_home: Path):
    result = _run(["status"])
    assert result.exit_code != 0
    assert "anvil init" in result.output


def test_cli_register_bad_repo_id(anvil_home: Path, git_repo_factory):
    repo_path = git_repo_factory("svc")
    _run(["init"])
    result = _run(["repo", "register", "--path", str(repo_path), "--name", "SVC"])
    assert result.exit_code != 0


def test_cli_leases_for_repo(anvil_home: Path, git_repo_factory):
    repo_path = git_repo_factory("svc")
    _run(["init"])
    _run(["repo", "register", "--path", str(repo_path), "--name", "repo-svc"])
    _run(["project", "create", "--name", "proj-svc", "--repo", "repo-svc"])
    result = _run(["leases", "--repo", "repo-svc"])
    assert result.exit_code == 0
    assert "No active leases" in result.output


def test_cli_pause_resume_roundtrip(anvil_home: Path, git_repo_factory):
    """Drive pause/resume through the CLI; pipeline_state stays put."""
    repo_path = git_repo_factory("svc")
    _run(["init"])
    _run(["repo", "register", "--path", str(repo_path), "--name", "repo-svc"])
    _run(["project", "create", "--name", "proj-svc", "--repo", "repo-svc"])

    # Create a run + advance pipeline state through the library (no `anvil run`
    # in M0.5), then exercise the CLI pause/resume commands.
    from anvil.registry import Registry

    reg = Registry()
    reg.create_run("RUN-20260601-001", "proj-svc", "repo-svc", "tester")
    reg.set_pipeline_state("RUN-20260601-001", "PLAN_CREATED")
    reg.set_lifecycle_state("RUN-20260601-001", "active")
    reg.close()

    paused = _run(["pause", "RUN-20260601-001"])
    assert paused.exit_code == 0
    assert "pipeline=PLAN_CREATED" in paused.output

    resumed = _run(["resume", "RUN-20260601-001"])
    assert resumed.exit_code == 0
    assert "lifecycle=active" in resumed.output
    assert "pipeline=PLAN_CREATED" in resumed.output


def _bootstrap_run(repo_path: Path):
    """Init + register + project + a created run via the library, return run id."""
    from anvil.registry import Registry

    _run(["init"])
    _run(["repo", "register", "--path", str(repo_path), "--name", "repo-svc"])
    _run(["project", "create", "--name", "proj-svc", "--repo", "repo-svc"])
    reg = Registry()
    reg.create_run("RUN-20260601-001", "proj-svc", "repo-svc", "tester")
    reg.set_lifecycle_state("RUN-20260601-001", "active")
    reg.close()
    return "RUN-20260601-001"


def test_cli_abort(anvil_home: Path, git_repo_factory):
    repo_path = git_repo_factory("svc")
    run_id = _bootstrap_run(repo_path)
    result = _run(["abort", run_id])
    assert result.exit_code == 0
    assert "lifecycle=aborted" in result.output
    # status reflects the aborted run.
    assert "aborted" in _run(["status"]).output


def test_cli_status_filters(anvil_home: Path, git_repo_factory):
    repo_path = git_repo_factory("svc")
    run_id = _bootstrap_run(repo_path)
    assert run_id in _run(["status", "--project", "proj-svc"]).output
    assert run_id in _run(["status", "--repo", "repo-svc"]).output
    # A non-matching filter shows no runs.
    assert "No runs." in _run(["status", "--project", "proj-other"]).output


def test_cli_active_lease_rendering(anvil_home: Path, git_repo_factory):
    repo_path = git_repo_factory("svc")
    run_id = _bootstrap_run(repo_path)
    from anvil.registry import Registry

    reg = Registry()
    reg.acquire_lease(run_id, "repo-svc", "file_write", "src/app.py")
    reg.close()
    out = _run(["leases", "--repo", "repo-svc"]).output
    assert "file_write" in out
    assert "src/app.py" in out
    assert run_id in out


def test_cli_leases_release_stale(anvil_home: Path, git_repo_factory):
    repo_path = git_repo_factory("svc")
    run_id = _bootstrap_run(repo_path)
    from anvil.registry import Registry

    reg = Registry()
    reg.acquire_lease(run_id, "repo-svc", "file_write", "src/app.py", expires_at="2000-01-01T00:00:00Z")
    reg.pause_run(run_id)
    reg.close()
    result = _run(["leases", "--repo", "repo-svc", "--release-stale"])
    assert result.exit_code == 0
    assert "Force-released" in result.output
    # Now no active leases remain.
    assert "No active leases" in _run(["leases", "--repo", "repo-svc"]).output


def test_cli_release_stale_is_repo_scoped(anvil_home: Path, git_repo_factory):
    """`leases --repo A --release-stale` must not release repo B's stale leases."""
    repo_a = git_repo_factory("a")
    repo_b = git_repo_factory("b")
    _run(["init"])
    _run(["repo", "register", "--path", str(repo_a), "--name", "repo-a"])
    _run(["repo", "register", "--path", str(repo_b), "--name", "repo-b"])
    _run(["project", "create", "--name", "proj-a", "--repo", "repo-a"])
    _run(["project", "create", "--name", "proj-b", "--repo", "repo-b"])

    from anvil.registry import Registry

    reg = Registry()
    reg.create_run("RUN-20260601-001", "proj-a", "repo-a", "tester")
    reg.activate_run("RUN-20260601-001")
    reg.acquire_lease("RUN-20260601-001", "repo-a", "file_write", "src/x.py", expires_at="2000-01-01T00:00:00Z")
    reg.pause_run("RUN-20260601-001")
    reg.create_run("RUN-20260601-002", "proj-b", "repo-b", "tester")
    reg.activate_run("RUN-20260601-002")
    reg.acquire_lease("RUN-20260601-002", "repo-b", "file_write", "src/y.py", expires_at="2000-01-01T00:00:00Z")
    reg.pause_run("RUN-20260601-002")
    reg.close()

    _run(["leases", "--repo", "repo-a", "--release-stale"])
    # repo-b's stale lease must still be active.
    assert "src/y.py" in _run(["leases", "--repo", "repo-b"]).output
    assert "No active leases" in _run(["leases", "--repo", "repo-a"]).output


def test_cli_pause_terminal_run_errors(anvil_home: Path, git_repo_factory):
    repo_path = git_repo_factory("svc")
    run_id = _bootstrap_run(repo_path)
    _run(["abort", run_id])
    result = _run(["pause", run_id])
    assert result.exit_code != 0
