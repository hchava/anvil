"""Integration tests for WorkOrderExecutor (Milestone 3).

Covers:
  - Successful execution in per-run worktree
  - Lease acquisition and release (success and failure paths)
  - Lease conflict blocks execution
  - Scope violation (forbidden file) triggers rollback
  - Scope violation (out-of-scope file) triggers rollback
  - Secret in diff triggers rollback
  - Validation command failure triggers rollback
  - Baseline diff detects new test failures
  - worktree_manifest.json is written and schema-valid
  - validation_results.json is written and schema-valid
  - Logs do not expose secret values

All tests use temp dirs and synthetic git repos. No network, no API keys,
no real Claude/Codex/tmux, no writes to real ~/.anvil.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from anvil.controller.events import EventLog
from anvil.controller.policy import CommandPolicy
from anvil.errors import LeaseConflictError
from anvil.executor import WorkOrderExecutor
from anvil.paths import ANVIL_HOME_ENV
from anvil.registry import Registry
from anvil.schemas_util import validate_artifact


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def anvil_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "anvil-home"
    monkeypatch.setenv(ANVIL_HOME_ENV, str(home))
    return home


@pytest.fixture
def registry(anvil_home: Path):
    reg = Registry()
    reg.init()
    try:
        yield reg
    finally:
        reg.close()


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / "repos" / name
    repo.mkdir(parents=True)
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Anvil Test"], repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "src" / "config.py").write_text("DEBUG = False\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "initial commit"], repo)
    return repo


def _setup_run(
    tmp_path: Path,
    registry: Registry,
    run_id: str = "RUN-20260607-001",
    repo_name: str = "my-service",
) -> tuple[Path, Path, Path]:
    """Create repo, register everything, activate run. Returns (repo, worktree, run_dir)."""
    repo = _make_repo(tmp_path, repo_name)
    repo_id = f"repo-{repo_name}"
    proj_id = f"proj-{repo_name}"
    registry.register_repo(repo_id, repo)
    registry.create_project(proj_id, [repo_id])
    registry.create_run(run_id, proj_id, repo_id, "tester")
    registry.activate_run(run_id)
    run_row = registry.get_run(run_id)
    worktree = Path(run_row["worktree_path"])
    run_dir = registry.paths.run_dir(run_id)
    return repo, worktree, run_dir


def _make_work_order(
    work_order_id: str = "EXEC-001",
    allowed_files: list[str] | None = None,
    forbidden_files: list[str] | None = None,
    validation_commands: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "work_order_id": work_order_id,
        "title": "Test work order",
        "negotiation_status": "agreed",
        "criticality": "required",
        "fail_policy": "fail_closed",
        "local_acceptance_criteria": ["Tests pass"],
        "assigned_scope": {
            "allowed_files": allowed_files or ["src/app.py"],
            "forbidden_files": forbidden_files or [],
        },
        "validation_commands": validation_commands or [
            {"command_array": ["python3", "--version"], "expected_exit_code": 0}
        ],
        "rollback_plan": [{"op": "noop"}],
    }


def _make_executor(
    registry: Registry,
    worktree: Path,
    run_dir: Path,
    work_order: dict,
    repo_id: str,
    run_id: str = "RUN-20260607-001",
    policy: CommandPolicy | None = None,
    baseline_tests: list[dict] | None = None,
) -> WorkOrderExecutor:
    event_log = EventLog(run_dir / "event_log.jsonl")
    return WorkOrderExecutor(
        run_id=run_id,
        run_dir=run_dir,
        worktree_path=worktree,
        work_order=work_order,
        repo_id=repo_id,
        registry=registry,
        event_log=event_log,
        policy=policy or CommandPolicy.from_dict({"allowed_binaries": ["python3"]}),
        baseline_tests=baseline_tests,
    )


# ---------------------------------------------------------------------------
# Fake execution agents
# ---------------------------------------------------------------------------

def _agent_writes_allowed_file(worktree: Path) -> None:
    (worktree / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")


def _agent_writes_forbidden_file(worktree: Path) -> None:
    (worktree / "src" / "config.py").write_text("DEBUG = True\n", encoding="utf-8")


def _agent_writes_out_of_scope_file(worktree: Path) -> None:
    (worktree / "src" / "new_module.py").write_text("EXTRA = 1\n", encoding="utf-8")


def _agent_writes_secret_in_file(worktree: Path) -> None:
    fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
    (worktree / "src" / "app.py").write_text(
        f"VALUE = 2\nKEY = '{fake_key}'\n", encoding="utf-8"
    )


def _agent_writes_nothing(worktree: Path) -> None:
    pass


# ---------------------------------------------------------------------------
# Successful execution
# ---------------------------------------------------------------------------

def test_successful_execution_returns_success(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order()
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_allowed_file)

    assert result.status == "success"


def test_successful_execution_touched_files_populated(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    executor = _make_executor(
        registry, worktree, run_dir, _make_work_order(), "repo-my-service"
    )
    result = executor.execute(_agent_writes_allowed_file)
    assert "src/app.py" in result.touched_files


def test_no_changes_still_succeeds(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    executor = _make_executor(
        registry, worktree, run_dir, _make_work_order(), "repo-my-service"
    )
    result = executor.execute(_agent_writes_nothing)
    assert result.status == "success"


# ---------------------------------------------------------------------------
# Lease lifecycle
# ---------------------------------------------------------------------------

def test_leases_acquired_before_execution(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(allowed_files=["src/app.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_nothing)

    # After success, all leases are released.
    assert result.status == "success"
    assert len(result.acquired_lease_ids) == 1
    active = registry.list_leases(repo_id="repo-my-service", active_only=True)
    assert len(active) == 0


def test_leases_released_on_scope_violation(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(allowed_files=["src/app.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_out_of_scope_file)

    # Execution fails, leases still released.
    assert result.status in ("scope_violation", "rollback_completed", "rollback_error")
    active = registry.list_leases(repo_id="repo-my-service", active_only=True)
    assert len(active) == 0


def test_leases_released_on_validation_failure(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    failing_cmd = {"command_array": ["python3", "nonexistent_fail_script.py"], "expected_exit_code": 0}
    work_order = _make_work_order(validation_commands=[failing_cmd])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_nothing)

    active = registry.list_leases(repo_id="repo-my-service", active_only=True)
    assert len(active) == 0


def test_lease_conflict_blocks_execution(tmp_path, registry):
    """A second run on the same repo+file should get a lease conflict."""
    repo, worktree, run_dir = _setup_run(tmp_path, registry, "RUN-20260607-001")

    # Register a second run that holds a conflicting lease.
    repo2_id = "repo-my-service"
    registry.create_run("RUN-20260607-002", "proj-my-service", repo2_id, "tester2")
    registry.activate_run("RUN-20260607-002")
    registry.acquire_lease("RUN-20260607-002", repo2_id, "file_write", "src/app.py")

    work_order = _make_work_order(allowed_files=["src/app.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, repo2_id)

    result = executor.execute(_agent_writes_nothing)

    assert result.status == "lease_conflict"
    assert result.error is not None


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------

def test_forbidden_file_triggers_rollback(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(
        allowed_files=["src/app.py", "src/config.py"],
        forbidden_files=["src/config.py"],
    )
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_forbidden_file)

    assert result.status in ("scope_violation", "rollback_completed", "rollback_error")
    assert result.scope_result is not None
    assert not result.scope_result.passed
    assert result.rollback_result is not None
    # config.py must be restored to its original state.
    assert (worktree / "src" / "config.py").read_text() == "DEBUG = False\n"


def test_out_of_scope_file_triggers_rollback(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(allowed_files=["src/app.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_out_of_scope_file)

    assert result.status in ("scope_violation", "rollback_completed", "rollback_error")
    # The new out-of-scope file must be removed.
    assert not (worktree / "src" / "new_module.py").exists()


# ---------------------------------------------------------------------------
# Secret scanner
# ---------------------------------------------------------------------------

def test_secret_in_diff_triggers_rollback(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(allowed_files=["src/app.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_secret_in_file)

    assert result.status in ("secret_detected", "rollback_completed", "rollback_error")
    assert result.scan_result is not None
    assert result.scan_result.has_secrets


def test_logs_do_not_expose_secret_values(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(allowed_files=["src/app.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    executor.execute(_agent_writes_secret_in_file)

    # Check event log — the raw key must not appear.
    event_log_path = run_dir / "event_log.jsonl"
    fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
    if event_log_path.exists():
        log_text = event_log_path.read_text(encoding="utf-8")
        assert fake_key not in log_text

    # Scan findings also must not contain the raw key.
    # (Verified via test_m3_scanner.py but check executor result too.)


def test_scan_findings_do_not_contain_raw_secret(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(allowed_files=["src/app.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_secret_in_file)

    fake_key = "AKIA" + "IOSFODNN7EXAMPLE"
    if result.scan_result:
        for finding in result.scan_result.findings:
            assert fake_key not in finding.redacted_excerpt


# ---------------------------------------------------------------------------
# Validation commands
# ---------------------------------------------------------------------------

def test_validation_command_failure_triggers_rollback(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    # Command that always fails
    fail_cmd = {"command_array": ["python3", "definitely_nonexistent_file.py"], "expected_exit_code": 0}
    work_order = _make_work_order(
        allowed_files=["src/app.py"],
        validation_commands=[fail_cmd],
    )
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_allowed_file)

    assert result.status in ("validation_failed", "rollback_completed", "rollback_error")
    # app.py must be restored.
    assert (worktree / "src" / "app.py").read_text() == "VALUE = 1\n"


def test_policy_blocked_command_causes_validation_failure(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    blocked_cmd = {"command_array": ["curl", "http://example.com"], "expected_exit_code": 0}
    work_order = _make_work_order(
        allowed_files=["src/app.py"],
        validation_commands=[blocked_cmd],
    )
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    result = executor.execute(_agent_writes_nothing)

    # Policy blocked → command failed → validation_failed → rollback
    assert result.status in ("validation_failed", "rollback_completed", "rollback_error")
    assert not result.command_results[0].policy_allowed


# ---------------------------------------------------------------------------
# Baseline comparison
# ---------------------------------------------------------------------------

def test_baseline_diff_detects_new_failure(tmp_path, registry):
    """A test_id passing in baseline but failing post-execution is a new failure."""
    # Pre-stage a failing pytest shim in the repo so it's tracked in HEAD.
    repo = _make_repo(tmp_path, "baseline-svc")
    script_content = (
        "print('tests/test_app.py::test_value FAILED')\n"
        "import sys; sys.exit(1)\n"
    )
    (repo / "fake_pytest.py").write_text(script_content, encoding="utf-8")
    _git(["add", "fake_pytest.py"], repo)
    _git(["commit", "-m", "add fake pytest shim"], repo)

    repo_id = "repo-baseline-svc"
    proj_id = "proj-baseline-svc"
    run_id = "RUN-20260607-009"
    registry.register_repo(repo_id, repo)
    registry.create_project(proj_id, [repo_id])
    registry.create_run(run_id, proj_id, repo_id, "tester")
    registry.activate_run(run_id)
    run_row = registry.get_run(run_id)
    worktree = Path(run_row["worktree_path"])
    run_dir = registry.paths.run_dir(run_id)

    baseline = [
        {"test_id": "tests/test_app.py::test_value", "status": "passed", "failure_fingerprint": ""}
    ]
    val_cmd = {"command_array": ["python3", "fake_pytest.py"], "expected_exit_code": 0}

    work_order = _make_work_order(
        work_order_id="EXEC-009",
        allowed_files=["src/app.py"],
        validation_commands=[val_cmd],
    )
    executor = _make_executor(
        registry, worktree, run_dir, work_order, repo_id,
        run_id=run_id,
        baseline_tests=baseline,
    )

    result = executor.execute(_agent_writes_nothing)

    assert result.status in ("validation_failed", "rollback_completed", "rollback_error")
    assert "tests/test_app.py::test_value" in result.new_failures


def test_baseline_diff_no_new_failures_when_baseline_empty(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order()
    executor = _make_executor(
        registry, worktree, run_dir, work_order, "repo-my-service", baseline_tests=[]
    )
    result = executor.execute(_agent_writes_nothing)
    assert result.new_failures == []


# ---------------------------------------------------------------------------
# Artifact writing
# ---------------------------------------------------------------------------

def test_worktree_manifest_written_on_success(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    executor = _make_executor(
        registry, worktree, run_dir, _make_work_order(), "repo-my-service"
    )
    executor.execute(_agent_writes_nothing)

    manifest_path = run_dir / "worktree_manifest.json"
    assert manifest_path.exists()
    doc = json.loads(manifest_path.read_text())
    assert doc["work_order_id"] == "EXEC-001"
    assert doc["execution_status"] == "success"
    assert doc["validation_status"] == "passed"


def test_worktree_manifest_written_on_failure(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(
        allowed_files=["src/app.py"],
        forbidden_files=["src/app.py"],
    )
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")
    executor.execute(_agent_writes_allowed_file)

    manifest_path = run_dir / "worktree_manifest.json"
    assert manifest_path.exists()
    doc = json.loads(manifest_path.read_text())
    assert doc["execution_status"] != "success"
    assert doc["validation_status"] == "failed"


def test_validation_results_written_on_success(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    executor = _make_executor(
        registry, worktree, run_dir, _make_work_order(), "repo-my-service"
    )
    executor.execute(_agent_writes_nothing)

    results_path = run_dir / "validation_results.json"
    assert results_path.exists()
    doc = json.loads(results_path.read_text())
    assert doc["run_id"] == "RUN-20260607-001"
    assert doc["work_order_ref"] == "EXEC-001"
    assert doc["overall_passed"] is True
    assert len(doc["results"]) >= 1


def test_validation_results_schema_valid(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    executor = _make_executor(
        registry, worktree, run_dir, _make_work_order(), "repo-my-service"
    )
    executor.execute(_agent_writes_nothing)

    doc = json.loads((run_dir / "validation_results.json").read_text())
    assert validate_artifact("validation_results", doc) == []


def test_worktree_manifest_schema_valid(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    executor = _make_executor(
        registry, worktree, run_dir, _make_work_order(), "repo-my-service"
    )
    executor.execute(_agent_writes_nothing)

    doc = json.loads((run_dir / "worktree_manifest.json").read_text())
    assert validate_artifact("worktree_manifest", doc) == []


def test_worktree_manifest_schema_valid_on_scope_violation(tmp_path, registry):
    """Manifest written on failure paths must also be schema-valid."""
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(allowed_files=["src/app.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")
    executor.execute(_agent_writes_out_of_scope_file)

    doc = json.loads((run_dir / "worktree_manifest.json").read_text())
    assert validate_artifact("worktree_manifest", doc) == []


def test_file_ownership_in_manifest(tmp_path, registry):
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(allowed_files=["src/app.py", "src/config.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")
    executor.execute(_agent_writes_nothing)

    doc = json.loads((run_dir / "worktree_manifest.json").read_text())
    assert "file_ownership" in doc
    paths = {e["file_path"] for e in doc["file_ownership"]}
    assert "src/app.py" in paths
    assert "src/config.py" in paths


# ---------------------------------------------------------------------------
# Blocker repro tests (guard regressions on the four fixed issues)
# ---------------------------------------------------------------------------

def test_partial_lease_acquisition_releases_acquired_leases(tmp_path, registry):
    """Blocker 2 repro: if the 2nd file conflicts, the 1st lease must be released."""
    repo, worktree, run_dir = _setup_run(tmp_path, registry, "RUN-20260607-010")

    # Grab a conflicting lease on config.py from a different run.
    registry.create_run("RUN-20260607-011", "proj-my-service", "repo-my-service", "other")
    registry.activate_run("RUN-20260607-011")
    registry.acquire_lease("RUN-20260607-011", "repo-my-service", "file_write", "src/config.py")

    # Request app.py (OK) and config.py (conflicts).
    work_order = _make_work_order(
        allowed_files=["src/app.py", "src/config.py"],
        work_order_id="EXEC-010",
    )
    executor = _make_executor(
        registry, worktree, run_dir, work_order, "repo-my-service", run_id="RUN-20260607-010"
    )
    result = executor.execute(_agent_writes_nothing)

    assert result.status == "lease_conflict"
    # The first lease (app.py) must have been released — only RUN-011's lease remains active.
    active = registry.list_leases(repo_id="repo-my-service", active_only=True)
    run_010_leases = [r for r in active if r["run_id"] == "RUN-20260607-010"]
    assert len(run_010_leases) == 0, "Partial lease from RUN-010 must be released on conflict"


def test_agent_exception_triggers_rollback_and_writes_artifacts(tmp_path, registry):
    """Blocker 4 repro: an exception from execution_agent must rollback and write artifacts."""
    repo, worktree, run_dir = _setup_run(tmp_path, registry)
    work_order = _make_work_order(allowed_files=["src/app.py"])
    executor = _make_executor(registry, worktree, run_dir, work_order, "repo-my-service")

    def crashing_agent(wt: Path) -> None:
        (wt / "src" / "app.py").write_text("PARTIAL WRITE\n", encoding="utf-8")
        raise RuntimeError("agent failed mid-execution")

    result = executor.execute(crashing_agent)

    assert result.status in ("agent_error", "rollback_error")
    assert result.error is not None
    # Worktree must be restored.
    assert (worktree / "src" / "app.py").read_text() == "VALUE = 1\n"
    # Artifacts must be written despite the exception.
    assert (run_dir / "worktree_manifest.json").exists()
    assert (run_dir / "validation_results.json").exists()
    # Leases must be released.
    active = registry.list_leases(repo_id="repo-my-service", active_only=True)
    assert len(active) == 0
