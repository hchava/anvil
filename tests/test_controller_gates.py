"""Gate-blocking behavior (Milestone 1): issue closure + guardrail gates."""

from __future__ import annotations

import pytest

from anvil.controller import Controller, ControllerError, RunInputs
from anvil.controller.gates import (
    GateError,
    cross_validation_gate,
    guardrail_gate,
    issue_closure_gate,
)
from anvil.controller.risk import FloorRules

from tests import m1_fixtures


def _standard_inputs(env) -> RunInputs:
    return RunInputs(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["production_runtime_path"],
        floor=FloorRules(production_runtime_path_touched=True),
    )


# ----- unit-level gate tests -------------------------------------------------

def test_issue_closure_blocks_open_critical():
    ledger = {"issues": [{"issue_id": "ISSUE-001", "severity": "critical", "resolution": "open"}]}
    with pytest.raises(GateError):
        issue_closure_gate(ledger)


def test_issue_closure_blocks_unsafe_deferred():
    ledger = {
        "issues": [
            {
                "issue_id": "ISSUE-002",
                "severity": "high",
                "resolution": "deferred",
                "human_decision_ref": "DEC-001",
                "safe_to_continue_without_resolution": False,
            }
        ]
    }
    with pytest.raises(GateError):
        issue_closure_gate(ledger)


def test_issue_closure_blocks_deferred_without_human_decision():
    ledger = {
        "issues": [
            {
                "issue_id": "ISSUE-003",
                "severity": "critical",
                "resolution": "deferred",
                "safe_to_continue_without_resolution": True,
            }
        ]
    }
    with pytest.raises(GateError):
        issue_closure_gate(ledger)


def test_issue_closure_passes_safe_deferred_with_human_decision():
    ledger = {
        "issues": [
            {
                "issue_id": "ISSUE-004",
                "severity": "high",
                "resolution": "deferred",
                "human_decision_ref": "DEC-001",
                "safe_to_continue_without_resolution": True,
            }
        ]
    }
    issue_closure_gate(ledger)  # no raise


def test_issue_closure_passes_resolved_by_evidence():
    ledger = {
        "issues": [
            {
                "issue_id": "ISSUE-005",
                "severity": "critical",
                "resolution": "resolved_by_evidence",
                "closure_evidence": [{"kind": "command_output", "ref": "cmd-1"}],
            }
        ]
    }
    issue_closure_gate(ledger)


def test_guardrail_gate_blocks_critical_not_checked():
    matrix = {
        "guardrails": [
            {"guardrail_id": "SEC-1", "severity": "critical", "applies": True, "status": "not_checked"}
        ]
    }
    with pytest.raises(GateError):
        guardrail_gate(matrix, "standard")


def test_guardrail_gate_blocks_critical_waived_without_human_approval():
    matrix = {
        "guardrails": [
            {
                "guardrail_id": "SEC-2",
                "severity": "critical",
                "applies": True,
                "status": "waived",
                "waiver": {"reason": "x", "approved_by": "u", "human_approved": False},
            }
        ]
    }
    with pytest.raises(GateError):
        guardrail_gate(matrix, "standard")


def test_guardrail_gate_allows_critical_human_waived():
    matrix = {
        "guardrails": [
            {
                "guardrail_id": "SEC-3",
                "severity": "critical",
                "applies": True,
                "status": "waived",
                "waiver": {"reason": "x", "approved_by": "u", "human_approved": True},
            }
        ]
    }
    guardrail_gate(matrix, "standard")


def test_cross_validation_gate_blocks_open_high():
    ledger = {"issues": [{"issue_id": "ISSUE-006", "severity": "high", "resolution": "open"}]}
    with pytest.raises(GateError):
        cross_validation_gate(ledger)


# ----- end-to-end: a bad fixture fails the run closed ------------------------

def test_run_fails_closed_on_open_critical_issue(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    # Replace the issue ledger with an open critical issue.
    bad = m1_fixtures.issue_ledger(
        env.run_id,
        issues=[
            {
                "issue_id": "ISSUE-001",
                "title": "Open critical.",
                "severity": "critical",
                "raised_by": "codex",
                "layer": "layer3",
                "resolution": "open",
                "safe_to_continue_without_resolution": False,
                "blocks_work_orders": [],
                "blocks_layers": [],
            }
        ],
    )
    m1_fixtures.write_one(env.run_dir, "issue_ledger.json", bad)
    with pytest.raises(ControllerError):
        Controller(env.registry, env.run_id).run(_standard_inputs(env))


def test_run_fails_closed_on_critical_not_checked_guardrail(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    bad = m1_fixtures.guardrail_matrix(
        env.run_id,
        guardrails=[
            {
                "guardrail_id": "SEC-001",
                "description": "No hardcoded creds.",
                "severity": "critical",
                "applies": True,
                "status": "not_checked",
                "waiver": None,
            }
        ],
    )
    m1_fixtures.write_one(env.run_dir, "guardrail_matrix.json", bad)
    with pytest.raises(ControllerError):
        Controller(env.registry, env.run_id).run(_standard_inputs(env))
