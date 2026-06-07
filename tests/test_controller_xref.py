"""Cross-reference validators catch dangling SRC/CLAIM/ISSUE/task/WO refs."""

from __future__ import annotations

import pytest

from anvil.controller import Controller, ControllerError, RunInputs
from anvil.controller.gates import (
    GateError,
    validate_claim_refs,
    validate_file_scope_against_contract,
    validate_issue_refs,
    validate_source_refs,
    validate_task_contract_refs,
    validate_work_order_dependencies,
)
from anvil.controller.risk import FloorRules

from tests import m1_fixtures


def test_validate_source_refs_catches_unknown_source():
    claims = {"claims": [{"claim_id": "CLAIM-001", "evidence": [{"source_id": "SRC-999"}]}]}
    sources = {"sources": [{"source_id": "SRC-001"}]}
    with pytest.raises(GateError):
        validate_source_refs(claims, sources)


def test_validate_claim_refs_catches_unknown_claim():
    issues = {"issues": [{"issue_id": "ISSUE-001", "related_claims": ["CLAIM-999"]}]}
    claims = {"claims": [{"claim_id": "CLAIM-001"}]}
    with pytest.raises(GateError):
        validate_claim_refs(issues, claims)


def test_validate_issue_refs_catches_unknown_work_order():
    issues = {"issues": [{"issue_id": "ISSUE-001", "blocks_work_orders": ["EXEC-999"]}]}
    work_orders = {"work_orders": [{"work_order_id": "EXEC-001"}]}
    with pytest.raises(GateError):
        validate_issue_refs(issues, work_orders)


def test_validate_task_contract_refs_catches_missing_ref():
    claims = {"claims": [{"claim_id": "CLAIM-001", "task_contract_ref": []}]}
    with pytest.raises(GateError):
        validate_task_contract_refs(claims, {})


def test_validate_work_order_dependencies_catches_cycle():
    work_orders = {
        "work_orders": [{"work_order_id": "EXEC-001"}, {"work_order_id": "EXEC-002"}],
        "dependency_matrix": [
            {"work_order_id": "EXEC-001", "depends_on": ["EXEC-002"], "can_run_parallel": False},
            {"work_order_id": "EXEC-002", "depends_on": ["EXEC-001"], "can_run_parallel": False},
        ],
    }
    with pytest.raises(GateError):
        validate_work_order_dependencies(work_orders)


def test_validate_work_order_dependencies_catches_unknown_dep():
    work_orders = {
        "work_orders": [{"work_order_id": "EXEC-001"}],
        "dependency_matrix": [
            {"work_order_id": "EXEC-001", "depends_on": ["EXEC-404"], "can_run_parallel": False}
        ],
    }
    with pytest.raises(GateError):
        validate_work_order_dependencies(work_orders)


def test_validate_file_scope_against_contract_catches_forbidden():
    work_orders = {
        "work_orders": [
            {"work_order_id": "EXEC-001", "assigned_scope": {"allowed_files": ["src/runner/run.py"]}}
        ]
    }
    contract = {"forbidden_changes": ["src/runner/"]}
    with pytest.raises(GateError):
        validate_file_scope_against_contract(work_orders, contract)


def test_run_blocks_on_dangling_claim_source(controller_env):
    """End-to-end: a claim pointing at a non-existent source fails the run."""
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    bad_claims = m1_fixtures.claim_ledger(env.run_id, source_id="SRC-404")
    m1_fixtures.write_one(env.run_dir, "claim_ledger.json", bad_claims)
    inputs = RunInputs(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["production_runtime_path"],
        floor=FloorRules(production_runtime_path_touched=True),
    )
    with pytest.raises(ControllerError):
        Controller(env.registry, env.run_id).run(inputs)


def test_run_blocks_on_forbidden_file_scope(controller_env):
    env = controller_env
    m1_fixtures.write_all(env.run_dir, env.run_id)
    # Work order touches a forbidden path (contract forbids src/runner/).
    bad_wo = m1_fixtures.execution_work_orders(env.run_id)
    bad_wo["work_orders"][0]["assigned_scope"]["allowed_files"] = ["src/runner/run.py"]
    m1_fixtures.write_one(env.run_dir, "execution_work_orders.json", bad_wo)
    inputs = RunInputs(
        run_id=env.run_id,
        project_id=env.project_id,
        repo_id=env.repo_id,
        scope_id=env.scope_id,
        initial_factor_ids=["production_runtime_path"],
        floor=FloorRules(production_runtime_path_touched=True),
    )
    with pytest.raises(ControllerError):
        Controller(env.registry, env.run_id).run(inputs)
