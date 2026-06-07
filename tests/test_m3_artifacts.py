"""Schema validation tests for M3 artifact updates (Milestone 3).

Verifies that the updated worktree_manifest and validation_results schemas
accept both the pre-M3 fixtures (backward compatibility) and new M3 fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anvil.schemas_util import validate_artifact


FIXTURES_VALID = Path(__file__).parent / "fixtures" / "valid"


# ---------------------------------------------------------------------------
# Backward compatibility: existing M0 fixtures still validate
# ---------------------------------------------------------------------------

def test_worktree_manifest_existing_fixture_still_valid() -> None:
    doc = json.loads((FIXTURES_VALID / "worktree_manifest" / "basic.json").read_text())
    validate_artifact("worktree_manifest", doc)  # Must not raise.


def test_validation_results_existing_fixture_still_valid() -> None:
    doc = json.loads((FIXTURES_VALID / "validation_results" / "basic.json").read_text())
    validate_artifact("validation_results", doc)  # Must not raise.


# ---------------------------------------------------------------------------
# New M3 fields accepted as optional extensions
# ---------------------------------------------------------------------------

def test_worktree_manifest_with_m3_execution_fields() -> None:
    doc = json.loads((FIXTURES_VALID / "worktree_manifest" / "basic.json").read_text())
    doc["work_order_id"] = "EXEC-001"
    doc["execution_status"] = "success"
    doc["touched_files"] = ["src/app.py"]
    doc["validation_status"] = "passed"
    doc["file_ownership"] = [
        {
            "file_path": "src/app.py",
            "owners": [
                {"work_order_id": "EXEC-001", "access": "write", "sequence": 1}
            ],
        }
    ]
    validate_artifact("worktree_manifest", doc)


def test_worktree_manifest_with_rollback_status() -> None:
    doc = json.loads((FIXTURES_VALID / "worktree_manifest" / "basic.json").read_text())
    doc["work_order_id"] = "EXEC-001"
    doc["execution_status"] = "validation_failed"
    doc["touched_files"] = ["src/app.py"]
    doc["validation_status"] = "failed"
    doc["rollback_status"] = "success"
    validate_artifact("worktree_manifest", doc)


def test_worktree_manifest_rejects_unknown_execution_status() -> None:
    doc = json.loads((FIXTURES_VALID / "worktree_manifest" / "basic.json").read_text())
    doc["execution_status"] = "flying"
    errors = validate_artifact("worktree_manifest", doc)
    assert errors, "Expected a schema validation error for unknown execution_status"


def test_validation_results_with_timing_fields() -> None:
    doc = json.loads((FIXTURES_VALID / "validation_results" / "basic.json").read_text())
    doc["results"][0]["started_at"] = "2026-06-07T10:00:00Z"
    doc["results"][0]["finished_at"] = "2026-06-07T10:00:01Z"
    doc["results"][0]["duration_seconds"] = 1.23
    doc["results"][0]["timed_out"] = False
    doc["results"][0]["policy_allowed"] = True
    validate_artifact("validation_results", doc)


def test_validation_results_rejects_empty_results_array() -> None:
    doc = json.loads((FIXTURES_VALID / "validation_results" / "basic.json").read_text())
    doc["results"] = []
    errors = validate_artifact("validation_results", doc)
    assert errors, "Expected a schema validation error for empty results array"


# ---------------------------------------------------------------------------
# Inline full-document tests (no fixture dependency)
# ---------------------------------------------------------------------------

def test_complete_m3_worktree_manifest_validates() -> None:
    doc = {
        "run_id": "RUN-20260607-001",
        "schema_version": "1.0.0",
        "created_at": "2026-06-07T10:00:00Z",
        "worktree_id": "wt-001",
        "base_repo": "my-service",
        "base_commit": "abc1234",
        "branch": "anvil/proj-my-service/RUN-20260607-001",
        "path": "/tmp/anvil/worktrees/RUN-20260607-001",
        "status": "active",
        "work_order_id": "EXEC-001",
        "execution_status": "success",
        "touched_files": ["src/app.py"],
        "validation_status": "passed",
        "file_ownership": [
            {
                "file_path": "src/app.py",
                "owners": [
                    {"work_order_id": "EXEC-001", "access": "write", "sequence": 1}
                ],
            }
        ],
    }
    validate_artifact("worktree_manifest", doc)


def test_complete_m3_validation_results_validates() -> None:
    doc = {
        "run_id": "RUN-20260607-001",
        "schema_version": "1.0.0",
        "generated_at": "2026-06-07T10:01:00Z",
        "work_order_ref": "EXEC-001",
        "overall_passed": True,
        "new_failures_vs_baseline": 0,
        "results": [
            {
                "command_array": ["pytest", "tests/"],
                "exit_code": 0,
                "passed": True,
                "stdout_excerpt": "5 passed in 0.5s",
                "stderr_excerpt": "",
                "started_at": "2026-06-07T10:00:00Z",
                "finished_at": "2026-06-07T10:00:01Z",
                "duration_seconds": 0.5,
                "timed_out": False,
                "policy_allowed": True,
            }
        ],
    }
    validate_artifact("validation_results", doc)
