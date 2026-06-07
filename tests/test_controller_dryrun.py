"""Full deterministic dry-run state path + artifact production (Milestone 1)."""

from __future__ import annotations

import json
from pathlib import Path

from anvil.controller import Controller, RunInputs
from anvil.controller import states
from anvil.controller.risk import FloorRules
from anvil.schemas_util import validate_artifact

from tests import m1_fixtures


def _standard_inputs(env) -> RunInputs:
    # production_runtime_path => score 3 => standard; floor flagged accordingly.
    return RunInputs(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["production_runtime_path"],
        floor=FloorRules(production_runtime_path_touched=True),
    )


def test_full_standard_dry_run_reaches_finalized(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    card = Controller(env.registry, env.run_id).run(_standard_inputs(env))

    assert card["mode"] == "standard"
    assert card["final_outcome"] == "passed"
    state = json.loads((env.run_dir / "controller_state.json").read_text())
    assert state["current_state"] == states.FINALIZED


def test_dry_run_writes_all_deterministic_artifacts(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_standard_inputs(env))

    expected = {
        "baseline_validation.json": "baseline_validation",
        "source_manifest.json": "source_manifest",
        "risk_assessment.json": "risk_assessment",
        "validation_results.json": "validation_results",
        "worktree_manifest.json": "worktree_manifest",
        "run_scorecard.json": "run_scorecard",
        "controller_state.json": "controller_state",
    }
    for filename, schema in expected.items():
        path = env.run_dir / filename
        assert path.exists(), f"missing artifact {filename}"
        errors = validate_artifact(schema, json.loads(path.read_text()))
        assert errors == [], f"{filename} invalid: {errors}"


def test_event_log_records_every_state_transition(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_standard_inputs(env))

    events = [json.loads(line) for line in (env.run_dir / "event_log.jsonl").read_text().splitlines() if line.strip()]
    transitions = [e["state_after"] for e in events if e["event_type"] == "state_transition"]
    seq = states.sequence("standard", multi_wo=False)
    # Every state in the standard sequence appears as a transition target.
    for state in seq:
        assert state in transitions, f"no transition event for {state}"


def test_fast_mode_skips_research_states(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    inputs = RunInputs(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["generated_or_docs_only"],  # score -2 => fast
        floor=FloorRules(),
    )
    card = Controller(env.registry, env.run_id).run(inputs)
    assert card["mode"] == "fast"

    events = [json.loads(line) for line in (env.run_dir / "event_log.jsonl").read_text().splitlines() if line.strip()]
    transitions = {e["state_after"] for e in events if e["event_type"] == "state_transition"}
    # Fast mode must NOT pass through deep-research / review states.
    assert states.CLAIMS_RESEARCHED not in transitions
    assert states.BLINDSPOT_SCAN_COMPLETE not in transitions
    assert states.CROSS_VALIDATION_PENDING not in transitions
    assert states.PLAN_REVIEWED not in transitions


def test_multi_wo_run_goes_through_integration_validated(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id, multi=True)
    inputs = _standard_inputs(env)
    inputs.multi_wo = True
    Controller(env.registry, env.run_id).run(inputs)

    events = [json.loads(line) for line in (env.run_dir / "event_log.jsonl").read_text().splitlines() if line.strip()]
    transitions = [e["state_after"] for e in events if e["event_type"] == "state_transition"]
    assert states.INTEGRATION_VALIDATED in transitions
    # INTEGRATION_VALIDATED precedes READY_FOR_COMMIT_REVIEW.
    assert transitions.index(states.INTEGRATION_VALIDATED) < transitions.index(states.READY_FOR_COMMIT_REVIEW)


def test_single_wo_skips_integration(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    Controller(env.registry, env.run_id).run(_standard_inputs(env))
    events = [json.loads(line) for line in (env.run_dir / "event_log.jsonl").read_text().splitlines() if line.strip()]
    transitions = {e["state_after"] for e in events if e["event_type"] == "state_transition"}
    assert states.INTEGRATION_VALIDATED not in transitions
    assert states.READY_FOR_COMMIT_REVIEW in transitions
