"""Builders for the LLM-owned fixture artifacts a Milestone 1 dry run consumes.

These are SYNTHETIC, schema-valid artifacts written into a run directory so the
deterministic controller can validate and gate on them without any LLM. Each
builder returns a dict; ``write_all`` drops them into the run dir.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RUN_RE_OK = "RUN-20260601-001"


def task_contract(run_id: str = RUN_RE_OK) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_summary": "Add fallback validation to the config loader path.",
        "goals": ["Ensure missing config keys raise an explicit validation error."],
        "non_goals": ["Refactoring the whole config module."],
        "constraints": ["Maintain backward compatibility."],
        "forbidden_changes": ["src/runner/"],
        "acceptance_criteria": ["New tests fail before and pass after the change."],
        "status": "accepted",
    }


def gap_matrix(run_id: str = RUN_RE_OK, *, sufficient: bool = True) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "coverage_areas": [
            {
                "area": "code_entry_points",
                "required_level": "required",
                "evidence_found": True,
                "source_ids": ["SRC-001"],
                "gap_status": "covered",
            }
        ],
        "overall_sufficient": sufficient,
    }


def claim_ledger(run_id: str = RUN_RE_OK, *, source_id: str = "SRC-001") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "claims": [
            {
                "claim_id": "CLAIM-001",
                "claim": "load_config is the canonical entry point.",
                "claim_type": "code_behavior",
                "impact": "medium",
                "task_contract_ref": ["goals[0]"],
                "evidence": [
                    {
                        "source_id": source_id,
                        "source_type": "code",
                        "path": "src/config/loader.py",
                        "line_start": 1,
                        "line_end": 2,
                        "commit_sha": "abc1234",
                        "content_hash": "sha256:" + "a" * 64,
                        "checked_at": "2026-06-01T00:00:00Z",
                        "evidence_type": "direct",
                        "supports_claim_because": "Docstring confirms entry point.",
                    }
                ],
                "confidence": "high",
                "validated_by": ["controller"],
            }
        ],
    }


def issue_ledger(run_id: str = RUN_RE_OK, issues: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if issues is None:
        issues = [
            {
                "issue_id": "ISSUE-001",
                "title": "Backoff lacks a ceiling.",
                "severity": "medium",
                "raised_by": "codex",
                "layer": "layer3-cross-validation",
                "related_claims": ["CLAIM-001"],
                "resolution": "resolved_by_evidence",
                "closure_evidence": [{"kind": "command_output", "ref": "cmd-1"}],
                "safe_to_continue_without_resolution": True,
                "blocks_work_orders": [],
                "blocks_layers": [],
            }
        ]
    return {"run_id": run_id, "issues": issues}


def execution_work_orders(
    run_id: str = RUN_RE_OK, *, multi: bool = False
) -> dict[str, Any]:
    wo1 = {
        "work_order_id": "EXEC-001",
        "title": "Add fallback validation",
        "negotiation_status": "agreed",
        "agreed_by": ["claude-orchestrator", "codex-validator"],
        "criticality": "required",
        "fail_policy": "fail_closed",
        "assigned_scope": {"allowed_files": ["src/config/loader.py"]},
        "local_acceptance_criteria": ["New test passes."],
        "validation_commands": [{"command_array": ["pytest", "-q"], "expected_exit_code": 0}],
        "rollback_plan": [{"op": "restore_file", "target": "src/config/loader.py", "ref": "HEAD"}],
        "status": "ready_for_execution",
    }
    work_orders = [wo1]
    dependency_matrix = [{"work_order_id": "EXEC-001", "depends_on": [], "can_run_parallel": True}]
    if multi:
        wo_int = {
            "work_order_id": "EXEC-INT-001",
            "title": "Integration validation",
            "negotiation_status": "agreed",
            "agreed_by": ["claude-orchestrator", "codex-validator"],
            "criticality": "required",
            "fail_policy": "fail_closed",
            "is_integration_wo": True,
            "assigned_scope": {"allowed_files": ["tests/"]},
            "local_acceptance_criteria": ["Full suite passes."],
            "validation_commands": [{"command_array": ["pytest"], "expected_exit_code": 0}],
            "rollback_plan": [{"op": "revert_commit", "ref": "abc1234"}],
            "status": "ready_for_execution",
        }
        work_orders.append(wo_int)
        dependency_matrix.append(
            {"work_order_id": "EXEC-INT-001", "depends_on": ["EXEC-001"], "can_run_parallel": False}
        )
    return {
        "run_id": run_id,
        "work_orders": work_orders,
        "dependency_matrix": dependency_matrix,
    }


def guardrail_matrix(run_id: str = RUN_RE_OK, guardrails: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if guardrails is None:
        guardrails = [
            {
                "guardrail_id": "SEC-001",
                "description": "No hardcoded credentials.",
                "severity": "critical",
                "applies": True,
                "checked_by": ["security-reviewer"],
                "status": "pass",
                "waiver": None,
            }
        ]
    return {"run_id": run_id, "guardrails": guardrails}


def write_all(run_dir: Path, run_id: str = RUN_RE_OK, *, multi: bool = False) -> None:
    """Write the standard set of valid fixtures into the run directory."""
    artifacts = {
        "task_contract.json": task_contract(run_id),
        "gap_matrix.json": gap_matrix(run_id),
        "claim_ledger.json": claim_ledger(run_id),
        "issue_ledger.json": issue_ledger(run_id),
        "execution_work_orders.json": execution_work_orders(run_id, multi=multi),
        "guardrail_matrix.json": guardrail_matrix(run_id),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in artifacts.items():
        _write(run_dir / name, payload)


def write_one(run_dir: Path, name: str, payload: dict[str, Any]) -> None:
    _write(run_dir / name, payload)


def _write(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
