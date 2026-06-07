"""Deterministic source discovery, baseline capture, and drift detection."""

from __future__ import annotations

import json
from pathlib import Path

from anvil.controller import Controller, RunInputs
from anvil.controller.baseline import normalize_pytest_identities, run_command
from anvil.controller.risk import FloorRules
from anvil.discovery import discover_sources
from anvil.schemas_util import validate_artifact

from tests import m1_fixtures
from tests.conftest import commit_change


def _inputs(env) -> RunInputs:
    return RunInputs(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["production_runtime_path"],
        floor=FloorRules(production_runtime_path_touched=True),
    )


def test_discovery_is_scope_aware_and_schema_valid(controller_env):
    env = controller_env
    scope = env.registry.load_project_config(env.project_id).task_scopes[env.scope_id]
    worktree = Path(env.registry.get_run(env.run_id)["worktree_path"])
    manifest = discover_sources(env.run_id, worktree, scope)

    assert validate_artifact("source_manifest", manifest) == []
    assert len(manifest["sources"]) >= 1
    # Every discovered path is within the scope focus.
    for src in manifest["sources"]:
        assert src["path"].startswith("src/config/") or src["path"].startswith("tests/")
    # Code sources carry a commit SHA (freshness requirement).
    code = [s for s in manifest["sources"] if s["source_type"] == "code"]
    assert code and all("commit_sha" in s["freshness"] for s in code)


def test_baseline_validation_written_and_valid(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_inputs(env))
    baseline = json.loads((env.run_dir / "baseline_validation.json").read_text())
    assert validate_artifact("baseline_validation", baseline) == []
    assert baseline["commands"]  # at least one command recorded
    assert "baseline_green" in baseline


def test_validation_results_and_worktree_manifest_valid(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_inputs(env))
    vr = json.loads((env.run_dir / "validation_results.json").read_text())
    wt = json.loads((env.run_dir / "worktree_manifest.json").read_text())
    assert validate_artifact("validation_results", vr) == []
    assert validate_artifact("worktree_manifest", wt) == []


def test_pytest_identity_normalization():
    stdout = (
        "tests/test_a.py::test_one PASSED\n"
        "tests/test_a.py::test_two FAILED\n"
        "some noise line\n"
    )
    ids = normalize_pytest_identities(stdout)
    assert {i["test_id"] for i in ids} == {"tests/test_a.py::test_one", "tests/test_a.py::test_two"}
    failed = [i for i in ids if i["status"] == "failed"][0]
    assert failed["failure_fingerprint"].startswith("sha256:")


def test_baseline_command_records_exit_code(controller_env):
    env = controller_env
    worktree = Path(env.registry.get_run(env.run_id)["worktree_path"])
    outcome = run_command(["git", "status", "--porcelain"], worktree)
    assert outcome.exit_code == 0
    assert outcome.passed is True


def test_drift_detected_when_target_branch_moves(controller_env):
    """When main moves after the run captured its base, the controller records
    drift (detection only — no rebase)."""
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    # Move main forward on the underlying repo after the run was created.
    commit_change(env.repo_path, "src/config/loader.py", "def load_config():\n    return {'k': 1}\n", "move main")

    Controller(env.registry, env.run_id).run(_inputs(env))
    drift_path = env.run_dir / "drift.json"
    assert drift_path.exists(), "drift should be detected and recorded"
    drift = json.loads(drift_path.read_text())
    assert drift["base_is_stale"] is True
    assert drift["rebase_required"] is True


def test_no_drift_when_target_unchanged(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_inputs(env))
    # Target never moved → no drift file written.
    assert not (env.run_dir / "drift.json").exists()
