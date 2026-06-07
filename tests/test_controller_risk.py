"""Risk scoring engine: factor registry, staged scoring, floor rules, escalation."""

from __future__ import annotations

import json

import pytest

from anvil.controller import Controller, RunInputs
from anvil.controller.risk import FloorRules, RiskEngine, mode_for_score
from anvil.schemas_util import validate_artifact

from tests import m1_fixtures


def test_mode_brackets():
    assert mode_for_score(0) == "fast"
    assert mode_for_score(2) == "fast"
    assert mode_for_score(3) == "standard"
    assert mode_for_score(6) == "standard"
    assert mode_for_score(7) == "critical"


def test_unknown_factor_id_rejected():
    engine = RiskEngine()
    with pytest.raises(ValueError):
        engine.assess("initial", ["not_a_real_factor"])


def test_floor_rules_force_standard_minimum():
    engine = RiskEngine()
    # Numeric score 0 (fast) but a dependency change floor violation.
    a = engine.assess("initial", [], floor=FloorRules(dependency_or_lockfile_changed=True))
    assert a.mode == "standard"
    assert a.mode_changed is True
    assert "floor rule" in (a.escalation_reason or "").lower()


def test_clean_floor_allows_fast():
    engine = RiskEngine()
    a = engine.assess("initial", ["generated_or_docs_only"], floor=FloorRules())
    assert a.mode == "fast"


def test_upward_only_escalation():
    engine = RiskEngine()
    engine.assess("initial", ["generated_or_docs_only"], floor=FloorRules())  # fast
    # Post-discovery reveals security work -> critical (4 -> not enough; add more).
    a = engine.assess("post_discovery", ["security_auth_data", "many_subsystems"], floor=FloorRules())
    assert a.mode == "critical"
    assert a.mode_changed is True
    # A later lower score must NOT drop the mode.
    b = engine.assess("post_plan", ["generated_or_docs_only"], floor=FloorRules())
    assert b.mode == "critical"
    assert b.mode_changed is False


def test_risk_assessment_artifact_is_schema_valid():
    engine = RiskEngine()
    engine.assess("initial", ["production_runtime_path"], floor=FloorRules(production_runtime_path_touched=True))
    engine.assess("post_discovery", ["production_runtime_path"], floor=FloorRules(production_runtime_path_touched=True))
    doc = engine.to_dict("RUN-20260601-001")
    assert validate_artifact("risk_assessment", doc) == []


def test_run_escalates_mode_on_post_discovery(controller_env):
    """A run that starts Fast but post-discovery surfaces risk escalates upward."""
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    inputs = RunInputs(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["generated_or_docs_only"],  # fast
        post_discovery_factor_ids=["security_auth_data", "production_runtime_path"],  # 7 => critical
        floor=FloorRules(),
    )
    card = Controller(env.registry, env.run_id).run(inputs)
    assert card["mode"] == "critical"
    assert card["mode_escalated"] is True

    events = [json.loads(line) for line in (env.run_dir / "event_log.jsonl").read_text().splitlines() if line.strip()]
    assert any(e["event_type"] == "mode_escalated" for e in events)
